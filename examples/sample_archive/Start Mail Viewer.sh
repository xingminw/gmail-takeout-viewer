#!/usr/bin/env sh
set -eu
ARCHIVE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ARCHIVE_DIR"
export GMAIL_VIEWER_READONLY=1
if command -v python3 >/dev/null 2>&1; then
  exec python3 -B app/portable_launch.py "$@"
elif command -v python >/dev/null 2>&1; then
  exec python -B app/portable_launch.py "$@"
else
  echo "Python 3.9 or newer is required." >&2
  exit 1
fi
