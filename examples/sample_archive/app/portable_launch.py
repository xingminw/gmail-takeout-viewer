#!/usr/bin/env python3
"""Launch a standalone MailArchive folder."""
import argparse
import os
import runpy
import sys
from pathlib import Path


def main():
    app_dir = Path(__file__).resolve().parent
    archive_root = app_dir.parent
    default_data_dir = os.environ.get("GMAIL_VIEWER_DATA_DIR") or str(archive_root / "data")
    parser = argparse.ArgumentParser(description="Launch the portable local mail archive viewer.")
    parser.add_argument("--data-dir", default=default_data_dir, help="Directory containing gmail_index.sqlite; relative paths resolve from the archive root.")
    parser.add_argument("--port", default=os.environ.get("GMAIL_VIEWER_PORT", ""), help="Optional fixed localhost port; default chooses a free port.")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = (archive_root / data_dir).resolve()
    db_path = data_dir / "gmail_index.sqlite"
    if not db_path.exists():
        raise SystemExit(f"Missing database: {db_path}\nImport an MBOX first or pass --data-dir pointing at an archive data folder.")

    os.environ["GMAIL_VIEWER_DATA_DIR"] = str(data_dir)
    os.environ.setdefault("GMAIL_VIEWER_READONLY", "1")
    if args.port:
        os.environ["GMAIL_VIEWER_PORT"] = str(args.port)
    os.chdir(app_dir)
    sys.path.insert(0, str(app_dir))
    runpy.run_path(str(app_dir / "app.py"), run_name="__main__")


if __name__ == "__main__":
    main()
