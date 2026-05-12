#!/usr/bin/env sh
set -eu
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$APP_DIR"
if command -v python3 >/dev/null 2>&1; then
  exec python3 -B portable/launch.py "$@"
elif command -v python >/dev/null 2>&1; then
  exec python -B portable/launch.py "$@"
else
  echo "Python 3.9 or newer is required." >&2
  exit 1
fi
