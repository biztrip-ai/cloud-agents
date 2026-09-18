---
name: provision-cloud-agent
description: >
  Provision an always-on cloud-hosted coding agent: pick a host platform
  (Railway or AWS EC2; pluggable for Fly.io, …), deploy a dev machine image
  with durable storage, install a coding agent (Claude Code, Codex, or
  OpenCode), wire it to a control plane (Flow or Bizzybot/Slack), give it GitHub access
  and the current repo, sync selected env vars from the local machine, and
  hand it a bootstrap task. Use when asked to "provision a cloud agent",
  "run an agent in the cloud", "set up a devbox agent", or similar.
---

# Provision a cloud-running coding agent

The result: a machine on a cloud host that boots a dev image, restores its
tools from durable storage, runs a coding agent connected to a control plane
(so humans can talk to it), and holds a working checkout of the current repo
with GitHub access and the env vars it needs to run the app and its tests.

Every agent this skill creates, and every existing agent it finds, is recorded
in the company's encrypted manifest `companies/<company>/agents.sops.json`,
which lives in the company's private repo at `$CLOUD_AGENTS_HOME` (see
**Agent manifest** below). Read it first with `bin/agents list <company>`:
the user may be asking about an agent that already exists.

The workflow is a spine of seven stages. Stages 1, 3, and 4 are **pluggable**:
each option is a reference file implementing a small contract, and adding a
platform means writing a new reference — not changing this file.

## Stage 0 — Decisions (collect before touching anything)

Ask the user (or read from their request):

| Decision | Options today | Reference |
|---|---|---|
| Host platform | `railway`, `aws` (implemented); `fly` (contract below, not yet written) | `references/hosts/<host>.md` |
| Coding agent | `claude`, `codex`, `opencode` | `references/agents/<agent>.md` |
| Control plane | `flow`, `bizzybot` (implemented); `none` (SSH-only box) | `references/control-planes/<plane>.md` |
| Company | which `companies/<company>/` manifest the agent belongs to (e.g. `biztrip`) | `bin/agents` |
| Repo | default: the current checkout's `git remote get-url origin` | — |
| Env vars to sync | user selects from the local environment (Stage 6) | `scripts/copy-env-vars.sh` |

**Compatibility check**: the control plane constrains the agent. The Flow
bridge today runs `claude` fully; its `codex` harness is a stub and it has no
`opencode` harness. Bizzybot drives Claude Code only. If the user picks a
non-Claude agent with either, say so and offer: Claude for the control plane
now, the other agent side-by-side for SSH use.

Also collect the agent's name/handle, and the secrets the user must generate
themselves (each agent reference says which). **Secrets never pass through the
conversation**: the user runs the commands that read or paste them
(`$(gh auth token)` etc. expand locally and are never printed).

## Stage 1 — Provision the machine (host reference)

Load `references/hosts/<host>.md` and follow its Provision section: create the
compute unit, attach **durable storage**, set the machine image, and give it a
start command that (a) restores tools from durable storage on every boot and
(b) falls back to an idle keep-alive (`sleep infinity`) when the agent isn't
configured yet, so the box is always reachable.

### Host contract (what a host reference must provide)

A new host platform is usable when its reference documents how to:

1. **Provision** a container/VM from a public Docker image.
2. **Attach durable storage** at a stable path (canonically `/workspaces`)
   that survives restarts and redeploys.
3. **Set the boot/start command** and restart the machine.
4. **Set environment variables** (and note whether changes restart the box).
5. **Open a shell** on the box (SSH or equivalent).
6. **Read logs** and **verify a deploy reached a running state** — never
   report success without observing it.
7. State its **quirks**: run-as-root or not, what's wiped on restart, CLI bugs.

## Stage 2 — Durability + coding agent

