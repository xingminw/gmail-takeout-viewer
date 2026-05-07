import argparse
import html
import re
import sqlite3
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path


def clean(value):
    if value is None:
        return ""
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def safe_name(value, fallback):
    value = value or fallback
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:160] or fallback


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


def iter_mbox_messages(path, limit=0):
    current = bytearray()
    current_from = b""
    count = 0
    with path.open("rb") as handle:
        for line in handle:
            if line.startswith(b"From "):
                if current:
                    count += 1
                    yield count, bytes(current), current_from
                    if limit and count >= limit:
                        return
                    current = bytearray()
                current_from = line
                continue
            current.extend(line)
        if current and (not limit or count < limit):
            count += 1
            yield count, bytes(current), current_from


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


def init_db(conn):
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE messages (
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
          body_html_path TEXT,
          raw_eml_path TEXT,
          mbox_from_line TEXT,
          in_reply_to TEXT,
          references_text TEXT,
          thread_key TEXT
        );
        CREATE INDEX idx_messages_thread_key ON messages(thread_key);
        CREATE INDEX idx_messages_thread_date ON messages(thread_key, date DESC, id DESC);
        CREATE TABLE attachments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          message_id INTEGER NOT NULL,
          filename TEXT,
          path TEXT,
          content_type TEXT,
          size_bytes INTEGER,
          size_mb REAL,
          FOREIGN KEY(message_id) REFERENCES messages(id)
        );
        CREATE INDEX idx_attachments_message_id ON attachments(message_id);
        CREATE TABLE message_labels (
          label TEXT NOT NULL,
          message_id INTEGER NOT NULL,
          PRIMARY KEY(label, message_id),
          FOREIGN KEY(message_id) REFERENCES messages(id)
        );
        CREATE INDEX idx_message_labels_message_id ON message_labels(message_id);
        CREATE INDEX idx_messages_date ON messages(date);
        CREATE INDEX idx_messages_year ON messages(year);
        CREATE INDEX idx_messages_from_email ON messages(from_email);
        CREATE INDEX idx_messages_from_domain ON messages(from_domain);
        CREATE INDEX idx_messages_size ON messages(size_bytes);
        CREATE VIRTUAL TABLE messages_fts USING fts5(
          subject, from_email, from_display, to_text, labels, preview, body_text,
          content='messages', content_rowid='id'
        );
        """
    )


def reset_output(out_dir, rebuild):
    db_path = out_dir / "gmail_index.sqlite"
    messages_dir = out_dir / "messages"
    if db_path.exists() and not rebuild:
        raise SystemExit(f"Database already exists. Use --rebuild to replace: {db_path}")
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()
    messages_dir.mkdir(parents=True, exist_ok=True)
    return db_path


def insert_message(conn, index, raw_bytes, from_line, out_dir):
    msg = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    message_dir = out_dir / "messages" / f"{index:06d}"
    attachment_dir = message_dir / "attachments"
    attachment_dir.mkdir(parents=True, exist_ok=True)

    raw_path = message_dir / "raw.eml"
    raw_path.write_bytes(raw_bytes)

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
            if not filename:
                ext = content_type.split("/", 1)[-1] if "/" in content_type else "bin"
                filename = f"attachment-{len(attachments) + 1}.{ext}"
            safe = safe_name(filename, f"attachment-{len(attachments) + 1}.bin")
            if "." not in Path(safe).name and "/" in content_type:
                ext = content_type.split("/", 1)[1].split(";", 1)[0]
                safe = f"{safe}.{'jpg' if ext == 'jpeg' else ext}"
            target = attachment_dir / safe
            if target.exists():
                target.unlink()
            target.write_bytes(payload)
            attachments.append((filename, target.relative_to(out_dir).as_posix(), content_type, len(payload)))
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

    body_path = message_dir / "body.html"
    body_path.write_text(body_html or "<em>No displayable body.</em>", encoding="utf-8")

    date, year = parse_date(msg.get("Date"))
    from_name, from_email, from_domain, from_display = parse_from(msg.get("From"))
    labels = clean(msg.get("X-Gmail-Labels"))
    subject = clean(msg.get("Subject"))
    preview = re.sub(r"\s+", " ", body_text).strip()[:500]
    size_mb = round(len(raw_bytes) / 1024 / 1024, 3)

    conn.execute(
        """
        INSERT INTO messages
        (id,date,year,from_name,from_email,from_domain,from_display,to_text,subject,labels,
         message_id,size_bytes,size_mb,preview,body_text,body_html_path,raw_eml_path,mbox_from_line,
         in_reply_to,references_text,thread_key)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            index, date, year, from_name, from_email, from_domain, from_display,
            clean(msg.get("To")), subject, labels, clean(msg.get("Message-ID")),
            len(raw_bytes), size_mb, preview, body_text,
            body_path.relative_to(out_dir).as_posix(),
            raw_path.relative_to(out_dir).as_posix(),
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
        (index, subject, from_email, from_display, clean(msg.get("To")), labels, preview, body_text),
    )
    for label in split_labels(labels):
        conn.execute(
            "INSERT OR IGNORE INTO message_labels(label, message_id) VALUES (?, ?)",
            (label, index),
        )
    for filename, path, content_type, size in attachments:
        conn.execute(
            """
            INSERT INTO attachments
            (message_id,filename,path,content_type,size_bytes,size_mb)
            VALUES (?,?,?,?,?,?)
            """,
            (index, filename, path, content_type, size, round(size / 1024 / 1024, 3)),
        )


def main():
    parser = argparse.ArgumentParser(description="Import Gmail Takeout MBOX into this local SQLite viewer.")
    parser.add_argument("mbox", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--commit-every", type=int, default=500)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    db_path = reset_output(args.out_dir, args.rebuild)
    conn = sqlite3.connect(db_path)
    init_db(conn)

    try:
        for index, raw, from_line in iter_mbox_messages(args.mbox, args.limit):
            insert_message(conn, index, raw, from_line, args.out_dir)
            if index % args.commit_every == 0:
                conn.commit()
                print(f"indexed={index}", flush=True)
        conn.commit()
    finally:
        message_count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        attachment_count = conn.execute("SELECT count(*) FROM attachments").fetchone()[0]
        conn.close()

    print(f"db={db_path}")
    print(f"messages={message_count}")
    print(f"attachments={attachment_count}")


if __name__ == "__main__":
    main()
