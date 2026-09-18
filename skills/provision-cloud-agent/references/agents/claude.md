# Coding agent: Claude Code

## Install (durable)

```sh
npm install -g --prefix /workspaces/.npm-global @anthropic-ai/claude-code
```

## Auth

Create the token **on the box**, never on the user's machine, with
`scripts/claude-login.sh`. It wraps `claude setup-token` in a tmux session:

```sh
bin/agents ssh <co> <agent> 'bash -s start' < scripts/claude-login.sh
#   → prints https://claude.com/cai/oauth/authorize?...  (give it to the user)
bin/agents ssh <co> <agent> 'bash -s finish <code>' < scripts/claude-login.sh
#   → "CLAUDE_CODE_OAUTH_TOKEN stored in /workspaces/env/agent.env (test prompt ok)"
```

The user opens the URL, signs in with the account the agent should use, and
pastes back the **short code** the page shows (`<code>#<state>`, about 92
characters). The code is safe to put in chat: only the login waiting on this
box can redeem it. The script feeds the code in, captures the 1-year
`sk-ant-oat01-…` token, checks it with a real prompt, and upserts it into
`agent.env`. Then restart the agent service.

Don't have the user run `claude setup-token` locally and paste anything.
Its mid-flow code (not a token) is easy to mistake for the token. A stored
value that doesn't start with `sk-ant-oat01-` means that happened, and it
gives `401 Invalid bearer token` / "Not logged in".

API-key alternative (billed per use): deliver `ANTHROPIC_API_KEY` with the
dropbox (`env:ANTHROPIC_API_KEY`).

## Persistence

Set `CLAUDE_CONFIG_DIR=/workspaces/.claude` so settings/history survive
redeploys. Do **not** copy `~/.claude` credentials from the local machine —
tokens there rotate via refresh and sharing them across machines causes
sign-out conflicts; `setup-token` exists for exactly this.

## Browser (chrome-devtools MCP)

Agents need a real browser to check their work. The host installs Google
Chrome stable (on AWS, `scripts/aws-user-data.sh` does it). Register
[chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp)
for Claude at **user scope**. With `CLAUDE_CONFIG_DIR=/workspaces/.claude` it
lands in `/workspaces/.claude/.claude.json`, so it survives redeploys and
every session loads it, including sessions started by the control plane:

```sh
claude mcp add --scope user chrome-devtools -- npx -y chrome-devtools-mcp@latest --headless --isolated
claude mcp list | grep chrome-devtools      # expect "✔ Connected"
```

- `--headless` because there's no display. `--isolated` gives each session a
  throwaway profile, so concurrent Slack threads don't share cookies or logins.
- MCP servers load when a session starts. Existing sessions don't get it, but
  new threads do; no daemon restart is needed.
- Keep `CLAUDE_CHROME=0` for Bizzybot. `--chrome` is Claude-in-Chrome, which
  needs a desktop Chrome with the extension, not this.

Verify with a real session before calling the box ready:

```sh
claude -p "Using the chrome-devtools MCP tools, open https://example.com and reply with only the page title." \
  --allowedTools "mcp__chrome-devtools__*" < /dev/null      # → Example Domain
```

## Privileges

Claude Code **refuses `--dangerously-skip-permissions` as root** ("cannot be
used with root/sudo privileges"). Control-plane daemons pass that flag, so the
daemon must run as a non-root user (see the host reference's start command).

## Headless verification

```sh
claude -p "reply with the single word ok"
```

A real reply proves install + auth. `401` → wrong/expired token.
