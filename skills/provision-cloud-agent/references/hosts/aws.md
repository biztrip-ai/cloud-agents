# Host: AWS (EC2)

Satisfies the host contract with a single EC2 instance running Ubuntu 24.04,
reached over SSH. Assumes the `aws` CLI is authenticated (`aws sts
get-caller-identity` works). Pass `--region` on every call — accounts often
have no default region configured.

Unlike a PaaS, an EC2 box is a persistent VM: there is no image or start
command to "set", and nothing is wiped on restart. The **root EBS volume is
the durable storage** — it survives stop/start and reboot. Only `terminate`
destroys it (snapshot first if you care). `/workspaces` is a directory on it.

## Provision

Everything below is scripted by `scripts/aws-user-data.sh` (cloud-init): OS
packages (git, gh, node 22, python3, uv, jq, ripgrep), the `/workspaces`
layout, a base env file, and the `agent.service` systemd unit. The user-data
does **not** install the coding agent or control plane — Stages 2/3 do that
over SSH.

```sh
CO=<company>; R=us-west-2; NAME=<agent-name>; MYIP=$(curl -s https://checkip.amazonaws.com)
AMI=$(aws ssm get-parameter --region $R --query Parameter.Value --output text \
  --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id)
VPC=$(aws ec2 describe-vpcs --region $R --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)

# Record the agent first, then give it its own SSH key. The private key lives
# only in the encrypted company manifest (see "Agent manifest" in SKILL.md).
echo "{\"name\":\"$NAME\",\"status\":\"provisioning\",\"host\":{\"platform\":\"aws\",\"region\":\"$R\"}}" \
  | bin/agents upsert $CO
PUB=$(bin/agents keygen $CO $NAME)
aws ec2 import-key-pair --region $R --key-name $NAME --public-key-material fileb://<(printf '%s\n' "$PUB")
SG=$(aws ec2 create-security-group --region $R --group-name $NAME --description "$NAME ssh" --vpc-id $VPC --query GroupId --output text)
aws ec2 authorize-security-group-ingress --region $R --group-id $SG --protocol tcp --port 22 --cidr $MYIP/32

ID=$(aws ec2 run-instances --region $R --image-id $AMI --instance-type t3.large \
  --key-name $NAME --security-group-ids $SG \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":60,"VolumeType":"gp3"}}]' \
  --user-data file://scripts/aws-user-data.sh \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" \
  --query 'Instances[0].InstanceId' --output text)

# Stable address across stop/start
EIP=$(aws ec2 allocate-address --region $R --domain vpc --query AllocationId --output text)
aws ec2 wait instance-running --region $R --instance-ids $ID
aws ec2 associate-address --region $R --instance-id $ID --allocation-id $EIP
IP=$(aws ec2 describe-addresses --region $R --allocation-ids $EIP --query 'Addresses[0].PublicIp' --output text)
```

Write the host details into the manifest right away, so the box is reachable
(and accounted for) even if a later step fails:

```sh
bin/agents set $CO $NAME host "$(printf '{"platform":"aws","account":"%s","region":"%s","instance_id":"%s","instance_type":"t3.large","public_ip":"%s","key_name":"%s","security_group":"%s","eip_allocation":"%s"}' \
  "$(aws sts get-caller-identity --query Account --output text)" $R $ID $IP $NAME $SG $EIP)"
bin/agents set $CO $NAME ssh.user '"ubuntu"'
bin/agents set $CO $NAME ssh.host "\"$IP\""
```

Every later `ssh $NAME '…'` in this skill means `bin/agents ssh $CO $NAME '…'`,
which uses the agent's stored key. Don't add a `~/.ssh/config` alias pointing
at a personal key.

Sizing: t3.large (2 vCPU / 8 GB) is the floor for Claude Code + an app + a
headless browser; bizzybot pins ~100 MB per live Slack thread. Root 60 GB gp3.

## Wait for cloud-init, then verify

```sh
ssh $NAME 'cloud-init status --wait; test -f /workspaces/.provisioned && echo provisioned'
ssh $NAME 'systemctl is-active agent.service; sudo tail -20 /var/log/cloud-init-output.log'
```

