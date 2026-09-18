# Coding agent: Codex CLI

## Install (durable)

```sh
npm install -g --prefix /workspaces/.npm-global @openai/codex
```

## Auth

Two options:

- **API key** (simplest for headless): set `OPENAI_API_KEY` on the host
  (deliver with the dropbox, `env:OPENAI_API_KEY`; from https://platform.openai.com/api-keys). Usage-billed.
- **ChatGPT subscription sign-in**: `codex login` uses a browser flow. On a
  headless box, run `codex login` locally, then copy `~/.codex/auth.json` to
  `/workspaces/.codex/auth.json` on the host with the dropbox, as a file item
  (`file:codex_auth:/workspaces/.codex/auth.json`). The user pastes the file's
  contents into the page. It contains tokens, so it never goes through the
  conversation.

## Persistence

Set `CODEX_HOME=/workspaces/.codex` so config, auth, and session state live on
the durable mount.

## Headless verification

```sh
codex exec "reply with the single word ok"
```

## Control-plane caveat

The Flow agent-bridge's `codex` harness is a **stub** — registration works but
conversations won't run through it. Until the bridge implements it, pair Codex
with `--harness demo` for wiring checks only, or use Codex over SSH alongside
a Claude-driven control plane.
