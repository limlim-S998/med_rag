#!/usr/bin/env bash
# One configuration controls Azure setup. Preflight runs before paid creation.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
exec "${MEDW_PYTHON:-$ROOT/.venv/bin/python}" "$ROOT/scripts/azure.py" up \
  --config "${AZURE_CONFIG:-$ROOT/data/azure/config.json}" "$@"
