#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Compatibility entry point. New automation should call manage.sh transfer-ownership.
if [[ $# -eq 0 ]]; then
  exec "$SCRIPT_DIR/manage.sh" interactive transfer-ownership
fi
exec "$SCRIPT_DIR/manage.sh" transfer-ownership "$@"
