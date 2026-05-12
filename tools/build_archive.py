#!/usr/bin/env python3
"""Build a standalone local mail archive from a Gmail Takeout MBOX."""
import argparse
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
VIEWER_DIR = REPO / "viewer"
TEMPLATE_DIR = Path(__file__).resolve().parent / "archive_templates"
MARKER_FILE = ".mail-archive-builder.json"
APP_FILES = [
    "app.py",
    "import_mbox.py",
    "analyze_mbox_stats.py",
]


def copy_template(src_rel, dest):
    src = TEMPLATE_DIR / src_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def copy_app(out):
    app_dir = out / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    for rel_path in APP_FILES:
        shutil.copy2(VIEWER_DIR / rel_path, app_dir / Path(rel_path).name)
    copy_template(Path("app") / "portable_launch.py", app_dir / "portable_launch.py")


def write_launchers(out):
    copy_template("Start Mail Viewer.command", out / "Start Mail Viewer.command")
    copy_template("Start Mail Viewer.sh", out / "Start Mail Viewer.sh")
    copy_template("Start Mail Viewer.bat", out / "Start Mail Viewer.bat")
    for rel_path in ("Start Mail Viewer.command", "Start Mail Viewer.sh", "app/portable_launch.py"):
        path = out / rel_path
        path.chmod(path.stat().st_mode | 0o111)


def write_marker(out, source_mbox):
    marker = {
        "generated_by": "gmail-takeout-archive-builder",
        "format_version": 1,
        "source_mbox": source_mbox.as_posix(),
    }
    (out / MARKER_FILE).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")


def ensure_rebuild_safe(out):
    marker = out / MARKER_FILE
    if not marker.exists():
        raise SystemExit(
            f"Refusing to replace unmarked directory: {out}\n"
            f"Only directories containing {MARKER_FILE} can be replaced with --rebuild."
        )
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Refusing to replace directory with invalid {MARKER_FILE}: {out}") from exc
    if data.get("generated_by") != "gmail-takeout-archive-builder":
        raise SystemExit(f"Refusing to replace directory with unrecognized {MARKER_FILE}: {out}")


def ensure_input_outside_output(mbox, out):
    try:
        mbox.relative_to(out)
    except ValueError:
        return
    raise SystemExit(
        f"Refusing to rebuild {out} from an MBOX inside that same output archive: {mbox}\n"
        "Use an input MBOX outside the output archive."
    )


def run_import(source_mbox, out, args):
    logs_dir = out / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    source_mbox_rel = Path("source") / source_mbox.name
    cmd = [
        sys.executable,
        str(out / "app" / "import_mbox.py"),
        source_mbox_rel.as_posix(),
        "--out-dir",
        "data",
        "--storage",
        args.storage,
        "--progress",
        str(args.progress),
        "--commit-every",
        str(args.commit_every),
    ]
    if args.rebuild:
        cmd.append("--rebuild")
    if args.limit:
        cmd.extend(["--limit", str(args.limit)])

    result = subprocess.run(cmd, cwd=str(out), text=True, capture_output=True)
    log_text = ""
    if result.stdout:
        log_text += result.stdout
    if result.stderr:
        log_text += result.stderr
    (logs_dir / "import.log").write_text(log_text, encoding="utf-8")
    if result.returncode != 0:
        raise SystemExit(log_text or f"Import failed with exit code {result.returncode}")


def build_viewer_indexes(out):
    app_path = out / "app" / "app.py"
    spec = importlib.util.spec_from_file_location("archive_builder_app", app_path)
    app = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise SystemExit(f"Could not load generated app: {app_path}")
    spec.loader.exec_module(app)
    app.DATA_DIR = (out / "data").resolve()
    app.DB_PATH = app.DATA_DIR / "gmail_index.sqlite"
    app.ensure_performance_schema()
    conn = sqlite3.connect(app.DB_PATH)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(app.DB_PATH) + suffix)
        if sidecar.exists():
            sidecar.unlink()


def remove_tree(path):
    def on_error(func, target, exc_info):
        if issubclass(exc_info[0], FileNotFoundError):
            return
        raise exc_info[1]

    shutil.rmtree(path, onerror=on_error)


def build_archive(args):
    mbox = args.mbox.resolve()
    out = args.out.resolve()
    if not mbox.exists():
        raise SystemExit(f"MBOX not found: {mbox}")
    if out.exists():
        if not args.rebuild:
            raise SystemExit(f"Output already exists. Use --rebuild to replace: {out}")
        ensure_rebuild_safe(out)
        ensure_input_outside_output(mbox, out)
        remove_tree(out)

    (out / "source").mkdir(parents=True)
    source_mbox = out / "source" / mbox.name
    shutil.copy2(mbox, source_mbox)
    write_marker(out, Path("source") / mbox.name)
    copy_app(out)
    write_launchers(out)
    run_import(source_mbox, out, args)
    build_viewer_indexes(out)
    print(f"archive={out}")
    print(f"source={source_mbox}")
    print(f"data={out / 'data'}")
    print(f"launcher={out / 'Start Mail Viewer.command'}")


def main():
    parser = argparse.ArgumentParser(description="Build a standalone MailArchive folder from a Gmail Takeout MBOX.")
    parser.add_argument("mbox", type=Path, help="Input Gmail Takeout .mbox file.")
    parser.add_argument("--out", type=Path, required=True, help="Output archive folder to create.")
    parser.add_argument("--rebuild", action="store_true", help="Replace the output folder if it already exists.")
    parser.add_argument("--limit", type=int, default=0, help="Import only the first N messages; useful for tests.")
    parser.add_argument("--storage", choices=("compact", "legacy"), default="compact")
    parser.add_argument("--progress", type=int, default=0)
    parser.add_argument("--commit-every", type=int, default=500)
    args = parser.parse_args()
    build_archive(args)


if __name__ == "__main__":
    main()
