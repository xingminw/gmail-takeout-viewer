import argparse
import html
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import traceback
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path


def clean(value):
    if value is None:
        return ""
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def safe_name(value, fallback, max_length=120):
    value = value or fallback
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:max_length].rstrip(" .") or fallback


def parse_date(value):
    value = clean(value)
    if not value:
        return "", ""
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.isoformat(sep=" ", timespec="seconds"), str(dt.year)
    except Exception:
        match = re.search(r"\b(19|20)\d{2}\b", value)
        return value, match.group(0) if match else ""


def parse_from(value):
    parsed = getaddresses([clean(value)])
    if not parsed:
        return "", "", "", ""
    name, addr = parsed[0]
    addr = addr.lower().strip()
    domain = addr.split("@", 1)[1] if "@" in addr else ""
    display = f"{name} <{addr}>" if name and addr else name or addr
    return clean(name), addr, domain, display


def normalize_subject(value):
    subject = clean(value).lower()
    subject = re.sub(r"^\s*((re|fw|fwd)\s*:\s*)+", "", subject)
    subject = re.sub(r"\s+", " ", subject).strip()
    return subject


def thread_key(msg):
    refs = clean(msg.get("References"))
    in_reply_to = clean(msg.get("In-Reply-To"))
    if refs:
        return refs.split()[0]
    if in_reply_to:
        return in_reply_to.split()[0]
    normalized = normalize_subject(msg.get("Subject"))
    sender_domain = parse_from(msg.get("From"))[2]
    return f"subject:{sender_domain}:{normalized}"


def iter_mbox_messages(path, limit=0, skip_through=0, only_indexes=None):
    current = bytearray()
    current_from = b""
    count = 0
    bytes_seen = 0
    current_start = 0
    with path.open("rb") as handle:
        for line in handle:
            bytes_seen += len(line)
            if line.startswith(b"From "):
                if current:
                    count += 1
                    if count > skip_through and (only_indexes is None or count in only_indexes):
                        yield count, bytes(current), current_from, current_start, len(current)
                    if limit and count >= limit:
                        return
                    current = bytearray()
                current_from = line
                current_start = bytes_seen
                continue
            current.extend(line)
        if current and (not limit or count < limit):
            count += 1
            if count > skip_through and (only_indexes is None or count in only_indexes):
                yield count, bytes(current), current_from, current_start, len(current)


def part_payload(part):
    try:
        return part.get_payload(decode=True) or b""
    except Exception:
        return b""


def decode_text_part(part):
    payload = part_payload(part)
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def strip_scripts(html_text):
    html_text = re.sub(r"(?is)<script\b.*?</script>", "", html_text)
    html_text = re.sub(r"(?is)<iframe\b.*?</iframe>", "", html_text)
    html_text = re.sub(r"\son\w+\s*=\s*(['\"]).*?\1", "", html_text)
    html_text = re.sub(r"(?i)javascript:", "", html_text)
    return html_text


def html_to_text(html_text):
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html_text)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p\s*>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", text).strip()


def split_labels(labels):
    return [label.strip() for label in (labels or "").split(",") if label.strip()]


def message_users(from_email, to_text):
    emails = set()
    from_email = (from_email or "").strip().lower()
    if from_email:
        emails.add(from_email)
    for _, address in getaddresses([to_text or ""]):
        address = address.strip().lower()
        if address:
            emails.add(address)
    return sorted(emails)


