# Control plane: Flow (agent-bridge)

Makes the agent a first-class member of a Flow workspace: real presence, DMs,
@-mentions, threads. Requires coding agent **Claude** today (the bridge's
`codex` harness is a stub; no `opencode` harness).

## Inputs

- One-time invite code (`flow-XXXX-XXXX`): the user clicks **Invite your
  Agent** at the bottom of the Flow sidebar. Do not waste it on a dry run.
- Agent display name + handle.

## Install (durable)

```sh
npm install -g --prefix /workspaces/.npm-global flow-agent-bridge
```

## Register (one-time, on the box)

Run in a durable directory — the config it writes is the agent's identity:

```sh
mkdir -p /workspaces/<handle> && cd /workspaces/<handle>
timeout 45 flow-agent-bridge --invite <CODE> --name <NAME> --handle <handle> \
  --harness claude --cwd /workspaces/projects/<repo>
```

Redemption is immediate (no approval step) and writes `agent.json` (chmod
600 — holds the agent token; never cat it). The daemon it starts can be left
to die with the timeout; the start command owns the daemon from here. To
change `cwd` later, edit `agent.json` with `jq` and restart.

## Daemon as the box's main process

Plug into the host start command (as the non-root user):

- `<AGENT_CONFIGURED_TEST>`: `-f /workspaces/<handle>/agent.json`
- `<AGENT_MAIN>`:
  `bash -c "cd /workspaces/<handle> && exec /workspaces/.npm-global/bin/flow-agent-bridge run agent.json"`

The bridge supervises itself (crash → respawn with backoff). Healthy logs:

```
[bridge …] <NAME> <@id> online in "<workspace>" — … cwd=…
[bridge …] connected (presence online)
```

## Humans use it via

- @-mention in a channel (replies in a thread) or DM it.
- `/reset` = fresh conversation; `/restart` = relaunch daemon;
  `/update` = bridge npm-updates itself and restarts.
- Interrupt button / 🛑 reaction stops a running turn.

## Recovery

Lost `agent.json` → `flow-agent-bridge login` (username + key) mints a fresh
token, revoking the old one. One live token per identity — an interactive
`mcp-init`/`login` elsewhere kills the daemon's token; register a separate
identity for interactive MCP use.
