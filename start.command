#!/bin/sh
set -eu

cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  python3 -B app.py
elif command -v python >/dev/null 2>&1; then
  python -B app.py
else
  echo "Python 3.9 or newer is required."
  echo "Install it from https://www.python.org/downloads/macos/ or with Homebrew:"
  echo "  brew install python"
  read -r -p "Press Enter to close this window..."
  exit 1
fi