def init_db(conn):
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY,
          date TEXT,
          year TEXT,
          from_name TEXT,
          from_email TEXT,
          from_domain TEXT,
          from_display TEXT,
          to_text TEXT,
          subject TEXT,
          labels TEXT,
          message_id TEXT,
          size_bytes INTEGER,
          size_mb REAL,
          preview TEXT,
          body_text TEXT,
          body_html TEXT,
          body_html_path TEXT,
          raw_eml_path TEXT,
          mbox_path TEXT,
          mbox_offset INTEGER,
          mbox_length INTEGER,
          storage_mode TEXT,
          mbox_from_line TEXT,
          in_reply_to TEXT,
          references_text TEXT,
          thread_key TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_messages_thread_key ON messages(thread_key);
        CREATE INDEX IF NOT EXISTS idx_messages_thread_date ON messages(thread_key, date DESC, id DESC);
        CREATE TABLE IF NOT EXISTS attachments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          message_id INTEGER NOT NULL,
          filename TEXT,
          path TEXT,
          content_type TEXT,
          size_bytes INTEGER,
          size_mb REAL,
          sha256 TEXT,
          FOREIGN KEY(message_id) REFERENCES messages(id)
        );
        CREATE INDEX IF NOT EXISTS idx_attachments_message_id ON attachments(message_id);
        CREATE TABLE IF NOT EXISTS message_labels (
          label TEXT NOT NULL,
          message_id INTEGER NOT NULL,
          PRIMARY KEY(label, message_id),
          FOREIGN KEY(message_id) REFERENCES messages(id)
        );
        CREATE INDEX IF NOT EXISTS idx_message_labels_message_id ON message_labels(message_id);
        CREATE TABLE IF NOT EXISTS message_users (
          email TEXT NOT NULL,
          message_id INTEGER NOT NULL,
          PRIMARY KEY(email, message_id)
        );
        CREATE INDEX IF NOT EXISTS idx_message_users_message_id ON message_users(message_id);
        CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date);
        CREATE INDEX IF NOT EXISTS idx_messages_year ON messages(year);
        CREATE INDEX IF NOT EXISTS idx_messages_from_email ON messages(from_email);
        CREATE INDEX IF NOT EXISTS idx_messages_from_domain ON messages(from_domain);
        CREATE INDEX IF NOT EXISTS idx_messages_size ON messages(size_bytes);
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
          subject, from_email, from_display, to_text, labels, preview, body_text,
          content='messages', content_rowid='id'
        );
        """
    )
    ensure_column(conn, "messages", "body_html", "TEXT")
    ensure_column(conn, "messages", "mbox_path", "TEXT")
    ensure_column(conn, "messages", "mbox_offset", "INTEGER")
    ensure_column(conn, "messages", "mbox_length", "INTEGER")
    ensure_column(conn, "messages", "storage_mode", "TEXT")
    ensure_column(conn, "attachments", "sha256", "TEXT")


def ensure_column(conn, table, column, decl):
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def reset_output(out_dir, rebuild, resume, storage="compact"):
    db_path = out_dir / "gmail_index.sqlite"
    messages_dir = out_dir / "messages"
    blobs_dir = out_dir / "blobs"
    if db_path.exists() and not rebuild and not resume:
        raise SystemExit(f"Database already exists. Use --rebuild to replace or --resume to continue: {db_path}")
    if rebuild:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(db_path) + suffix)
            if candidate.exists():
                candidate.unlink()
        if messages_dir.exists():
            remove_tree(messages_dir)
        if storage == "compact" and blobs_dir.exists():
            remove_tree(blobs_dir)
    if storage == "legacy":
        messages_dir.mkdir(parents=True, exist_ok=True)
    else:
        blobs_dir.mkdir(parents=True, exist_ok=True)
    return db_path


def remove_tree(path):
    def on_error(func, item, exc_info):
        try:
            os.chmod(item, 0o700)
            func(item)
        except Exception:
            raise

    shutil.rmtree(path, onerror=on_error)


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def append_jsonl(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def db_scalar(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def max_imported_id(conn):
    return db_scalar(conn, "SELECT COALESCE(MAX(id), 0) FROM messages")


def validate_import(conn, out_dir):
    rows = conn.execute("SELECT id, body_html_path, raw_eml_path FROM messages ORDER BY id").fetchall()
    missing_body = []
    missing_raw = []
    for message_id, body_path, raw_path in rows:
        if body_path and not (out_dir / body_path).exists():
            missing_body.append(message_id)
        if raw_path and not (out_dir / raw_path).exists():
            missing_raw.append(message_id)
    return {
        "messages": len(rows),
        "attachments": db_scalar(conn, "SELECT count(*) FROM attachments"),
        "message_labels": db_scalar(conn, "SELECT count(*) FROM message_labels"),
        "fts_rows": db_scalar(conn, "SELECT count(*) FROM messages_fts"),
        "missing_body_html": missing_body[:50],
        "missing_body_html_count": len(missing_body),
        "missing_raw_eml": missing_raw[:50],
        "missing_raw_eml_count": len(missing_raw),
        "top_labels": [
            {"label": row[0], "count": row[1]}
            for row in conn.execute(
                "SELECT label, count(*) FROM message_labels GROUP BY label ORDER BY count(*) DESC LIMIT 20"
            )
        ],
        "top_domains": [
            {"domain": row[0], "count": row[1], "mb": round(row[2] or 0, 2)}
            for row in conn.execute(
                "SELECT from_domain, count(*), sum(size_bytes)/1024.0/1024.0 FROM messages WHERE from_domain <> '' GROUP BY from_domain ORDER BY sum(size_bytes) DESC LIMIT 20"
            )
        ],
        "largest_messages": [
            {"id": row[0], "subject": row[1], "from": row[2], "mb": row[3]}
            for row in conn.execute(
                "SELECT id, subject, from_email, size_mb FROM messages ORDER BY size_bytes DESC LIMIT 20"
            )
        ],
    }


def attachment_blob_path(out_dir, payload):
    sha = hashlib.sha256(payload).hexdigest()
    rel = Path("blobs") / sha[:2] / sha[2:4] / f"{sha}.blob"
    target = out_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(payload)
    return rel.as_posix(), sha


def insert_message(conn, index, raw_bytes, from_line, out_dir, storage="compact", mbox_path="", mbox_offset=None, mbox_length=None):
    msg = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    message_dir = out_dir / "messages" / f"{index:06d}"
    attachment_dir = message_dir / "attachments"
    if storage == "legacy":
        attachment_dir.mkdir(parents=True, exist_ok=True)
        raw_path = message_dir / "raw.eml"
        raw_path.write_bytes(raw_bytes)
    else:
        raw_path = None

    text_parts = []
    html_parts = []
    attachments = []

    for part in msg.walk():
        if part.is_multipart():
            continue
        content_type = clean(part.get_content_type()).lower()
        disposition = clean(part.get_content_disposition()).lower()
        filename = part.get_filename()
        is_attachment = bool(filename) or disposition == "attachment"

        if is_attachment:
            payload = part_payload(part)
            rel_blob_path, sha = attachment_blob_path(out_dir, payload)
            if not filename:
                ext = content_type.split("/", 1)[-1] if "/" in content_type else "bin"
                filename = f"attachment-{len(attachments) + 1}.{ext}"
            safe = safe_name(filename, f"attachment-{len(attachments) + 1}.bin")
            if "." not in Path(safe).name and "/" in content_type:
                ext = content_type.split("/", 1)[1].split(";", 1)[0]
                safe = f"{safe}.{'jpg' if ext == 'jpeg' else ext}"
            safe = safe_name(safe, f"attachment-{len(attachments) + 1}.bin")
            if storage == "legacy":
                target = attachment_dir / safe
                if target.exists():
                    target.unlink()
                target.write_bytes(payload)
                rel_path = target.relative_to(out_dir).as_posix()
            else:
                rel_path = rel_blob_path
            attachments.append((filename, rel_path, content_type, len(payload), sha))
            continue

        if content_type == "text/plain":
            text_parts.append(decode_text_part(part))
        elif content_type == "text/html":
            html_parts.append(strip_scripts(decode_text_part(part)))

    body_text = "\n\n".join(part.strip() for part in text_parts if part.strip())
    body_html = "\n<hr>\n".join(part.strip() for part in html_parts if part.strip())
    if not body_text and body_html:
        body_text = html_to_text(body_html)
    if not body_html and body_text:
        body_html = f"<pre>{html.escape(body_text)}</pre>"

    display_body_html = body_html or "<em>No displayable body.</em>"
    if storage == "legacy":
        body_path = message_dir / "body.html"
        body_path.write_text(display_body_html, encoding="utf-8")
        body_html_path = body_path.relative_to(out_dir).as_posix()
        raw_eml_path = raw_path.relative_to(out_dir).as_posix()
        stored_body_html = None
    else:
        body_html_path = ""
        raw_eml_path = ""
        stored_body_html = display_body_html

    date, year = parse_date(msg.get("Date"))
    from_name, from_email, from_domain, from_display = parse_from(msg.get("From"))
    labels = clean(msg.get("X-Gmail-Labels"))
    subject = clean(msg.get("Subject"))
    to_text = clean(msg.get("To"))
    preview = re.sub(r"\s+", " ", body_text).strip()[:500]
    size_mb = round(len(raw_bytes) / 1024 / 1024, 3)

    conn.execute(
        """
        INSERT INTO messages
        (id,date,year,from_name,from_email,from_domain,from_display,to_text,subject,labels,
         message_id,size_bytes,size_mb,preview,body_text,body_html,body_html_path,raw_eml_path,
         mbox_path,mbox_offset,mbox_length,storage_mode,mbox_from_line,in_reply_to,references_text,thread_key)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            index, date, year, from_name, from_email, from_domain, from_display,
            to_text, subject, labels, clean(msg.get("Message-ID")),
            len(raw_bytes), size_mb, preview, body_text, stored_body_html,
            body_html_path, raw_eml_path,
            str(mbox_path) if mbox_path else "", mbox_offset, mbox_length, storage,
            from_line.decode("utf-8", errors="replace").strip(),
            clean(msg.get("In-Reply-To")),
            clean(msg.get("References")),
            thread_key(msg),
        ),
    )
    conn.execute(
        """
        INSERT INTO messages_fts
        (rowid,subject,from_email,from_display,to_text,labels,preview,body_text)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (index, subject, from_email, from_display, to_text, labels, preview, body_text),
    )
    for label in split_labels(labels):
        conn.execute(
            "INSERT OR IGNORE INTO message_labels(label, message_id) VALUES (?, ?)",
            (label, index),
        )
    for email in message_users(from_email, to_text):
        conn.execute(
            "INSERT OR IGNORE INTO message_users(email, message_id) VALUES (?, ?)",
            (email, index),
        )
    for filename, path, content_type, size, sha in attachments:
        conn.execute(
            """
            INSERT INTO attachments
            (message_id,filename,path,content_type,size_bytes,size_mb,sha256)
            VALUES (?,?,?,?,?,?,?)
            """,
            (index, filename, path, content_type, size, round(size / 1024 / 1024, 3), sha),
        )


def main():
    parser = argparse.ArgumentParser(description="Import Gmail Takeout MBOX into this local SQLite viewer.")
    parser.add_argument("mbox", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--commit-every", type=int, default=500)
    parser.add_argument("--progress", type=int, default=1000)
    parser.add_argument("--only-indexes", default="", help="Comma-separated 1-based MBOX message indexes to import or repair.")
    parser.add_argument("--storage", choices=("compact", "legacy"), default="compact", help="compact stores body HTML in SQLite and deduplicated attachments in blobs/ (default); legacy writes messages/<id>/ files.")
    parser.add_argument("--legacy", action="store_true", help="Shortcut for --storage legacy.")
    args = parser.parse_args()
    if args.legacy:
        args.storage = "legacy"

    if args.rebuild and args.resume:
        raise SystemExit("Use only one of --rebuild or --resume.")
    if not args.mbox.exists():
        raise SystemExit(f"MBOX not found: {args.mbox}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    db_path = reset_output(args.out_dir, args.rebuild, args.resume, args.storage)
    reports_dir = args.out_dir / "reports"
    error_log = reports_dir / "import_errors.jsonl"
    summary_path = reports_dir / "import_summary.json"
    if args.rebuild and error_log.exists():
        error_log.unlink()

    started = time.time()
    mbox_size = args.mbox.stat().st_size
    conn = sqlite3.connect(db_path)
    init_db(conn)
    only_indexes = {int(item) for item in re.split(r"[,\s]+", args.only_indexes.strip()) if item}
    skip_through = 0 if only_indexes else max_imported_id(conn) if args.resume else 0
    imported = 0
    failed = 0
    seen = skip_through
    last_bytes = 0

    try:
        for index, raw, from_line, mbox_offset, mbox_length in iter_mbox_messages(args.mbox, args.limit, skip_through, only_indexes or None):
            seen = index
            last_bytes = (mbox_offset or 0) + (mbox_length or 0)
            if conn.execute("SELECT 1 FROM messages WHERE id = ?", (index,)).fetchone():
                continue
            conn.execute("SAVEPOINT message_import")
            try:
                insert_message(
                    conn, index, raw, from_line, args.out_dir,
                    storage=args.storage, mbox_path=args.mbox,
                    mbox_offset=mbox_offset, mbox_length=mbox_length,
                )
                conn.execute("RELEASE SAVEPOINT message_import")
                imported += 1
            except Exception as exc:
                conn.execute("ROLLBACK TO SAVEPOINT message_import")
                conn.execute("RELEASE SAVEPOINT message_import")
                failed += 1
                message_dir = args.out_dir / "messages" / f"{index:06d}"
                if message_dir.exists():
                    shutil.rmtree(message_dir, ignore_errors=True)
                append_jsonl(
                    error_log,
                    {
                        "index": index,
                        "error": str(exc),
                        "traceback": traceback.format_exc(limit=8),
                    },
                )
            if index % args.commit_every == 0:
                conn.commit()
            if args.progress and (index % args.progress == 0):
                elapsed = max(time.time() - started, 0.001)
                rate = imported / elapsed
                pct = (last_bytes / mbox_size * 100) if mbox_size else 0
                print(
                    f"seen={index} imported={imported} failed={failed} "
                    f"mbox={pct:.1f}% rate={rate:.1f}/s",
                    flush=True,
                )
        conn.commit()
    finally:
        validation = validate_import(conn, args.out_dir)
        summary = {
            "mbox": str(args.mbox),
            "out_dir": str(args.out_dir),
            "db": str(db_path),
            "mode": "rebuild" if args.rebuild else "resume" if args.resume else "new",
            "storage": args.storage,
            "limit": args.limit,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_seconds": round(time.time() - started, 2),
            "seen_message_index": seen,
            "skipped_existing": skip_through,
            "imported_this_run": imported,
            "failed_this_run": failed,
            "mbox_bytes_seen": last_bytes,
            "mbox_size_bytes": mbox_size,
            "validation": validation,
        }
        write_json(summary_path, summary)
        message_count = validation["messages"]
        attachment_count = validation["attachments"]
        conn.close()

    print(f"db={db_path}")
    print(f"messages={message_count}")
    print(f"attachments={attachment_count}")
    print(f"failed={failed}")
    print(f"summary={summary_path}")


if __name__ == "__main__":
    main()
