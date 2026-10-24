# Deploying Central-Dispatch on AWS

Central-Dispatch runs on one small EC2 instance. Its database is a SQLite file
on the instance's disk, copied continuously to a Cloudflare R2 bucket by
Litestream. This replaces the Railway service + Neon Postgres setup.

```
Slack ──HTTPS──▶ ┌──────────── EC2 t4g.micro (us-west-2) ────────────┐
agents ──WSS───▶ │ Caddy :443 ──▶ node src/index.js :3000            │
                 │                    │                              │
                 │      /var/lib/central-dispatch/central-dispatch.db│
                 │                    │ (WAL)                        │
                 │               Litestream ─────────────────────────┼──▶ R2 bucket
                 └───────────────────────────────────────────────────┘
                   secrets ◀── Secrets Manager (via the instance role)
```

| Piece | Choice |
|---|---|
| Host | EC2 `t4g.micro` (2 vCPU burst, 1 GB, Arm), Ubuntu 24.04, 20 GB gp3, Elastic IP |
| Hostname | `central-dispatch.biztrip.ai` (Cloudflare DNS, A record, DNS only) |
| TLS | Caddy, with Let's Encrypt certificates it obtains and renews itself |
| App | `node src/index.js` under systemd as the `dispatch` user, from a checkout of this repo at `/opt/cloud-agents` |
| Database | SQLite (`node:sqlite`, WAL mode) at `/var/lib/central-dispatch/central-dispatch.db` |
| Backup | Litestream → R2 bucket, continuous (about one second behind) |
| Secrets | One Secrets Manager secret, `central-dispatch/prod`, read at boot by the instance role |
| Access | SSH with a per-box key kept in the company manifest (`bin/agents ssh biztrip central-dispatch`) |

Cost is roughly $11–12 a month on EC2 (instance, IPv4, disk) plus $0.40 for the
secret. R2 storage for a database this size is within its free tier.

Central-Dispatch has to run as **exactly one process**: live agent sockets,
pending OAuth state and the scheduler all live in memory. Don't add replicas or
an autoscaling group.

## Files

Everything the box needs is in `central-dispatch/deploy/aws/`:

| File | What it does |
|---|---|
| `user-data.sh` | cloud-init: installs git, clones this repo, runs `install.sh` |
| `install.sh` | Idempotent setup: packages (Node 22, Caddy, Litestream, AWS CLI), the `dispatch` user, config, systemd units, `npm ci`. Safe to re-run. |
| `update.sh` | Pulls `main`, re-runs `install.sh`, restarts the app |
| `load-secrets.sh` | Fetches the secret and writes `/run/central-dispatch/app.env` and `litestream.env` (tmpfs, root only) |
| `prepare.sh`, `central-dispatch-prepare.service` | Oneshot before the app: loads secrets, restores the DB from R2 if the file is missing |
| `central-dispatch.service` | The app |
| `litestream.service`, `litestream.yml` | Replication to R2 |
| `Caddyfile` | TLS + reverse proxy (WebSockets pass through unchanged) |

Scripts run from a laptop, in `central-dispatch/scripts/`:

| Script | What it does |
|---|---|
| `set-dispatch-secret.sh NAME…` | Reads each value hidden and merges it into the Secrets Manager secret. Never prints values. |
| `set-slack-apps.sh --target aws` | Same as before, but reads and writes `SLACK_APPS` in Secrets Manager and restarts the service |
| `migrate-postgres-to-sqlite.js` | One-time copy of every table from Postgres (Neon) into a fresh SQLite file |

## Configuration

The box's own settings are in `/etc/central-dispatch/host.env`, written once by
`user-data.sh` from the values given at launch:

```
DISPATCH_HOST=central-dispatch.biztrip.ai
R2_ACCOUNT_ID=…
R2_BUCKET=…
SECRET_ID=central-dispatch/prod
AWS_REGION=us-west-2
REPO_REF=main
STALE_EVENT_MAX_AGE_MS=…        # copied from Railway
```

`install.sh` derives the app's `/etc/central-dispatch/config.env` from it
(`PORT=3000`, `PUBLIC_URL`, `DB_PATH`). To change a setting, edit `host.env`
and run `update.sh`.

Secrets are keys of the JSON secret `central-dispatch/prod`:

| Key | Notes |
|---|---|
| `SLACK_APPS` | JSON array of Slack apps (see the central-dispatch README) |
| `ADMIN_KEY` | Copied from Railway so existing admin tooling keeps working |
| `SESSION_SECRET` | Copied from Railway so dashboard sessions survive the move |
| `LITESTREAM_ACCESS_KEY_ID`, `LITESTREAM_SECRET_ACCESS_KEY` | An R2 API token scoped to the backup bucket, Object Read & Write |

`DATABASE_URL` is **not** set: without it Central-Dispatch uses SQLite.

The instance role may only call `secretsmanager:GetSecretValue` on this one
secret. A changed secret takes effect on `sudo systemctl restart
central-dispatch-prepare central-dispatch litestream`.

## Database and backups

- SQLite runs in WAL mode with a 5 s busy timeout, which Litestream needs.
- Litestream ships every WAL change to `s3://<R2_BUCKET>/central-dispatch` on
  the R2 endpoint, and takes periodic snapshots.
- On boot, `central-dispatch-prepare` runs
  `litestream restore -if-db-not-exists -if-replica-exists`. If the disk is ever
  lost, a new instance with the same secret comes up with the data restored, at
  most a second or two behind.