`agent.service` must be `active`; before Stage 3 its process is `sleep
infinity` (idle keep-alive). Never report the box as ready without seeing
`provisioned`.

## Durable layout + start command

`/workspaces` follows the canonical layout in SKILL.md plus:

- `/workspaces/env/base.env` — non-secret env (PATH, `CLAUDE_CONFIG_DIR`,
  `GIT_CONFIG_GLOBAL`, `UV_TOOL_DIR`/`UV_TOOL_BIN_DIR`, `PYTHONUSERBASE`).
- `/workspaces/env/agent.env` — secrets + per-agent settings, mode 600.
- `/workspaces/bin/start-agent.sh` — the unit's ExecStart: execs
  `/workspaces/bin/agent-main.sh` if present, else `sleep infinity`.

Both env files are loaded by the systemd unit **and** by login shells
(`/etc/profile.d/agent-env.sh`), so `ssh box claude -p …` sees the same
environment the daemon does.

The "start command" contract maps to: the control plane writes
`/workspaces/bin/agent-main.sh` (its `<AGENT_MAIN>`, which must itself check
`<AGENT_CONFIGURED_TEST>` and idle if unconfigured), then
`sudo systemctl restart agent.service`. The unit already runs as `ubuntu`
(non-root), so no privilege drop is needed in the script.

## Environment variables

Append/replace `KEY=value` lines in `/workspaces/env/agent.env`, then restart
the unit. `scripts/copy-env-vars.sh --host aws --target <ssh-alias> NAME…`
does this without printing values (upsert per key, over SSH stdin). Manual
equivalent for a single user-generated secret:

```sh
printf 'GH_TOKEN=%s\n' "$(gh auth token)" | ssh $NAME 'cat >> /workspaces/env/agent.env'
ssh $NAME 'sudo systemctl restart agent.service'
```

Changes apply on restart of the unit (a few seconds; no reboot). Vars are
readable by anyone who can SSH in as `ubuntu` or has EC2/SSM access to the
instance — say so before the user copies secrets in.

## Shell, logs, verification

```sh
ssh $NAME '<commands>'                                  # non-interactive
ssh $NAME 'sudo journalctl -u agent.service -n 50 --no-pager'   # daemon logs
ssh $NAME 'systemctl show agent.service -p ActiveState,MainPID,ExecMainStartTimestamp'
ssh $NAME 'ps -o user,pid,cmd -u ubuntu | grep -v grep | grep -E "bizzybot|claude|flow|sleep"'
aws ec2 describe-instance-status --region $R --instance-ids $ID --query 'InstanceStatuses[0].[InstanceState.Name,InstanceStatus.Status,SystemStatus.Status]'
```

After the control plane is the main process, confirm the daemon (not `sleep`)
is `MainPID` and runs as `ubuntu`.

## Lifecycle

```sh
aws ec2 stop-instances --region $R --instance-ids $ID     # keeps disk + EIP; stops compute billing
aws ec2 start-instances --region $R --instance-ids $ID    # unit auto-starts; same IP
aws ec2 create-snapshot --region $R --volume-id <vol> --description "$NAME backup"
aws ec2 terminate-instances --region $R --instance-ids $ID   # DESTROYS the disk; release the EIP too
```

## Quirks summary

- Runs as `ubuntu` with passwordless sudo; no root-drop needed. Root-owned
  files under `/workspaces` (e.g. from a `sudo npm i -g`) break the unit —
  `chown -R ubuntu:ubuntu /workspaces` if in doubt.
- Nothing is ephemeral, so "install if missing" runs once; don't bake tool
  installs into user-data, which only runs on first boot.
- A stopped instance still bills for EBS and for the EIP (~$3.6/mo unattached).
- The security group pins SSH to the IP you provisioned from; re-run
  `authorize-security-group-ingress` from a new network. An SSH timeout the
  next day is almost always this, not the box.
- SSO-backed `aws` credentials expire (often overnight); `aws sso login` is
  interactive and must be run by the user.
- No inbound ports besides 22 — Central-Dispatch style control planes dial
  out, so none are needed.
