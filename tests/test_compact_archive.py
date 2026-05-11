import http.client
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from email.message import EmailMessage
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class CompactArchiveTests(unittest.TestCase):
    def make_mbox(self, path):
        msg = EmailMessage()
        msg["From"] = "Sender <sender@example.com>"
        msg["To"] = "Recipient <recipient@example.com>"
        msg["Subject"] = "Compact archive test"
        msg["Date"] = "Fri, 01 Mar 2024 12:34:56 +0000"
        msg["Message-ID"] = "<compact-test@example.com>"
        msg["X-Gmail-Labels"] = "Inbox,Test"
        msg.set_content("Plain body text")
        msg.add_alternative("<html><body><h1>Hello compact</h1><p>HTML body</p></body></html>", subtype="html")
        msg.add_attachment(b"attachment bytes", maintype="text", subtype="plain", filename="note.txt")
        with path.open("wb") as f:
            f.write(b"From sender@example.com Fri Mar 01 12:34:56 2024\n")
            f.write(msg.as_bytes())
            f.write(b"\n")

    def test_compact_import_and_app_endpoints(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mbox = td / "sample.mbox"
            out = td / "archive"
            self.make_mbox(mbox)

            result = subprocess.run(
                [sys.executable, str(REPO / "import_mbox.py"), str(mbox), "--out-dir", str(out), "--rebuild", "--storage", "compact"],
                cwd=str(REPO), text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertFalse((out / "messages" / "000001").exists())
            db_path = out / "gmail_index.sqlite"
            self.assertTrue(db_path.exists())

            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute("SELECT body_html, body_html_path, raw_eml_path, mbox_offset, mbox_length, storage_mode FROM messages WHERE id=1").fetchone()
                self.assertIsNotNone(row)
                body_html, body_path, raw_path, offset, length, mode = row
                self.assertIn("Hello compact", body_html)
                self.assertEqual(body_path, "")
                self.assertEqual(raw_path, "")
                self.assertIsInstance(offset, int)
                self.assertGreater(length, 0)
                self.assertEqual(mode, "compact")
                att = conn.execute("SELECT path, sha256, size_bytes FROM attachments WHERE message_id=1").fetchone()
                self.assertIsNotNone(att)
                rel_path, sha256, size = att
                self.assertTrue(sha256)
                self.assertEqual(size, len(b"attachment bytes"))
                self.assertTrue((out / rel_path).exists())
                self.assertIn("blobs", rel_path.replace("\\", "/"))
            finally:
                conn.close()

            env = os.environ.copy()
            env["GMAIL_VIEWER_DATA_DIR"] = str(out)
            env["GMAIL_VIEWER_PORT"] = "0"
            sys.path.insert(0, str(REPO))
            import app
            app.DATA_DIR = out.resolve()
            app.DB_PATH = app.DATA_DIR / "gmail_index.sqlite"
            app.ensure_performance_schema()
            server = app.ThreadingHTTPServer((app.HOST, 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                http_conn = http.client.HTTPConnection(app.HOST, port, timeout=5)
                try:
                    http_conn.request("GET", "/api/message/1")
                    resp = http_conn.getresponse()
                    self.assertEqual(resp.status, 200)
                    self.assertIn(b"Compact archive test", resp.read())
                    http_conn.request("GET", "/body/1")
                    resp = http_conn.getresponse()
                    self.assertEqual(resp.status, 200)
                    self.assertIn(b"Hello compact", resp.read())
                    http_conn.request("GET", "/file/" + rel_path)
                    resp = http_conn.getresponse()
                    self.assertEqual(resp.status, 200)
                    self.assertEqual(resp.read(), b"attachment bytes")
                finally:
                    http_conn.close()
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
