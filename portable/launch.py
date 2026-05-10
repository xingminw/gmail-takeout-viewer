#!/usr/bin/env python3
"""Cross-platform portable launcher for Mail Backup Local Viewer.

Place this repository next to a data directory, or pass/define one:
  python portable/launch.py --data-dir ./data
  GMAIL_VIEWER_DATA_DIR=/path/to/data python portable/launch.py

The data directory must contain gmail_index.sqlite. Paths are resolved relative
to the repository root so the folder can be moved between machines/drives.
"""
import argparse
import os
import runpy
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Launch the portable local mail archive viewer.")
    parser.add_argument("--data-dir", default=os.environ.get("GMAIL_VIEWER_DATA_DIR") or str(root), help="Directory containing gmail_index.sqlite; relative paths resolve from the app folder.")
    parser.add_argument("--port", default=os.environ.get("GMAIL_VIEWER_PORT", ""), help="Optional fixed localhost port; default chooses a free port.")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = (root / data_dir).resolve()
    db_path = data_dir / "gmail_index.sqlite"
    if not db_path.exists():
        raise SystemExit(f"Missing database: {db_path}\nImport an MBOX first or pass --data-dir pointing at an archive data folder.")

    os.environ["GMAIL_VIEWER_DATA_DIR"] = str(data_dir)
    if args.port:
        os.environ["GMAIL_VIEWER_PORT"] = str(args.port)
    os.chdir(root)
    sys.path.insert(0, str(root))
    runpy.run_path(str(root / "app.py"), run_name="__main__")


if __name__ == "__main__":
    main()
