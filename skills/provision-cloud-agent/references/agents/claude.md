# Coding agent: Claude Code

## Install (durable)

```sh
npm install -g --prefix /workspaces/.npm-global @anthropic-ai/claude-code
```

## Auth

The **user** generates a long-lived subscription token on their local machine:

```sh
claude setup-token
```

It opens a browser for approval, then the **CLI prints the token at the end**
— `sk-ant-oat01-…`. NOT the shorter code shown in the browser mid-flow (that
code gets pasted back into the CLI; a stored value that doesn't start with
`sk-ant-oat01-` is the wrong thing and yields `401 Invalid bearer token`).

Set on the host as `CLAUDE_CODE_OAUTH_TOKEN` (user-run). API-key alternative:
`ANTHROPIC_API_KEY` (usage-billed).

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
