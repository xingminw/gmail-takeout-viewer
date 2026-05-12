#!/usr/bin/env python3
"""Cross-platform portable launcher for Gmail Takeout Viewer.

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


def default_data_dir(root):
    env_dir = os.environ.get("GMAIL_VIEWER_DATA_DIR")
    if env_dir:
        return Path(env_dir), "environment"

    candidates = [
        root,
        root / "data",
        root / "archive",
        root / "mail-data",
        root.parent / "data",
        root.parent / "MailArchive" / "data",
    ]
    for candidate in candidates:
        if (candidate / "gmail_index.sqlite").exists():
            return candidate, "auto"
    return root, "default"


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Launch the portable local mail archive viewer.")
    detected_data_dir, source = default_data_dir(root)
    parser.add_argument("--data-dir", default=str(detected_data_dir), help="Directory containing gmail_index.sqlite; relative paths resolve from the app folder.")
    parser.add_argument("--port", default=os.environ.get("GMAIL_VIEWER_PORT", ""), help="Optional fixed localhost port; default chooses a free port.")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = (root / data_dir).resolve()
    db_path = data_dir / "gmail_index.sqlite"
    if not db_path.exists():
        tried = [
            root / "gmail_index.sqlite",
            root / "data" / "gmail_index.sqlite",
            root / "archive" / "gmail_index.sqlite",
            root / "mail-data" / "gmail_index.sqlite",
            root.parent / "data" / "gmail_index.sqlite",
            root.parent / "MailArchive" / "data" / "gmail_index.sqlite",
        ]
        tried_text = "\n".join(f"  - {path}" for path in tried)
        raise SystemExit(
            f"Missing database: {db_path}\n\n"
            "Import an MBOX first, copy an existing archive data folder next to this app, "
            "or pass --data-dir pointing at a folder that contains gmail_index.sqlite.\n\n"
            "Auto-detected data source: "
            f"{source}\n"
            "Checked common locations:\n"
            f"{tried_text}"
        )

    os.environ["GMAIL_VIEWER_DATA_DIR"] = str(data_dir)
    if args.port:
        os.environ["GMAIL_VIEWER_PORT"] = str(args.port)
    os.chdir(root)
    sys.path.insert(0, str(root))
    runpy.run_path(str(root / "app.py"), run_name="__main__")


if __name__ == "__main__":
    main()
