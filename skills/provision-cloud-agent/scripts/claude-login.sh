#!/bin/bash
# Create the box's Claude Code token *on the box* with `claude setup-token`, so
# the long-lived sk-ant-oat01-… token never exists anywhere else. Runs on the
# agent box; drive it over SSH by piping this file in:
#
#   bin/agents ssh <co> <agent> 'bash -s start'          < scripts/claude-login.sh
#       → prints the sign-in URL. The user opens it, signs in, and pastes back
#         the short code the page shows (`<code>#<state>`, ~92 chars). That code
#         is safe to put in chat: only this box's pending login can redeem it.
#   bin/agents ssh <co> <agent> 'bash -s finish <code>'  < scripts/claude-login.sh
#       → feeds the code to the waiting login, captures the token, checks it
#         with a real `claude -p`, upserts CLAUDE_CODE_OAUTH_TOKEN into
#         /workspaces/env/agent.env (0600), and cleans up. Prints no secrets.
#
# Restart the agent service afterwards so the daemon picks the token up.
set -euo pipefail
ENV_FILE=${ENV_FILE:-/workspaces/env/agent.env}
SESSION=claudeauth
set -a; . /workspaces/env/base.env; set +a

case "${1:-}" in
start)
  tmux kill-session -t $SESSION 2>/dev/null || true
  tmux new-session -d -s $SESSION -x 400 -y 50 \
    "set -a; . /workspaces/env/base.env; set +a; claude setup-token; echo SETUP_EXIT=\$?; sleep 1800"
  for _ in $(seq 1 20); do
    url=$(tmux capture-pane -p -J -t $SESSION | grep -oE 'https://claude\.com/cai/oauth/authorize[^ ]*' || true)
    [ -n "$url" ] && { echo "$url"; exit 0; }
    sleep 1
  done
  echo "error: no sign-in URL appeared; see: tmux attach -t $SESSION" >&2
  exit 1
  ;;
finish)
  code=${2:?usage: finish <code>}
  tmux has-session -t $SESSION 2>/dev/null || { echo "error: no pending login; run start first" >&2; exit 1; }
  tmux send-keys -t $SESSION -l "$code"; sleep 1; tmux send-keys -t $SESSION Enter
  tok=""
  for _ in $(seq 1 30); do
    tok=$(tmux capture-pane -p -J -S -200 -t $SESSION | grep -oE 'sk-ant-oat01-[A-Za-z0-9_-]+' | tail -1 || true)
    [ -n "$tok" ] && break
    sleep 1
  done
  tmux kill-session -t $SESSION 2>/dev/null || true
  if [ -z "$tok" ]; then
    echo "error: no token produced (wrong or expired code?). Run start again." >&2
    exit 1
  fi
  reply=$(cd /tmp && CLAUDE_CODE_OAUTH_TOKEN="$tok" claude -p "reply with the single word ok" < /dev/null 2>/dev/null | tail -1)
  if [ "$reply" != "ok" ]; then
    echo "error: new token failed a test prompt; not stored" >&2
    exit 1
  fi
  umask 077
  { grep -v '^CLAUDE_CODE_OAUTH_TOKEN=' "$ENV_FILE" 2>/dev/null || true
    printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$tok"; } > "$ENV_FILE.tmp"
  cat "$ENV_FILE.tmp" > "$ENV_FILE"; rm -f "$ENV_FILE.tmp"; chmod 600 "$ENV_FILE"
  unset tok
  echo "CLAUDE_CODE_OAUTH_TOKEN stored in $ENV_FILE (test prompt ok)"
  ;;
*)
  echo "usage: $0 start | finish <code>" >&2
  exit 2
  ;;
esac
