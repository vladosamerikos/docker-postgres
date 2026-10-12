#!/usr/bin/env bash
REPO_URL='https://github.com/vladosamerikos/docker-postgres.git'
set -euo pipefail

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: ./setup.sh [--version 10..18] [--directory PATH] [--no-color]
       curl -fsSL https://raw.githubusercontent.com/vladosamerikos/docker-postgres/main/setup.sh | bash

Reuse a local PostgreSQL template or clone it into ./postgresVERSION.
Existing installations keep their PostgreSQL version and administrator credentials.
POSTGRES_SETUP_REPO_URL overrides the repository URL.
EOF
}

VERSION=''
DIRECTORY=''
NO_COLOR_OPTION=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --version|--directory)
      [[ $# -ge 2 ]] || die "$1 requires a value."
      if [[ "$1" == --version ]]; then VERSION="$2"; else DIRECTORY="$2"; fi
      shift 2 ;;
    --no-color) NO_COLOR_OPTION=1; export NO_COLOR=1; shift ;;
    *) die "Unknown argument: $1" ;;
  esac
done
if [[ -n "$VERSION" && ! "$VERSION" =~ ^(1[0-8])$ ]]; then
  die 'Choose a PostgreSQL major version from 10 to 18.'
fi
command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die 'Python 3.9 or newer is required.'

is_template() {
  [[ -f "$1/docker-compose.yml" && -f "$1/scripts/lib/postgres_cli.py" && -f "$1/scripts/lib/setup_cli.py" ]]
}

ROOT=''
if [[ -n "$DIRECTORY" ]]; then
  is_template "$DIRECTORY" || die '--directory must point to a compatible local PostgreSQL template.'
  ROOT="$(cd -- "$DIRECTORY" && pwd)"
elif [[ -f "${BASH_SOURCE[0]:-}" ]]; then
  SCRIPT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
  if is_template "$SCRIPT_ROOT"; then ROOT="$SCRIPT_ROOT"; fi
fi
if [[ -z "$ROOT" ]]; then
  if is_template "$PWD"; then ROOT="$PWD";
  elif [[ "$(basename -- "$PWD")" == scripts ]] && is_template "$PWD/.."; then ROOT="$(cd .. && pwd)";
  fi
fi

STAGING=''
cleanup() { if [[ -n "$STAGING" && -d "$STAGING" ]]; then rm -rf -- "$STAGING"; fi; }
trap cleanup EXIT
trap 'exit 130' INT TERM

if [[ -z "$ROOT" ]]; then
  for dependency in git openssl docker; do
    command -v "$dependency" >/dev/null 2>&1 || die "$dependency is required."
  done
  docker compose version >/dev/null 2>&1 || die 'Docker Compose is required.'
  docker info >/dev/null 2>&1 || die 'Docker Engine is unavailable.'
  if ! { exec 3<>/dev/tty; } 2>/dev/null; then
    die 'Setup requires an interactive terminal.'
  fi
  if [[ -z "$VERSION" ]]; then
    while true; do
      printf 'PostgreSQL major version (10-18) [18]: ' >&3
      IFS= read -r VERSION <&3 || exit 130
      VERSION="${VERSION:-18}"
      [[ "$VERSION" =~ ^(1[0-8])$ ]] && break
      printf '%s\n' 'Choose a version between 10 and 18.' >&3
    done
  fi
  ROOT="$PWD/postgres$VERSION"
  if [[ -e "$ROOT" ]]; then
    is_template "$ROOT" || die "$ROOT already exists and is not a compatible template; nothing was overwritten."
    printf 'INFO: Reusing %s\n' "$ROOT" >&2
  else
    STAGING="$(mktemp -d "$PWD/.postgres-bootstrap.XXXXXXXX")"
    printf 'INFO: Cloning PostgreSQL template into %s\n' "$ROOT" >&2
    git clone -- "${POSTGRES_SETUP_REPO_URL:-$REPO_URL}" "$STAGING/repo"
    is_template "$STAGING/repo" || die 'The repository is missing the PostgreSQL template/setup helper.'
    # Do not replace a directory created by another setup while git was cloning.
    python3 - "$STAGING/repo" "$ROOT" <<'PY'
import os, sys
if os.path.lexists(sys.argv[2]):
    sys.exit('ERROR: Destination appeared during cloning; nothing was overwritten.')
os.rename(sys.argv[1], sys.argv[2])
PY
    cleanup
    STAGING=''
  fi
  exec 3>&-
fi

ARGS=(--root "$ROOT")
[[ -z "$VERSION" ]] || ARGS+=(--version "$VERSION")
[[ -z "$NO_COLOR_OPTION" ]] || ARGS+=(--no-color)
exec python3 "$ROOT/scripts/lib/setup_cli.py" "${ARGS[@]}"
