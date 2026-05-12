#!/bin/sh
set -u
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$APP_DIR"

pause_on_error() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo
    echo "Gmail Takeout Viewer did not start."
    echo "Check that the data folder contains gmail_index.sqlite, or pass --data-dir."
    echo
    printf "Press Enter to close this window..."
    read _answer
  fi
  exit "$status"
}
trap pause_on_error EXIT

if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  echo "Python 3.9 or newer is required. Install it from https://www.python.org/downloads/macos/."
  exit 1
fi

"$PYTHON" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY
if [ "$?" -ne 0 ]; then
  echo "Python 3.9 or newer is required."
  echo "Current Python is too old: $($PYTHON --version 2>&1)"
  exit 1
fi

"$PYTHON" -B portable/launch.py "$@"
