import http.client
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
EXAMPLE_MBOX = REPO / "examples" / "sample_10.mbox"
CHECKED_IN_SAMPLE_ARCHIVE = REPO / "examples" / "sample_archive"
TEMPLATE_DIR = REPO / "tools" / "archive_templates"


class BuildArchiveEndToEndTests(unittest.TestCase):
    def test_checked_in_sample_archive_matches_example_mbox(self):
        expected_files = [
            ".mail-archive-builder.json",
            "Start Mail Viewer.command",
            "Start Mail Viewer.sh",
            "Start Mail Viewer.bat",
            "app/app.py",
            "app/portable_launch.py",
            "data/gmail_index.sqlite",
            "data/reports/import_summary.json",
            "source/sample_10.mbox",
            "logs/import.log",
        ]
        for rel_path in expected_files:
            self.assertTrue((CHECKED_IN_SAMPLE_ARCHIVE / rel_path).exists(), rel_path)

        marker = json.loads((CHECKED_IN_SAMPLE_ARCHIVE / ".mail-archive-builder.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["generated_by"], "gmail-takeout-archive-builder")
        self.assertEqual(marker["source_mbox"], "source/sample_10.mbox")

        db_path = CHECKED_IN_SAMPLE_ARCHIVE / "data" / "gmail_index.sqlite"
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 10)
            paths = {row[0] for row in conn.execute("SELECT DISTINCT mbox_path FROM messages")}
            self.assertEqual(paths, {"source/sample_10.mbox"})
        finally:
            conn.close()

        summary_path = CHECKED_IN_SAMPLE_ARCHIVE / "data" / "reports" / "import_summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertEqual(summary["mbox"], "source/sample_10.mbox")
        self.assertEqual(summary["out_dir"], "data")
        self.assertEqual(summary["db"], "data/gmail_index.sqlite")
        self.assertNotIn(str(REPO), summary_path.read_text(encoding="utf-8"))
        self.assertNotIn(str(REPO), (CHECKED_IN_SAMPLE_ARCHIVE / "logs" / "import.log").read_text(encoding="utf-8"))

        expected_copies = [
            (REPO / "viewer" / "app.py", CHECKED_IN_SAMPLE_ARCHIVE / "app" / "app.py"),
            (REPO / "viewer" / "import_mbox.py", CHECKED_IN_SAMPLE_ARCHIVE / "app" / "import_mbox.py"),
            (REPO / "viewer" / "analyze_mbox_stats.py", CHECKED_IN_SAMPLE_ARCHIVE / "app" / "analyze_mbox_stats.py"),
            (TEMPLATE_DIR / "app" / "portable_launch.py", CHECKED_IN_SAMPLE_ARCHIVE / "app" / "portable_launch.py"),
            (TEMPLATE_DIR / "Start Mail Viewer.command", CHECKED_IN_SAMPLE_ARCHIVE / "Start Mail Viewer.command"),
            (TEMPLATE_DIR / "Start Mail Viewer.sh", CHECKED_IN_SAMPLE_ARCHIVE / "Start Mail Viewer.sh"),
            (TEMPLATE_DIR / "Start Mail Viewer.bat", CHECKED_IN_SAMPLE_ARCHIVE / "Start Mail Viewer.bat"),
        ]
        for source, copied in expected_copies:
            self.assertEqual(source.read_text(encoding="utf-8"), copied.read_text(encoding="utf-8"), copied)

    def test_builds_standalone_archive_from_mbox(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "MailArchive"

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO / "tools" / "build_archive.py"),
                    str(EXAMPLE_MBOX),
                    "--out",
                    str(out),
                    "--rebuild",
                    "--limit",
                    "10",
                ],
                cwd=str(REPO),
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

            expected_files = [
                ".mail-archive-builder.json",
                "Start Mail Viewer.command",
                "Start Mail Viewer.sh",
                "Start Mail Viewer.bat",
                "app/app.py",
                "app/import_mbox.py",
                "app/analyze_mbox_stats.py",
                "app/portable_launch.py",
                "data/gmail_index.sqlite",
                "data/reports/import_summary.json",
                "source/sample_10.mbox",
                "logs/import.log",
            ]
            for rel_path in expected_files:
                self.assertTrue((out / rel_path).exists(), rel_path)

            self.assertFalse((out / ".git").exists())
            self.assertFalse((out / "app" / ".git").exists())

            conn = sqlite3.connect(out / "data" / "gmail_index.sqlite")
            try:
                self.assertEqual(conn.execute("SELECT count(*) FROM messages").fetchone()[0], 10)
                paths = {row[0] for row in conn.execute("SELECT DISTINCT mbox_path FROM messages")}
                self.assertEqual(paths, {"source/sample_10.mbox"})
            finally:
                conn.close()

            summary_path = out / "data" / "reports" / "import_summary.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["mbox"], "source/sample_10.mbox")
            self.assertEqual(summary["out_dir"], "data")
            self.assertEqual(summary["db"], "data/gmail_index.sqlite")
            self.assertNotIn(str(out), summary_path.read_text(encoding="utf-8"))
            self.assertNotIn(str(out), (out / "logs" / "import.log").read_text(encoding="utf-8"))

            spec = importlib.util.spec_from_file_location("built_archive_app", out / "app" / "app.py")
            app = importlib.util.module_from_spec(spec)
            self.assertIsNotNone(spec.loader)
            spec.loader.exec_module(app)
            app.DATA_DIR = (out / "data").resolve()
            app.DB_PATH = app.DATA_DIR / "gmail_index.sqlite"
            app.READONLY_DB = True

            server = app.ThreadingHTTPServer((app.HOST, 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn_http = http.client.HTTPConnection(app.HOST, server.server_address[1], timeout=5)
                try:
                    conn_http.request("GET", "/")
                    response = conn_http.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"Gmail Takeout Viewer", response.read())

                    conn_http.request("GET", "/api/conversations")
                    response = conn_http.getresponse()
                    self.assertEqual(response.status, 200)
                    body = response.read()
                    self.assertIn(b"Fixture message 10", body)
                    self.assertIn(b'"total": 10', body)
                finally:
                    conn_http.close()
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_rebuild_refuses_to_delete_unmarked_directory(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "important"
            out.mkdir()
            keep = out / "keep.txt"
            keep.write_text("do not delete", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO / "tools" / "build_archive.py"),
                    str(EXAMPLE_MBOX),
                    "--out",
                    str(out),
                    "--rebuild",
                    "--limit",
                    "1",
                ],
                cwd=str(REPO),
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(keep.exists())
            self.assertIn(".mail-archive-builder.json", result.stderr + result.stdout)

    def test_rebuild_refuses_input_mbox_inside_output_directory(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "MailArchive"
            first = subprocess.run(
                [
                    sys.executable,
                    str(REPO / "tools" / "build_archive.py"),
                    str(EXAMPLE_MBOX),
                    "--out",
                    str(out),
                    "--limit",
                    "1",
                ],
                cwd=str(REPO),
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(first.returncode, 0, first.stderr + first.stdout)

            archived_mbox = out / "source" / "sample_10.mbox"
            self.assertTrue(archived_mbox.exists())

            second = subprocess.run(
                [
                    sys.executable,
                    str(REPO / "tools" / "build_archive.py"),
                    str(archived_mbox),
                    "--out",
                    str(out),
                    "--rebuild",
                    "--limit",
                    "1",
                ],
                cwd=str(REPO),
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertNotEqual(second.returncode, 0)
            self.assertTrue(archived_mbox.exists())
            self.assertIn("outside the output archive", second.stderr + second.stdout)


if __name__ == "__main__":
    unittest.main()