- The database holds Slack bot and user tokens and Mailgun keys in plain text.
  Keep the R2 bucket private, and scope its token to that one bucket.

Check replication:

```sh
bin/agents ssh biztrip central-dispatch 'sudo -u dispatch litestream ltx -config /etc/litestream.yml /var/lib/central-dispatch/central-dispatch.db | tail -3'
bin/agents ssh biztrip central-dispatch 'sudo journalctl -u litestream -n 20 --no-pager'
```

Test a restore into a scratch path every so often:

```sh
bin/agents ssh biztrip central-dispatch 'sudo -u dispatch litestream restore -config /etc/litestream.yml -o /tmp/restore-test.db /var/lib/central-dispatch/central-dispatch.db && sqlite3 /tmp/restore-test.db "select count(*) from agents" && rm /tmp/restore-test.db'
```

## Provisioning

Done once, from a laptop with `aws` (admin SSO) and `bin/agents` set up.
`R=us-west-2` throughout.

1. **R2 bucket and token** (Cloudflare dashboard): create a private bucket, then
   an R2 API token with *Object Read & Write* on that bucket only. Note the
   account ID and bucket name. The token's keys go into the secret in step 3,
   never into chat.
2. **Secret**: create `central-dispatch/prod`, then fill it:
   ```sh
   aws secretsmanager create-secret --region $R --name central-dispatch/prod --secret-string '{}'
   central-dispatch/scripts/set-dispatch-secret.sh --from-railway SLACK_APPS ADMIN_KEY SESSION_SECRET
   central-dispatch/scripts/set-dispatch-secret.sh LITESTREAM_ACCESS_KEY_ID LITESTREAM_SECRET_ACCESS_KEY
   ```
   `--from-railway` pipes the current Railway values straight into the secret
   without printing them. The second call prompts for each value, hidden.
3. **IAM**: role `central-dispatch` (trusted by `ec2.amazonaws.com`) with an
   inline policy allowing `secretsmanager:GetSecretValue` on
   `arn:aws:secretsmanager:$R:<account>:secret:central-dispatch/prod-*`, and an
   instance profile of the same name.
4. **Manifest entry and SSH key**: `central-dispatch` in the `biztrip` manifest,
   `"role": "control-plane"`, its own key from `bin/agents keygen`.
5. **Security group** `central-dispatch`: 80 and 443 from anywhere (Let's
   Encrypt and Slack), 22 from the operator's IP.
6. **Instance**: `t4g.micro`, Ubuntu 24.04 arm64, 20 GB gp3, IMDSv2 required,
   the instance profile, and `user-data.sh` with the hostname and R2 settings
   prepended. Associate an Elastic IP.
7. **DNS** (Cloudflare): an `A` record `central-dispatch.biztrip.ai` → the Elastic IP,
   **DNS only** (grey cloud) so Caddy can get its certificate. `*.dev.biztrip.ai`
   is a wildcard to the dev cluster, so stay out of that subdomain.
8. **Verify**: `curl https://central-dispatch.biztrip.ai/health` returns
   `{"ok":true}`, `journalctl -u central-dispatch` shows `storage: SQLite`, and
   Litestream shows replication.

Record the instance ID, Elastic IP, security group and role in the manifest
as each step finishes.

## Deploying changes

```sh
bin/agents ssh biztrip central-dispatch 'sudo /opt/cloud-agents/bizzybot/central-dispatch/deploy/aws/update.sh'
```

`update.sh` pulls `main`, runs `install.sh` (which does `npm ci` and refreshes
units and config), and restarts the app. Agents reconnect on their own and
replay missed events by `lastSeq`. There is no auto-deploy on push yet.

Logs: `sudo journalctl -u central-dispatch -f`, `-u litestream`, `-u caddy`.

## Cutover from Railway

Railway and Neon stay untouched until this point, so the rollback is to
switch the URLs back.

1. **Deploy** to AWS with an empty database and check `/health`.
2. **Freeze Railway**: scale the `claudebot` service to zero replicas. Slack
   retries events it can't deliver for a while, so a short window loses
   little.
3. **Copy the data**: stop `central-dispatch` on the box, delete the empty
   database, then run `migrate-postgres-to-sqlite.js` on the box with the Neon
   URL piped in over stdin. It copies all six tables (`agents`, `events`,
   `agent_user_tokens`, `workspace_settings`, `scheduled_tasks`, `dropboxes`)
   and prints row counts per table. Compare them with Neon. Start the service.
4. **Repoint Slack**: for each Slack app, sync its manifest so the event and
   OAuth redirect URLs use `https://central-dispatch.biztrip.ai`
   (`GET /slack/manifest?name=<App>` gives the new manifest;
   `slack manifest sync` from the app's project in the private repo).
5. **Repoint agents**: the wrapper saves `central_url` in its config on first
   run, so changing its default isn't enough. On each agent box, add
   `CENTRAL_URL=https://central-dispatch.biztrip.ai` to `/workspaces/env/agent.env`
   and restart `agent.service`. Check `connected to Central-Dispatch` in its
   journal. Also change `DEFAULT_CENTRAL_URL` in the wrapper for new installs.
6. **Watch** for a day: messages flowing, scheduled tasks firing, Litestream
   replicating.
7. **Retire** the Railway service and the `claudebot` schema in Neon. Update
   the hosted URL in the READMEs, CLAUDE.md and the provisioning skill.

**Rollback** before step 7: scale Railway back up, revert the Slack manifests
and remove `CENTRAL_URL` from the agents. Anything written on AWS after the
switch (new agents, scheduled-task runs) is not copied back.
