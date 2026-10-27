#!/bin/bash
# Give an agent box its own GitHub identity through a GitHub App, instead of a
# person's token. Pushes, merges and comments then show as `<app>[bot]`, and
# the App's permissions (not a person's) bound what the agent can do. Runs on
# the agent box; copy it there and run it over SSH:
#
#   bin/agents ssh <co> <agent> 'install -D -m 755 /dev/stdin /workspaces/bin/github-app-token &&
#     /workspaces/bin/github-app-token install <app-id> <installation-id> <app-slug>' < scripts/github-app-auth.sh
#       → installs a `gh`
#         wrapper in /usr/local/bin (ahead of /usr/bin on PATH), points git's
#         credential helper at it, sets the git identity to the App's bot, and
#         checks that a token can be minted. Needs the App's private key at
#         /workspaces/env/github-app.pem (0600), delivered with the dropbox as
#         `file:github-app.pem:/workspaces/env/github-app.pem`.
#   /workspaces/bin/github-app-token [token]
#       → prints an installation token, minting a new one when the cached one
#         is 45 minutes old (they last 60).
#
# Then remove GH_TOKEN from agent.env (it would win over the wrapper) and
# restart the agent service. Prints no secrets.
set -euo pipefail
CONF=/workspaces/env/github-app.env
KEY=/workspaces/env/github-app.pem
CACHE=/workspaces/env/github-app.token
SELF=/workspaces/bin/github-app-token
MAX_AGE_S=$((45 * 60))

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

mint() {
  . "$CONF"
  local now header payload sig
  now=$(date +%s)
  header=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
  payload=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' $((now - 60)) $((now + 540)) "$GH_APP_ID" | b64url)
  sig=$(printf '%s.%s' "$header" "$payload" | openssl dgst -sha256 -sign "$KEY" -binary | b64url)
  curl -fsS -X POST \
    -H "Authorization: Bearer $header.$payload.$sig" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/app/installations/$GH_APP_INSTALLATION_ID/access_tokens" |
    jq -er .token
}

token() {
  exec 9>"$CACHE.lock"
  flock 9
  if [ ! -s "$CACHE" ] || [ $(($(date +%s) - $(stat -c %Y "$CACHE"))) -ge $MAX_AGE_S ]; then
    local t
    t=$(mint)
    (umask 077; printf '%s\n' "$t" >"$CACHE.new")
    mv "$CACHE.new" "$CACHE"
  fi
  cat "$CACHE"
}

case "${1:-token}" in
token)
  token
  ;;
install)
  app_id=${2:?app id}; inst_id=${3:?installation id}; slug=${4:?app slug}
  [ -s "$KEY" ] || { echo "missing $KEY (deliver it with the dropbox first)" >&2; exit 1; }
  chmod 600 "$KEY"
  (umask 077; printf 'GH_APP_ID=%s\nGH_APP_INSTALLATION_ID=%s\nGH_APP_SLUG=%s\n' "$app_id" "$inst_id" "$slug" >"$CONF")
  [ -x "$SELF" ] || { echo "copy this script to $SELF first (see the header)" >&2; exit 1; }
  rm -f "$CACHE"
  "$SELF" token >/dev/null && echo "minted an installation token ✓"

  sudo tee /usr/local/bin/gh >/dev/null <<'EOF'
#!/bin/sh
# GitHub App identity for this agent (see /workspaces/bin/github-app-token).
if [ -s /workspaces/env/github-app.env ]; then
  GH_TOKEN=$(/workspaces/bin/github-app-token) || exit 1
  export GH_TOKEN
fi
exec /usr/bin/gh "$@"
EOF
  sudo chmod 755 /usr/local/bin/gh

  set -a; . /workspaces/env/base.env; set +a   # GIT_CONFIG_GLOBAL
  for host in https://github.com https://gist.github.com; do
    git config --global --replace-all "credential.$host.helper" ''
    git config --global --add "credential.$host.helper" '!/usr/local/bin/gh auth git-credential'
  done
  bot_id=$(curl -fsS "https://api.github.com/users/$slug%5Bbot%5D" | jq -er .id)
  git config --global user.email "$bot_id+$slug[bot]@users.noreply.github.com"
  echo "git identity: $(git config --global user.name) <$(git config --global user.email)>"
  grep -q '^GH_TOKEN=' /workspaces/env/agent.env 2>/dev/null &&
    echo "NOTE: remove GH_TOKEN from /workspaces/env/agent.env, then restart agent.service"
  exit 0
  ;;
*)
  echo "usage: $0 [token] | install <app-id> <installation-id> <app-slug>" >&2
  exit 2
  ;;
esac
