#!/bin/bash
#
# Run a litestream command with this box's config and R2 keys. Run as root:
#   sudo …/deploy/aws/litestream.sh ltx /var/lib/central-dispatch/central-dispatch.db
#   sudo …/deploy/aws/litestream.sh restore -o /tmp/restore-test.db /var/lib/central-dispatch/central-dispatch.db
set -euo pipefail
[ $# -gt 0 ] || { echo "usage: $0 <litestream command> [args…]"; exit 1; }

set -a; . /etc/central-dispatch/host.env; . /run/central-dispatch/litestream.env; set +a
cmd=$1; shift
exec /usr/bin/litestream "$cmd" -config /etc/litestream.yml "$@"
