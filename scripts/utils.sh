#!/usr/bin/env bash
run_postgres_cli() {
  if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'ERROR: Python 3.9 or newer is required.' >&2
    return 1
  fi
  local utils_dir
  utils_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
  exec python3 "$utils_dir/lib/postgres_cli.py" "$@"
}
