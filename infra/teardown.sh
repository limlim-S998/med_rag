#!/usr/bin/env bash
# Delete only the resources recorded in the deployment ownership journal.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
exec "${MEDW_PYTHON:-$ROOT/.venv/bin/python}" "$ROOT/scripts/azure.py" down \
  --config "${AZURE_CONFIG:-$ROOT/data/azure/config.json}" "$@"
