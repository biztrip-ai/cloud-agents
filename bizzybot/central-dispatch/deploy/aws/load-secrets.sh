#!/bin/bash
#
# Fetch the Central-Dispatch secret (a JSON object of env vars) and split it
# into systemd env files on tmpfs, readable by root only:
#   /run/central-dispatch/app.env         everything except LITESTREAM_*
#   /run/central-dispatch/litestream.env  LITESTREAM_* (the R2 keys)
# Values are never printed.
set -euo pipefail
: "${SECRET_ID:?}" "${AWS_REGION:?}"

install -d -m 0700 /run/central-dispatch
umask 077
json=$(/usr/local/bin/aws secretsmanager get-secret-value --region "$AWS_REGION" \
  --secret-id "$SECRET_ID" --query SecretString --output text)

# One KEY="value" line per entry; tojson gives systemd a double-quoted,
# backslash-escaped value, and non-string values (a JSON array) are compacted.
to_env='to_entries[] | select(.key | test("^[A-Za-z_][A-Za-z0-9_]*$")) | select(.key | startswith("LITESTREAM_") | (if $ls then . else not end)) | "\(.key)=\(.value | if type == "string" then . else tojson end | tojson)"'
jq -r --argjson ls false "$to_env" <<<"$json" > /run/central-dispatch/app.env
jq -r --argjson ls true  "$to_env" <<<"$json" > /run/central-dispatch/litestream.env
echo "secrets loaded: app.env $(wc -l < /run/central-dispatch/app.env) keys, litestream.env $(wc -l < /run/central-dispatch/litestream.env) keys"