On the box (host's shell access), everything long-lived goes under the durable
mount. Canonical layout:

```
/workspaces/
  .npm-global/   # node-installed CLIs (the agents live here)
  .python/       # PYTHONUSERBASE
  .claude/ | .codex/ | .opencode/   # agent config/state (per agent reference)
  .gitconfig     # via GIT_CONFIG_GLOBAL
  <handle>/      # control-plane config (e.g. agent.json)
  projects/      # repo checkouts — the agent's working world
```

Load `references/agents/<agent>.md` and follow it: install the CLI into the
durable prefix, set its auth env var (user-generated token), set its
config-dir env var so state persists, and note its privilege rules (Claude
refuses permission-bypass mode as root — the start command must drop to a
non-root user).

Also register the browser MCP (the agent reference's **Browser** section) so
the agent can check web UIs itself.

Verify: run the agent headlessly on the box with a trivial prompt and see a
real reply, then a second prompt that opens a page through the browser MCP,
before going further.

## Stage 3 — Control plane

Load `references/control-planes/<plane>.md`. The control plane is what turns
"a CLI on a box" into "a teammate you can message". It must cover: one-time
registration (identity + token, stored on durable storage), running its daemon
as the box's supervised main process (as a non-root user), and how humans
reach the agent (mentions, DMs, reset/restart commands).

For `none`: skip; the box keeps its idle keep-alive and you use it over SSH.

## Stage 4 — GitHub access + repo checkout

1. The **user** copies their GitHub token to the host's env (host reference's
   set-variable mechanism): `... "GH_TOKEN=$(gh auth token)"`. Warn: host env
   vars are visible to anyone with access to the hosting project.
2. On the box: `gh auth setup-git` (wires git's HTTPS credential helper),
   `git config --global user.name/email`, both persisting via
   `GIT_CONFIG_GLOBAL` on the durable mount. Take the name from
   `gh api user --jq .name`; for the email use
   `<login>@users.noreply.github.com` unless the token has the `user:email`
   scope (`gh api user/emails` 404s otherwise).
3. Determine the repo from the local checkout: `git remote get-url origin`
   (convert `git@github.com:owner/repo.git` → `https://github.com/owner/repo`
   since the box authenticates over HTTPS). Clone into
   `/workspaces/projects/<repo>`. Point the agent/control-plane `cwd` at it —
   **cwd is the agent's identity**.

## Stage 5 — Sync env vars from the local machine

The app in the repo usually needs env vars (database URLs, API keys) that
exist locally. Run `scripts/copy-env-vars.sh`:

```sh
scripts/copy-env-vars.sh --host railway --target <service> NAME1 NAME2 …
scripts/copy-env-vars.sh --host aws --target <ssh-alias> NAME1 NAME2 …
```

It copies each named variable from the local environment (or a `--env-file`)
to the host without printing values. Ask the user which variables the app
needs — propose a list by reading the repo's `.env.example`, compose files, or
config docs, and let the user approve it. Never sync wholesale: local
environments hold secrets the box shouldn't have.

## Stage 6 — Bootstrap task: hand the work to the remote agent

Finish by making the *remote agent* prove the setup. Send it (via the control
plane, or headless CLI if `none`) a bootstrap task:

> Clone is at `/workspaces/projects/<repo>`. Get the app running: install
> dependencies, run the test suite, and start the app. Then verify it in a
> browser — install a headless browser (e.g. `npx playwright install
> chromium --with-deps`) if none is present, load the app's main page, and
> report what you see plus any failures.

(With the browser MCP registered, adjust the task: "verify it with the
chrome-devtools browser tools" instead of installing Playwright.)

Its report is the real end-to-end verification: it exercises the agent auth,
the control plane, GitHub access, the checkout, and the synced env vars in one
shot. Relay the outcome to the user, including anything the agent could not
make work (missing vars, services it can't reach from the box).

## Agent manifest — `companies/<company>/agents.sops.json`

The explicit record of every cloud agent a company runs. It is SOPS-encrypted
(age) and committed to the **company's private repo**, never this public one.
`CLOUD_AGENTS_HOME` points `bin/agents` at the directory that holds that repo's
`.sops.yaml` and `companies/`; if it isn't set, ask the user where it is. Always go
through `bin/agents` (run from the repo root), which decrypts in memory and
re-encrypts on save. **Never write a decrypted copy to disk and never print
private keys or tokens.**

```sh
bin/agents list    <co>                      # what exists
bin/agents show    <co> <agent>              # one entry, private key masked
bin/agents ssh     <co> <agent> '<cmd>'      # interrogate an EC2 agent
bin/agents railway <co> <agent> '<cmd>'      # interrogate a Railway agent
bin/agents upsert  <co> < entry.json         # add or replace an entry by name
bin/agents set     <co> <agent> <dotted.path> '<json>'
bin/agents keygen  <co> <agent>              # new per-agent SSH key → manifest
```

Keep it updated as part of the workflow, not afterwards:

- **Start of Stage 1**: `upsert` a stub entry with `"status": "provisioning"`
  and run `keygen`, so the agent's own SSH key exists before the machine does.
  Every agent gets its own key pair; never reuse a personal `~/.ssh` key.
- **End of Stage 1**: record the host details (instance/service IDs, IP,
  security group, EIP…) so an abandoned run still says what to clean up.
- **After Stage 6**: fill in the rest, set `"status": "active"` and
  `last_verified` to today.
- **Found an agent that isn't listed**: add it with `"status": "unverified"`
  and what you know in `notes`.
- **Tore one down**: `"status": "deleted"`; keep the entry unless asked.
- **Upgrades, token renewals, restarts**: record the new commit/date in
  `control_plane_details` / `claude_auth`.

Store secrets that are needed to *reach or recover* the agent (its SSH private
key). Do not copy the agent's runtime secrets (`CLAUDE_CODE_OAUTH_TOKEN`,
`GH_TOKEN`, …) into the manifest; `env_vars` lists their **names** only — they
live on the box.

Entry shape (`null` for unknowns):

```json
{
  "name": "acme-pm",
  "aliases": ["PM bot"],
  "status": "provisioning | active | unverified | stopped | deleted",
  "created": "2026-01-01T00:00:00Z",
  "last_verified": "2026-01-02",
  "provisioned_by": "provision-cloud-agent",
  "host": {
    "platform": "aws",
    "account": "…", "region": "us-west-2",
    "instance_id": "i-…", "instance_type": "t3.xlarge",
    "public_ip": "…", "key_name": "…", "security_group": "sg-…", "eip_allocation": "eipalloc-…"
  },
  "ssh": {
    "user": "ubuntu", "host": "…", "port": 22,
    "key": { "type": "ed25519", "public": "ssh-ed25519 …", "private": "-----BEGIN OPENSSH PRIVATE KEY-----…" }
  },
  "coding_agent": "claude | codex | opencode",
  "control_plane": "flow | bizzybot | none",
  "control_plane_details": { "package": "…", "commit": "…", "agent_id": "…", "sponsor": "…" },
  "claude_auth": { "method": "claude setup-token", "renewed": "…", "expires_approx": "…" },
  "state_dir": "/workspaces/<handle>",
  "repo": { "url": "https://github.com/owner/repo", "path": "/workspaces/projects/repo" },
  "env_vars": ["GH_TOKEN", "…"],
  "notes": ""
}
```

For Railway, `host` holds `project`, `project_id`, `environment`, `service`,
`service_id`, and there is no `ssh` block (`bin/agents railway` uses the IDs).
Company-level records (Slack workspace, AWS account, shared infrastructure such
as the Bizzybot Central-Dispatch, Slack apps) sit at the top level of the same
file.

## Add-ons

- `references/access/eks-readonly-and-db.md` — give the box read-only
  Kubernetes access and read-only production DB queries without any network
  path into the VPCs (instance role + EKS view policy + in-cluster psql pod).

## Day-2 notes

- Control-plane daemons usually support in-band `/reset`, `/restart`,
  `/update` commands — see the plane's reference.
- The checkout drifts behind the default branch; tell the agent to
  `git pull`, or add a pull to the start command.
- Rotating a token = update the host env var (user-run) + restart.
- Recurring apt/system packages → bake a custom image instead of installing
  on every boot.
