#!/usr/bin/env bash
# Explicit disposable context + local Git source; never upgrades Flux-owned releases.
set -euo pipefail
MEDW_PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -x "$MEDW_PROJECT_ROOT/.venv/bin/python" ]]; then
  exec "$MEDW_PROJECT_ROOT/.venv/bin/python" "$MEDW_PROJECT_ROOT/scripts/local_deploy.py" "$@"
fi
exec python3 "$MEDW_PROJECT_ROOT/scripts/local_deploy.py" "$@"
