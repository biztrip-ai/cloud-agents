#!/usr/bin/env bash
# Copy selected env vars from the local environment to a cloud host, without
# ever printing their values.
#
# Usage:
#   copy-env-vars.sh --host railway --target <service> [--env-file .env] NAME1 NAME2 ...
#
#   --host      Host platform (adapter below). Currently: railway, aws
#   --target    Host-specific destination (railway: the service name/id;
#               aws: the ssh alias/host of the box)
#   --env-file  Optionally source names from a dotenv-style file first;
#               real environment values still win over file values.
#   NAME...     The variable names to copy. Names only — values are resolved
#               locally and passed process-to-process.
#
# Add a host: extend the case statement at the bottom with one function.

set -euo pipefail

HOST="" TARGET="" ENV_FILE=""
NAMES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)     HOST="$2"; shift 2 ;;
    --target)   TARGET="$2"; shift 2 ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    -h|--help)  grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)          NAMES+=("$1"); shift ;;
  esac
done

[[ -n "$HOST" && -n "$TARGET" && ${#NAMES[@]} -gt 0 ]] || {
  echo "usage: copy-env-vars.sh --host <host> --target <target> [--env-file f] NAME..." >&2
  exit 1
}

# Load the env file first (if given), then re-apply the real environment on
# top so live values win.
if [[ -n "$ENV_FILE" ]]; then
  set -a; # shellcheck disable=SC1090
  source "$ENV_FILE"; set +a
fi

resolve() { # name -> value or unset marker
  local name="$1"
  if [[ -n "${!name+x}" ]]; then printf '%s' "${!name}"; else return 1; fi
}

set_railway() { # name value
  railway variable set --skip-deploys --service "$TARGET" "$1=$2" >/dev/null
}

# aws: upsert KEY=value into /workspaces/env/agent.env over ssh. The value
# travels on the ssh stdin, never in argv or the remote shell's history.
AWS_ENV_FILE="${AWS_ENV_FILE:-/workspaces/env/agent.env}"
set_aws() { # name value
  printf '%s=%s\n' "$1" "$2" | ssh "$TARGET" "f=$AWS_ENV_FILE; k=$1; \
    tmp=\$(mktemp); grep -v \"^\$k=\" \"\$f\" > \"\$tmp\" 2>/dev/null || true; \
    cat >> \"\$tmp\"; install -m 600 \"\$tmp\" \"\$f\"; rm -f \"\$tmp\""
}

copied=() missing=()
for name in "${NAMES[@]}"; do
  if value=$(resolve "$name"); then
    case "$HOST" in
      railway) set_railway "$name" "$value" ;;
      aws)     set_aws "$name" "$value" ;;
      *) echo "unknown host: $HOST" >&2; exit 1 ;;
    esac
    copied+=("$name")
  else
    missing+=("$name")
  fi
done

echo "copied:  ${copied[*]:-none}"
[[ ${#missing[@]} -gt 0 ]] && echo "missing locally (skipped): ${missing[*]}" >&2

case "$HOST" in
  railway)
    echo "note: vars were staged with --skip-deploys — redeploy the service to apply." ;;
  aws)
    echo "note: written to $AWS_ENV_FILE on $TARGET — run: ssh $TARGET 'sudo systemctl restart agent.service'" ;;
esac
