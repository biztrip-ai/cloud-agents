# Control plane: Bizzybot (Slack)

Makes the agent a Slack app user (`@bizzy`, `@cosmo`, `@omni` on the hosted
Central-Dispatch, or your own app if you self-host it). Source:
https://github.com/biztrip-ai/cloud-agents (`bizzybot/`) — the `agent-wrapper/` half runs on
the box; it dials out over a WebSocket to Central-Dispatch, so no inbound
ports. Requires coding agent **Claude** (it drives Claude Code via the
Agent SDK); `gh` on PATH.

## Inputs

- **Bot user id** (for sending it a mention programmatically): on the box,
  `POST $CENTRAL_URL/api/register {"token": …}` returns `slackBotToken`; call
  Slack `auth.test` with it and print only `user`/`user_id`. Slack search
  tools generally can't find bot users.

- **Registration token**: the user signs in with Slack at the Central-Dispatch
  dashboard (hosted: https://claudebot-production-34ba.up.railway.app),
  installs one of the Slack apps to the workspace, and copies that agent's
  token from the dashboard. **One token = one agent identity, and
  Central-Dispatch delivers events to the most recent connection for a
  token** — a second wrapper with the same token silently steals the agent
  from the first. Use a fresh app/token for the cloud box, or stop the local
  wrapper first.
- Which repo the agent works in (`CLAUDE_CWD`).

## Install (durable)

Python tool via `uv`; with `UV_TOOL_DIR`/`UV_TOOL_BIN_DIR` from the host's
base env it lands under `/workspaces/.uv` and `bizzybot` is on PATH:

```sh
uv tool install "git+https://github.com/biztrip-ai/cloud-agents.git#subdirectory=bizzybot/agent-wrapper"
bizzybot --help >/dev/null 2>&1 || command -v bizzybot   # proves the entrypoint exists
```

The package also provides `bizzybot-dropbox`, the standard way to get secrets
onto the box (SKILL.md **Delivering secrets**). It works before registration,
so install the package first and request `REGISTRATION_TOKEN` through it.

Update later: see **Upgrade** below. A plain `ssh host '…'` session on AWS
doesn't get `UV_TOOL_DIR`/`UV_TOOL_BIN_DIR`, so pass them explicitly.
Otherwise `uv` looks in `~/.local/share/uv/tools` and reports that the tool
isn't installed.

## Configure (env, on the host's set-variable mechanism)

The wrapper reads its config from the environment (also a `.env` in its cwd;
prefer env so the host's mechanism owns it). Set:

| Var | Value | Why |
|---|---|---|
| `REGISTRATION_TOKEN` | via the dropbox (`env:REGISTRATION_TOKEN`) | identity; **secret**. 48 hex chars |
| `BIZZYBOT_STATE_DIR` | `/workspaces/<handle>` | sessions, acked seq, logs — durable |
| `CLAUDE_CWD` | `/workspaces/projects/<repo>` | the agent's identity/world |
| `CLAUDE_CHROME` | `0` | default `1` adds `--chrome` (Claude-in-Chrome, needs desktop Chrome + extension). The server's browser is chrome-devtools-mcp instead (agent reference) |
| `CLAUDE_PERMISSION_MODE` | `bypassPermissions` (default) | no approval UI in Slack |
| `CENTRAL_URL` | omit for hosted | only for a self-hosted Central-Dispatch |
| `PR_REVIEW_CHANNEL` etc. | optional | see `agent-wrapper/.env.example` |

Per-agent settings that should reach the `claude` subprocess (OpenRouter keys,
Sentry, Mailgun) go in `$BIZZYBOT_STATE_DIR/settings.env` instead — every key
there is exported into each Claude session.

**No stdin under a supervisor**: with `REGISTRATION_TOKEN` unset the wrapper
prompts on stdin and crashes under systemd. `agent-main.sh` below idles until
the token exists, satisfying the host's "always reachable" rule.

## Daemon as the box's main process

Write the host's `<AGENT_MAIN>` (for AWS: `/workspaces/bin/agent-main.sh`,
picked up by `agent.service`):

```sh
#!/bin/bash
# <AGENT_CONFIGURED_TEST>: registration token present and the tool installed
if [ -z "${REGISTRATION_TOKEN:-}" ] || ! command -v bizzybot >/dev/null; then
  echo "bizzybot not configured (REGISTRATION_TOKEN / install missing); idling"
  exec sleep infinity
fi
export BIZZYBOT_STATE_DIR="${BIZZYBOT_STATE_DIR:-/workspaces/bizzybot}"
mkdir -p "$BIZZYBOT_STATE_DIR"
cd "$BIZZYBOT_STATE_DIR"
exec bizzybot
```

Then restart the unit. Healthy logs (journal or
`$BIZZYBOT_STATE_DIR/logs/agent-wrapper-<ts>-<pid>.log`):

```
preflight: claude ✓
preflight: gh authenticated ✓
preflight: git identity ✓ (...)
registered with Central-Dispatch ... / websocket connected
```

`preflight: claude` missing is fatal (check PATH in the unit env); `gh`/git
identity are warnings — fix them anyway (Stage 4) or pushes fail.

Note: the wrapper drives Claude through the Agent SDK's **bundled** CLI, not
the `claude` on PATH; the durable npm install is for SSH/headless use. Auth
still comes from `CLAUDE_CODE_OAUTH_TOKEN` in the env, so the Stage 2
headless test remains the right check.

## Humans use it via

- @-mention the app in a channel (replies in a thread) or DM it.
- `!stop` interrupts the running turn; `!clear` resets the thread's session;
  `!help`.
- Threads run concurrently in the one `CLAUDE_CWD`; the wrapper's system
  prompt tells the agent to use `git worktree` per task. Advisory only.
- If the box is offline, Central-Dispatch queues events and replays on
  reconnect (cursor in `agent-wrapper-state.json`).

## Recovery / day 2

- Lost or rotated token: reissue it from the dashboard, then run
  `bizzybot-dropbox request --restart 'env:REGISTRATION_TOKEN::…'`.
- Upgrade (restarting ends the open `claude` subprocesses. First check that
  they're idle with `ps -eo pid,etime,pcpu,comm | grep claude`. Their sessions
  are listed in `sessions.json` and resume on reconnect):
  ```sh
  ssh <alias> 'export UV_TOOL_DIR=/workspaces/.uv/tools UV_TOOL_BIN_DIR=/workspaces/.uv/bin
    uv tool upgrade bizzybot-agent-wrapper && sudo systemctl restart agent.service'
  ```
  Verify with `journalctl -u agent.service`: the `preflight` lines should show ✓,
  followed by `connected to Central-Dispatch`. Record the new commit in
  the company manifest (`bin/agents set <co> <agent> control_plane_details.commit …`).
- Idle `claude` subprocesses are reaped after `SESSION_IDLE_TIMEOUT_S`
  (default 4 h); sessions resume transparently.
- Logs are per run, capped 1 MiB + one rotation; prune `logs/` yourself.
