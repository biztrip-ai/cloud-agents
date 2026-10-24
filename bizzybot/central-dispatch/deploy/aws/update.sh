#!/bin/bash
#
# Deploy the latest REPO_REF (main) to this box: pull, re-run install.sh,
# restart the app. Run as root:
#   bin/agents ssh biztrip central-dispatch 'sudo /opt/cloud-agents/bizzybot/central-dispatch/deploy/aws/update.sh'
set -euo pipefail

set -a; . /etc/central-dispatch/host.env; set +a
cd /opt/cloud-agents
git fetch --quiet origin "$REPO_REF"
git checkout --quiet --detach FETCH_HEAD
echo "deploying $(git log -1 --format='%h %s')"

bizzybot/central-dispatch/deploy/aws/install.sh
systemctl restart central-dispatch.service
sleep 2
systemctl is-active central-dispatch.service
curl -fsS http://127.0.0.1:3000/health && echo
