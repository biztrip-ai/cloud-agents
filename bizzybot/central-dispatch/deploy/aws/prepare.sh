#!/bin/bash
#
# Before the app starts: load secrets, then restore the database from R2 if the
# file is missing (a new or rebuilt box). Run as root by
# central-dispatch-prepare.service.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
DB=/var/lib/central-dispatch/central-dispatch.db

"$HERE/load-secrets.sh"

# Litestream reads its R2 keys from the environment; they're plain tokens, so
# sourcing the file is safe.
set -a; . /run/central-dispatch/litestream.env; set +a
/usr/bin/litestream restore -config /etc/litestream.yml -if-db-not-exists -if-replica-exists "$DB"
chown -R dispatch:dispatch /var/lib/central-dispatch
