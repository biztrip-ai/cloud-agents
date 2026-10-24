#!/usr/bin/env bash
#
# Set keys in Central-Dispatch's AWS secret (a JSON object of env vars),
# keeping every other key. Values travel over env and stdin only: never in
# argv, `ps`, shell history, or printed back.
#
#   ./central-dispatch/scripts/set-dispatch-secret.sh NAME…                 # prompts for each, hidden
#   ./central-dispatch/scripts/set-dispatch-secret.sh --from-railway NAME…  # copies the Railway values
#
# The running service picks changes up on
#   sudo systemctl restart central-dispatch-prepare central-dispatch litestream
#
set -euo pipefail

SECRET_ID="${SECRET_ID:-central-dispatch/prod}"
REGION="${AWS_REGION:-us-west-2}"
RAILWAY_PROJECT="${RAILWAY_PROJECT_ID:-06312cb9-32a3-4f3d-9e43-1d4b13d1d86b}"
RAILWAY_ENV="${RAILWAY_ENVIRONMENT:-production}"
RAILWAY_SERVICE="${RAILWAY_SERVICE:-claudebot}"

command -v aws  >/dev/null || { echo "aws CLI not found on PATH"; exit 1; }
command -v node >/dev/null || { echo "node not found on PATH"; exit 1; }

FROM_RAILWAY=0
[ "${1:-}" = "--from-railway" ] && { FROM_RAILWAY=1; shift; }
[ $# -gt 0 ] || { echo "usage: $0 [--from-railway] NAME…"; exit 1; }

# New values as a JSON object, built without echoing anything.
if [ "$FROM_RAILWAY" -eq 1 ]; then
  command -v railway >/dev/null || { echo "railway CLI not found on PATH"; exit 1; }
  NEW_JSON=$(
    railway variables -p "$RAILWAY_PROJECT" -e "$RAILWAY_ENV" -s "$RAILWAY_SERVICE" --json |
      NAMES="$*" node -e '
        let s = ""; process.stdin.on("data", (d) => (s += d)).on("end", () => {
          const vars = JSON.parse(s), out = {};
          for (const n of process.env.NAMES.split(" ")) {
            if (!(n in vars)) { console.error(`  ✗ ${n} is not set on Railway`); process.exit(1); }
            out[n] = vars[n];
          }
          process.stdout.write(JSON.stringify(out));
        });'
  )
else
  NEW_JSON="{}"
  for NAME in "$@"; do
    read -rsp "$NAME: " VALUE; echo
    NEW_JSON=$(NEW_JSON="$NEW_JSON" NAME="$NAME" VALUE="$VALUE" node -e '
      const o = JSON.parse(process.env.NEW_JSON);
      o[process.env.NAME] = process.env.VALUE;
      process.stdout.write(JSON.stringify(o));')
  done
  unset VALUE
fi

CURRENT=$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$SECRET_ID" \
  --query SecretString --output text)
CURRENT="$CURRENT" NEW_JSON="$NEW_JSON" node -e '
  process.stdout.write(JSON.stringify({ ...JSON.parse(process.env.CURRENT), ...JSON.parse(process.env.NEW_JSON) }));' |
  aws secretsmanager put-secret-value --region "$REGION" --secret-id "$SECRET_ID" \
    --secret-string file:///dev/stdin >/dev/null

echo "Set in $SECRET_ID (values not printed): $*"
