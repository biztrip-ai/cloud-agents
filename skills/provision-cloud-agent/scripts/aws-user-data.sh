#!/bin/bash
# cloud-init user-data for the AWS EC2 host (references/hosts/aws.md).
# Ubuntu 24.04. Idempotent: safe to re-run by hand over SSH as root.
#
# Prepares the OS and the durable layout under /workspaces, and installs a
# systemd unit (agent.service) that runs /workspaces/bin/agent-main.sh as the
# non-root user when the control plane has written it, or idles otherwise.
# It does NOT install the coding agent or the control plane — those are
# Stages 2/3 of SKILL.md, done over SSH so the plugin references stay in charge.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
AGENT_USER=ubuntu
WS=/workspaces

# --- OS packages -------------------------------------------------------------
apt-get update
apt-get install -y --no-install-recommends \
  git curl ca-certificates gnupg jq unzip ripgrep build-essential \
  python3 python3-venv python3-pip sqlite3 tmux

# GitHub CLI (official apt repo)
if ! command -v gh >/dev/null; then
  install -dm755 /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list
  apt-get update && apt-get install -y gh
fi

# Node 22 (NodeSource)
if ! command -v node >/dev/null || [ "$(node -v | cut -c2-3)" -lt 22 ]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi

# uv (Python tool installer) — system-wide binary
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | \
  env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh

# Google Chrome stable (official apt repo) — the agent's headless browser, driven
# through chrome-devtools-mcp (registered for Claude in references/agents/claude.md).
if ! command -v google-chrome-stable >/dev/null; then
  install -dm755 /etc/apt/keyrings
  curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | \
    gpg --dearmor --yes -o /etc/apt/keyrings/google-chrome.gpg
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list
  apt-get update && apt-get install -y google-chrome-stable fonts-liberation fonts-noto-color-emoji
fi

# --- Durable layout ----------------------------------------------------------
mkdir -p $WS/.npm-global/bin $WS/.python $WS/.uv/tools $WS/.uv/bin \
         $WS/.claude $WS/projects $WS/env $WS/bin
touch $WS/.gitconfig

# Base (non-secret) environment shared by the systemd unit and login shells.
cat > $WS/env/base.env <<ENV
PATH=$WS/.npm-global/bin:$WS/.uv/bin:$WS/.python/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
PYTHONUSERBASE=$WS/.python
UV_TOOL_DIR=$WS/.uv/tools
UV_TOOL_BIN_DIR=$WS/.uv/bin
GIT_CONFIG_GLOBAL=$WS/.gitconfig
CLAUDE_CONFIG_DIR=$WS/.claude
ENV

# Secrets + per-agent settings go here (Stage 2-5 append to it). Never printed.
[ -f $WS/env/agent.env ] || : > $WS/env/agent.env
chmod 600 $WS/env/agent.env

# Login shells get the same env, so `ssh box claude` and `gh` just work.
cat > /etc/profile.d/agent-env.sh <<'PROFILE'
if [ -r /workspaces/env/base.env ]; then set -a; . /workspaces/env/base.env; set +a; fi
if [ -r /workspaces/env/agent.env ]; then set -a; . /workspaces/env/agent.env; set +a; fi
PROFILE
# ~/.bashrc runs for non-login interactive shells; make it source the same.
grep -q agent-env.sh /home/$AGENT_USER/.bashrc || \
  echo '. /etc/profile.d/agent-env.sh' >> /home/$AGENT_USER/.bashrc

# --- Supervisor: agent.service ----------------------------------------------
cat > $WS/bin/start-agent.sh <<'START'
#!/bin/bash
# Runs the control plane's daemon if it has been installed, else idles so the
# box is always up and reachable. The control-plane reference writes
# /workspaces/bin/agent-main.sh.
if [ -x /workspaces/bin/agent-main.sh ]; then
  exec /workspaces/bin/agent-main.sh
fi
echo "agent-main.sh not present; idling (box reachable over SSH)"
exec sleep infinity
START
chmod +x $WS/bin/start-agent.sh

cat > /etc/systemd/system/agent.service <<UNIT
[Unit]
Description=Cloud coding agent (control-plane daemon or idle keep-alive)
After=network-online.target
Wants=network-online.target

[Service]
User=$AGENT_USER
Group=$AGENT_USER
WorkingDirectory=$WS
EnvironmentFile=$WS/env/base.env
EnvironmentFile=-$WS/env/agent.env
ExecStart=$WS/bin/start-agent.sh
Restart=always
RestartSec=5
KillMode=mixed
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
UNIT

chown -R $AGENT_USER:$AGENT_USER $WS
systemctl daemon-reload
systemctl enable --now agent.service
echo "user-data complete" > $WS/.provisioned
