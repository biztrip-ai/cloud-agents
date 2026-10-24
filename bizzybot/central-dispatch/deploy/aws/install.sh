#!/bin/bash
#
# Set up (or refresh) the Central-Dispatch box: packages, the `dispatch` user,
# config, systemd units, Caddy, Litestream, npm deps. Idempotent; update.sh
# runs it on every deploy. Run as root. Reads /etc/central-dispatch/host.env.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
APP_DIR=$(cd "$HERE/../.." && pwd)
DATA_DIR=/var/lib/central-dispatch
NODE_MAJOR=22
LITESTREAM_VERSION=0.5.17

set -a; . /etc/central-dispatch/host.env; set +a
export DEBIAN_FRONTEND=noninteractive
ARCH=$(dpkg --print-architecture)   # arm64 | amd64

# --- Packages (each installed only when missing or at the wrong version) ---
apt-get install -y curl jq unzip sqlite3 gnupg debian-keyring debian-archive-keyring apt-transport-https

if [ "$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null)" != "$NODE_MAJOR" ]; then
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
  apt-get install -y nodejs
fi

if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y caddy
fi

if ! /usr/local/bin/aws --version >/dev/null 2>&1; then
  tmp=$(mktemp -d)
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o "$tmp/awscli.zip"
  unzip -q "$tmp/awscli.zip" -d "$tmp"
  "$tmp/aws/install" --update
  rm -rf "$tmp"
fi

if ! litestream version 2>/dev/null | grep -q "$LITESTREAM_VERSION"; then
  lsarch=$([ "$ARCH" = amd64 ] && echo x86_64 || echo "$ARCH")
  deb=$(mktemp --suffix=.deb)
  curl -fsSL -o "$deb" \
    "https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-${LITESTREAM_VERSION}-linux-${lsarch}.deb"
  dpkg -i "$deb"
  rm -f "$deb"
fi

# --- User, data dir, app deps ---
id dispatch >/dev/null 2>&1 \
  || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin dispatch
install -d -m 0750 -o dispatch -g dispatch "$DATA_DIR"
(cd "$APP_DIR" && npm ci --omit=dev --no-audit --no-fund)

# --- Config ---
cat > /etc/central-dispatch/config.env <<EOF
PORT=3000
PUBLIC_URL=https://$DISPATCH_HOST
DB_PATH=$DATA_DIR/central-dispatch.db
NODE_ENV=production
EOF
[ -n "${STALE_EVENT_MAX_AGE_MS:-}" ] \
  && echo "STALE_EVENT_MAX_AGE_MS=$STALE_EVENT_MAX_AGE_MS" >> /etc/central-dispatch/config.env

install -m 0644 "$HERE/litestream.yml" /etc/litestream.yml

# Caddy gets the hostname from host.env; restart it only when its config changed.
install -d /etc/systemd/system/caddy.service.d
printf '[Service]\nEnvironmentFile=/etc/central-dispatch/host.env\n' > /etc/systemd/system/caddy.service.d/central-dispatch.conf
caddy_changed=0
cmp -s "$HERE/Caddyfile" /etc/caddy/Caddyfile || caddy_changed=1
install -m 0644 "$HERE/Caddyfile" /etc/caddy/Caddyfile

# --- systemd ---
for unit in central-dispatch-prepare.service central-dispatch.service litestream.service; do
  sed "s#@APP_DIR@#$APP_DIR#g" "$HERE/$unit" > "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable central-dispatch.service litestream.service caddy.service
if [ "$caddy_changed" = 1 ]; then systemctl restart caddy; fi
systemctl start central-dispatch.service litestream.service
