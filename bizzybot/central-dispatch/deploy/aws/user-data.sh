#!/bin/bash
#
# cloud-init user data for the Central-Dispatch box (see bizzybot/docs/DEPLOY-AWS.md).
# Launch it with the box's settings inserted after the shebang:
#
#   { echo '#!/bin/bash'
#     echo 'DISPATCH_HOST=central-dispatch.biztrip.ai R2_ACCOUNT_ID=… R2_BUCKET=…'
#     tail -n +2 user-data.sh; } > /tmp/ud.sh
#
# Optional: SECRET_ID (default central-dispatch/prod), AWS_REGION (us-west-2),
# REPO_REF (main), STALE_EVENT_MAX_AGE_MS.
set -euxo pipefail
: "${DISPATCH_HOST:?}" "${R2_ACCOUNT_ID:?}" "${R2_BUCKET:?}"

install -d -m 0755 /etc/central-dispatch
cat > /etc/central-dispatch/host.env <<EOF
DISPATCH_HOST=$DISPATCH_HOST
R2_ACCOUNT_ID=$R2_ACCOUNT_ID
R2_BUCKET=$R2_BUCKET
SECRET_ID=${SECRET_ID:-central-dispatch/prod}
AWS_REGION=${AWS_REGION:-us-west-2}
REPO_REF=${REPO_REF:-main}
STALE_EVENT_MAX_AGE_MS=${STALE_EVENT_MAX_AGE_MS:-}
EOF

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y git
git clone --branch "${REPO_REF:-main}" https://github.com/biztrip-ai/cloud-agents.git /opt/cloud-agents
/opt/cloud-agents/bizzybot/central-dispatch/deploy/aws/install.sh
touch /etc/central-dispatch/.provisioned
