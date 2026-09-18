# Coding agent: OpenCode

## Install (durable)

```sh
npm install -g --prefix /workspaces/.npm-global opencode-ai
```

## Auth

`opencode auth login` is interactive (pick provider, paste API key). On a
headless box, prefer provider env vars delivered with the dropbox (SKILL.md **Delivering secrets**), e.g.
`ANTHROPIC_API_KEY` or `OPENAI_API_KEY` — OpenCode picks them up without a
stored credential. Alternatively run `opencode auth login` once over an
interactive SSH session; it stores credentials under `~/.local/share/opencode`.

## Persistence

Set `XDG_DATA_HOME=/workspaces/.opencode-data` and
`XDG_CONFIG_HOME=/workspaces/.opencode-config` before first run so auth,
config, and session state land on the durable mount (OpenCode follows XDG
paths).

## Headless verification

```sh
opencode run "reply with the single word ok"
```

## Control-plane caveat

The Flow agent-bridge has **no OpenCode harness** today. OpenCode on a box is
SSH-only until a harness exists; pair it with control plane `none`, or run it
alongside a Claude-driven bridge.
