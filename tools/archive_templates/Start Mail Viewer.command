#!/bin/zsh
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
export GMAIL_VIEWER_READONLY=1
if command -v python3 >/dev/null 2>&1; then
  exec python3 -B app/portable_launch.py "$@"
else
  exec python -B app/portable_launch.py "$@"
fi
