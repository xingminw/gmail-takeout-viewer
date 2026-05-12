import json
import mimetypes
import os
import re
import shlex
import socket
import sqlite3
import threading
import webbrowser
from email import policy
from email.utils import getaddresses
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("GMAIL_VIEWER_DATA_DIR", APP_DIR)).resolve()
DB_PATH = DATA_DIR / "gmail_index.sqlite"
MESSAGES_DIR = DATA_DIR / "messages"
HOST = "127.0.0.1"
CONFIG_PATH = APP_DIR / "config.json"


def load_config():
    if not CONFIG_PATH.exists():
        return {"account_emails": []}
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


CONFIG = load_config()
ACCOUNT_EMAILS = {email.lower() for email in CONFIG.get("account_emails", [])}
DEFAULT_TOP_USER_EXCLUDE = [
    "noreply", "no-reply", "donotreply", "do-not-reply", "newsletter",
    "promo", "marketing", "offers", "rewards", "shop", "notification",
]
TOP_USER_INCLUDE_PATTERNS = [pattern.lower() for pattern in CONFIG.get("top_user_include_patterns", [])]
TOP_USER_EXCLUDE_PATTERNS = [
    pattern.lower()
    for pattern in CONFIG.get("top_user_exclude_patterns", DEFAULT_TOP_USER_EXCLUDE)
]


def is_relative_to(path, root):
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def find_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


READONLY_DB = False


def connect_db(readonly=False):
    if readonly or READONLY_DB:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro&immutable=1", uri=True)
    else:
        conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db():
    return connect_db()


def json_response(handler, payload, status=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_sql(sql, params=()):
    conn = db()
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def one_sql(sql, params=()):
    conn = db()
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def split_labels(labels):
    return [label.strip() for label in (labels or "").split(",") if label.strip()]


def schema_ready_readonly():
    required = {"messages", "attachments", "message_labels", "message_users", "conversation_index", "conversation_labels", "conversation_filters", "messages_fts"}
    conn = None
    try:
        conn = connect_db(readonly=True)
        existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required.issubset(existing):
            return False
        checks = [
            "SELECT count(*) FROM messages",
            "SELECT count(*) FROM conversation_index",
            "SELECT count(*) FROM conversation_labels",
            "SELECT count(*) FROM conversation_filters",
            "SELECT count(*) FROM messages_fts",
        ]
        for sql in checks:
            conn.execute(sql).fetchone()
        return True
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            conn.close()


def ensure_performance_schema():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS message_labels (
              label TEXT NOT NULL,
              message_id INTEGER NOT NULL,
              PRIMARY KEY(label, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_message_labels_message_id ON message_labels(message_id);
            CREATE TABLE IF NOT EXISTS message_users (
              email TEXT NOT NULL,
              message_id INTEGER NOT NULL,
              PRIMARY KEY(email, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_message_users_message_id ON message_users(message_id);
            CREATE INDEX IF NOT EXISTS idx_attachments_message_id ON attachments(message_id);
            CREATE INDEX IF NOT EXISTS idx_messages_thread_date ON messages(thread_key, date DESC, id DESC);
            CREATE TABLE IF NOT EXISTS conversation_index (
              conversation_id TEXT PRIMARY KEY,
              message_count INTEGER NOT NULL,
              latest_date TEXT,
              total_size_bytes INTEGER,
              latest_message_id INTEGER NOT NULL,
              attachment_count INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_conversation_index_latest_date ON conversation_index(latest_date);
            CREATE INDEX IF NOT EXISTS idx_conversation_index_size ON conversation_index(total_size_bytes);
            CREATE TABLE IF NOT EXISTS conversation_labels (
              label TEXT NOT NULL,
              conversation_id TEXT NOT NULL,
              message_count INTEGER NOT NULL,
              latest_date TEXT,
              total_size_bytes INTEGER,
              latest_message_id INTEGER NOT NULL,
              attachment_count INTEGER NOT NULL,
              PRIMARY KEY(label, conversation_id)
            );
            CREATE INDEX IF NOT EXISTS idx_conversation_labels_label_date ON conversation_labels(label, latest_date);
            CREATE TABLE IF NOT EXISTS conversation_filters (
              filter_type TEXT NOT NULL,
              filter_value TEXT NOT NULL,
              conversation_id TEXT NOT NULL,
              message_count INTEGER NOT NULL,
              latest_date TEXT,
              total_size_bytes INTEGER,
              latest_message_id INTEGER NOT NULL,
              attachment_count INTEGER NOT NULL,
              PRIMARY KEY(filter_type, filter_value, conversation_id)
            );
            CREATE INDEX IF NOT EXISTS idx_conversation_filters_value_date ON conversation_filters(filter_type, filter_value, latest_date);
            CREATE INDEX IF NOT EXISTS idx_conversation_filters_value_size ON conversation_filters(filter_type, filter_value, total_size_bytes);
            """
        )
        ensure_column(conn, "messages", "body_html", "TEXT")
        ensure_column(conn, "messages", "mbox_path", "TEXT")
        ensure_column(conn, "messages", "mbox_offset", "INTEGER")
        ensure_column(conn, "messages", "mbox_length", "INTEGER")
        ensure_column(conn, "messages", "storage_mode", "TEXT")
        ensure_column(conn, "attachments", "sha256", "TEXT")
        indexed = conn.execute("SELECT count(DISTINCT message_id) FROM message_labels").fetchone()[0]
        expected = conn.execute("SELECT count(*) FROM messages WHERE labels <> ''").fetchone()[0]
        if indexed != expected:
            conn.execute("DELETE FROM message_labels")
            rows = conn.execute("SELECT id, labels FROM messages WHERE labels <> ''")
            label_rows = [
                (label, message_id)
                for message_id, labels in rows
                for label in split_labels(labels)
            ]
            conn.executemany(
                "INSERT OR IGNORE INTO message_labels(label, message_id) VALUES (?, ?)",
                label_rows,
            )
        indexed_users = conn.execute("SELECT count(DISTINCT message_id) FROM message_users").fetchone()[0]
        expected_users = conn.execute(
            "SELECT count(*) FROM messages WHERE from_email <> '' OR to_text <> ''"
        ).fetchone()[0]
        if indexed_users != expected_users:
            conn.execute("DELETE FROM message_users")
            rows = conn.execute("SELECT id, from_email, to_text FROM messages WHERE from_email <> '' OR to_text <> ''")
            user_rows = []
            for message_id, from_email, to_text in rows:
                emails = set()
                from_email = (from_email or "").strip().lower()
                if from_email:
                    emails.add(from_email)
                for _, address in getaddresses([to_text or ""]):
                    address = address.strip().lower()
                    if address:
                        emails.add(address)
                user_rows.extend((email, message_id) for email in emails)
            conn.executemany(
                "INSERT OR IGNORE INTO message_users(email, message_id) VALUES (?, ?)",
                user_rows,
            )
        expected_conversations = conn.execute(
            "SELECT count(DISTINCT COALESCE(NULLIF(thread_key, ''), 'message:' || id)) FROM messages"
        ).fetchone()[0]
        indexed_conversations = conn.execute("SELECT count(*) FROM conversation_index").fetchone()[0]
        expected_label_conversations = conn.execute(
            """
            SELECT count(*) FROM (
              SELECT ml.label, COALESCE(NULLIF(m.thread_key, ''), 'message:' || m.id) AS conversation_id
              FROM message_labels ml
              JOIN messages m ON m.id = ml.message_id
              GROUP BY ml.label, conversation_id
            )
            """
        ).fetchone()[0]
        indexed_label_conversations = conn.execute("SELECT count(*) FROM conversation_labels").fetchone()[0]
        expected_filter_conversations = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM (
                SELECT year, COALESCE(NULLIF(thread_key, ''), 'message:' || id) AS conversation_id
                FROM messages
                WHERE year <> ''
                GROUP BY year, conversation_id
              )) +
              (SELECT count(*) FROM (
                SELECT from_domain, COALESCE(NULLIF(thread_key, ''), 'message:' || id) AS conversation_id
                FROM messages
                WHERE from_domain <> ''
                GROUP BY from_domain, conversation_id
              )) +
              (SELECT count(*) FROM (
                SELECT mu.email, COALESCE(NULLIF(m.thread_key, ''), 'message:' || m.id) AS conversation_id
                FROM message_users mu
                JOIN messages m ON m.id = mu.message_id
                GROUP BY mu.email, conversation_id
              ))
            """
        ).fetchone()[0]
        indexed_filter_conversations = conn.execute("SELECT count(*) FROM conversation_filters").fetchone()[0]
        if (
            indexed_conversations != expected_conversations
            or indexed_label_conversations != expected_label_conversations
            or indexed_filter_conversations != expected_filter_conversations
        ):
            rebuild_conversation_indexes(conn)
        conn.execute("PRAGMA optimize")
        conn.commit()
    finally:
        conn.close()


def ensure_column(conn, table, column, decl):
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def rebuild_conversation_indexes(conn):
    conn.executescript(
        """
        DELETE FROM conversation_index;
        DELETE FROM conversation_labels;
        DELETE FROM conversation_filters;

        INSERT INTO conversation_index
        (conversation_id,message_count,latest_date,total_size_bytes,latest_message_id,attachment_count)
        WITH message_conversations AS (
          SELECT m.id, m.date, m.size_bytes,
                 COALESCE(NULLIF(m.thread_key, ''), 'message:' || m.id) AS conversation_id
          FROM messages m
        ),
        ranked AS (
          SELECT mc.*,
                 count(*) OVER (PARTITION BY conversation_id) AS message_count,
                 max(date) OVER (PARTITION BY conversation_id) AS latest_date,
                 sum(size_bytes) OVER (PARTITION BY conversation_id) AS total_size_bytes,
                 row_number() OVER (PARTITION BY conversation_id ORDER BY date DESC, id DESC) AS rn
          FROM message_conversations mc
        ),
        attachment_counts AS (
          SELECT mc.conversation_id, count(a.id) AS attachment_count
          FROM message_conversations mc
          LEFT JOIN attachments a ON a.message_id = mc.id
          GROUP BY mc.conversation_id
        )
        SELECT r.conversation_id, r.message_count, r.latest_date, r.total_size_bytes, r.id,
               COALESCE(ac.attachment_count, 0)
        FROM ranked r
        LEFT JOIN attachment_counts ac ON ac.conversation_id = r.conversation_id
        WHERE r.rn = 1;

        INSERT INTO conversation_labels
        (label,conversation_id,message_count,latest_date,total_size_bytes,latest_message_id,attachment_count)
        WITH labeled AS (
          SELECT ml.label, m.id, m.date, m.size_bytes,
                 COALESCE(NULLIF(m.thread_key, ''), 'message:' || m.id) AS conversation_id
          FROM message_labels ml
          JOIN messages m ON m.id = ml.message_id
        ),
        ranked AS (
          SELECT l.*,
                 count(*) OVER (PARTITION BY label, conversation_id) AS message_count,
                 max(date) OVER (PARTITION BY label, conversation_id) AS latest_date,
                 sum(size_bytes) OVER (PARTITION BY label, conversation_id) AS total_size_bytes,
                 row_number() OVER (PARTITION BY label, conversation_id ORDER BY date DESC, id DESC) AS rn
          FROM labeled l
        ),
        attachment_counts AS (
          SELECT l.label, l.conversation_id, count(a.id) AS attachment_count
          FROM labeled l
          LEFT JOIN attachments a ON a.message_id = l.id
          GROUP BY l.label, l.conversation_id
        )
        SELECT r.label, r.conversation_id, r.message_count, r.latest_date, r.total_size_bytes, r.id,
               COALESCE(ac.attachment_count, 0)
        FROM ranked r
        LEFT JOIN attachment_counts ac ON ac.label = r.label AND ac.conversation_id = r.conversation_id
        WHERE r.rn = 1;

        INSERT INTO conversation_filters
        (filter_type,filter_value,conversation_id,message_count,latest_date,total_size_bytes,latest_message_id,attachment_count)
        WITH filtered AS (
          SELECT 'year' AS filter_type, m.year AS filter_value, m.id, m.date, m.size_bytes,
                 COALESCE(NULLIF(m.thread_key, ''), 'message:' || m.id) AS conversation_id
          FROM messages m
          WHERE m.year <> ''
          UNION ALL
          SELECT 'domain' AS filter_type, m.from_domain AS filter_value, m.id, m.date, m.size_bytes,
                 COALESCE(NULLIF(m.thread_key, ''), 'message:' || m.id) AS conversation_id
          FROM messages m
          WHERE m.from_domain <> ''
          UNION ALL
          SELECT 'user' AS filter_type, mu.email AS filter_value, m.id, m.date, m.size_bytes,
                 COALESCE(NULLIF(m.thread_key, ''), 'message:' || m.id) AS conversation_id
          FROM message_users mu
          JOIN messages m ON m.id = mu.message_id
        ),
        ranked AS (
          SELECT f.*,
                 count(*) OVER (PARTITION BY filter_type, filter_value, conversation_id) AS message_count,
                 max(date) OVER (PARTITION BY filter_type, filter_value, conversation_id) AS latest_date,
                 sum(size_bytes) OVER (PARTITION BY filter_type, filter_value, conversation_id) AS total_size_bytes,
                 row_number() OVER (PARTITION BY filter_type, filter_value, conversation_id ORDER BY date DESC, id DESC) AS rn
          FROM filtered f
        ),
        attachment_counts AS (
          SELECT f.filter_type, f.filter_value, f.conversation_id, count(a.id) AS attachment_count
          FROM filtered f
          LEFT JOIN attachments a ON a.message_id = f.id
          GROUP BY f.filter_type, f.filter_value, f.conversation_id
        )
        SELECT r.filter_type, r.filter_value, r.conversation_id, r.message_count, r.latest_date,
               r.total_size_bytes, r.id, COALESCE(ac.attachment_count, 0)
        FROM ranked r
        LEFT JOIN attachment_counts ac
          ON ac.filter_type = r.filter_type
         AND ac.filter_value = r.filter_value
         AND ac.conversation_id = r.conversation_id
        WHERE r.rn = 1;
        """
    )



def parse_size(value):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmMgG]?)", value.strip())
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2).lower()
    if unit == "g":
        number *= 1024 * 1024 * 1024
    elif unit == "m":
        number *= 1024 * 1024
    elif unit == "k":
        number *= 1024
    return int(number)


def fts_query(text):
    tokens = re.findall(r"[\w@.+-]+", text, flags=re.UNICODE)
    if not tokens:
        return ""
    return " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)


def add_text_search(clauses, values, text):
    text = text.strip()
    if not text:
        return
    clauses.append(
        """
        (
          m.subject LIKE ? OR m.from_email LIKE ? OR m.from_display LIKE ? OR
          m.to_text LIKE ? OR m.labels LIKE ? OR m.preview LIKE ? OR m.body_text LIKE ?
        )
        """
    )
    like = f"%{text}%"
    values.extend([like] * 7)


def apply_search_query(query, clauses, values):
    if not query:
        return
    try:
        tokens = shlex.split(query)
    except ValueError:
        tokens = query.split()

    plain = []
    for token in tokens:
        if ":" not in token:
            plain.append(token)
            continue
        key, value = token.split(":", 1)
        key = key.lower()
        value = value.strip().strip('"')
        if not value and key != "has":
            continue

        if key == "from":
            clauses.append("(m.from_email LIKE ? OR m.from_display LIKE ?)")
            values.extend([f"%{value.lower()}%", f"%{value}%"])
        elif key == "to":
            clauses.append("m.to_text LIKE ?")
            values.append(f"%{value}%")
        elif key == "subject":
            clauses.append("m.subject LIKE ?")
            values.append(f"%{value}%")
        elif key in ("label", "category"):
            clauses.append("m.labels LIKE ?")
            values.append(f"%{value}%")
        elif key == "has" and value.lower() in ("attachment", "attachments"):
            clauses.append("EXISTS (SELECT 1 FROM attachments a WHERE a.message_id = m.id)")
        elif key in ("larger", "size"):
            size = parse_size(value)
            if size is not None:
                clauses.append("m.size_bytes >= ?")
                values.append(size)
        elif key == "smaller":
            size = parse_size(value)
            if size is not None:
                clauses.append("m.size_bytes <= ?")
                values.append(size)
        elif key == "older":
            clauses.append("substr(m.date, 1, 10) < ?")
            values.append(value)
        elif key == "newer":
            clauses.append("substr(m.date, 1, 10) > ?")
            values.append(value)
        elif key == "year":
            clauses.append("m.year = ?")
            values.append(value)
        else:
            plain.append(token)

    if plain:
        add_text_search(clauses, values, " ".join(plain))

def build_where(params):
    clauses = []
    values = []

    q = params.get("q", [""])[0].strip()
    if q and ":" not in q:
        match_query = fts_query(q)
        if match_query:
            clauses.append("m.id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)")
            values.append(match_query)
        else:
            apply_search_query(q, clauses, values)
    else:
        apply_search_query(q, clauses, values)

    label = params.get("label", [""])[0].strip()
    if label:
        clauses.append("EXISTS (SELECT 1 FROM message_labels ml WHERE ml.message_id = m.id AND ml.label = ?)")
        values.append(label)

    year = params.get("year", [""])[0].strip()
    if year:
        clauses.append("m.year = ?")
        values.append(year)

    date_from = params.get("date_from", [""])[0].strip()
    if date_from:
        clauses.append("substr(m.date, 1, 10) >= ?")
        values.append(date_from)

    date_to = params.get("date_to", [""])[0].strip()
    if date_to:
        clauses.append("substr(m.date, 1, 10) <= ?")
        values.append(date_to)

    domain = params.get("domain", [""])[0].strip()
    if domain:
        clauses.append("m.from_domain = ?")
        values.append(domain)

    user = params.get("user", [""])[0].strip().lower()
    if user:
        clauses.append("EXISTS (SELECT 1 FROM message_users mu WHERE mu.message_id = m.id AND mu.email = ?)")
        values.append(user)

    attach = params.get("attachments", [""])[0].strip()
    if attach == "1":
        clauses.append("EXISTS (SELECT 1 FROM attachments a WHERE a.message_id = m.id)")

    mailbox = params.get("mailbox", [""])[0].strip()
    account_match = " OR ".join(["m.from_email = ?" for _ in ACCOUNT_EMAILS])
    account_to_match = " OR ".join(["LOWER(m.to_text) LIKE ?" for _ in ACCOUNT_EMAILS])
    if mailbox == "sent":
        if account_match:
            clauses.append(f"(m.labels LIKE '%Sent%' OR {account_match})")
            values.extend(sorted(ACCOUNT_EMAILS))
        else:
            clauses.append("m.labels LIKE '%Sent%'")
    elif mailbox == "inbox":
        clauses.append("m.labels LIKE '%Inbox%'")
    elif mailbox == "spam":
        clauses.append("m.labels LIKE '%Spam%'")
    elif mailbox == "trash":
        clauses.append("m.labels LIKE '%Trash%'")
    elif mailbox == "important":
        clauses.append("m.labels LIKE '%Important%'")
    elif mailbox == "received":
        if account_match:
            clauses.append(f"NOT (m.labels LIKE '%Sent%' OR {account_match})")
            values.extend(sorted(ACCOUNT_EMAILS))
        else:
            clauses.append("m.labels NOT LIKE '%Sent%'")
        if account_to_match:
            clauses.append(f"(m.labels LIKE '%Inbox%' OR {account_to_match})")
            values.extend([f"%{email}%" for email in sorted(ACCOUNT_EMAILS)])

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return where, values


MESSAGE_SUMMARY_COLUMNS = """
    id, date, year, from_name, from_email, from_domain, from_display,
    to_text, subject, labels, message_id, size_bytes, size_mb, preview,
    body_html_path, raw_eml_path, mbox_path, mbox_offset, mbox_length,
    storage_mode, mbox_from_line, in_reply_to, references_text, thread_key
"""


def list_messages(params):
    page = max(int(params.get("page", ["1"])[0] or "1"), 1)
    page_size = min(max(int(params.get("page_size", ["50"])[0] or "50"), 10), 100)
    offset = (page - 1) * page_size
    sort = params.get("sort", ["date_desc"])[0]
    order_by = {
        "date_asc": "m.date ASC",
        "date_desc": "m.date DESC",
        "size_desc": "m.size_bytes DESC",
        "size_asc": "m.size_bytes ASC",
        "sender": "m.from_email ASC, m.date DESC",
        "subject": "m.subject ASC, m.date DESC",
    }.get(sort, "m.date DESC")

    where, values = build_where(params)
    rows = read_sql(
        f"""
        SELECT m.id, m.date, m.year, m.from_display, m.from_email, m.from_domain,
               m.subject, m.labels, m.size_mb, m.preview,
               (SELECT count(*) FROM attachments a WHERE a.message_id = m.id) AS attachment_count
        FROM messages m
        {where}
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
        """,
        (*values, page_size, offset),
    )
    total = one_sql(f"SELECT count(*) AS n FROM messages m {where}", values)["n"]
    return {"rows": rows, "page": page, "page_size": page_size, "total": total}


def fast_conversation_filter(params):
    blocking = ("q", "date_from", "date_to")
    if any(params.get(key, [""])[0].strip() for key in blocking):
        return None

    clauses = []
    values = []
    label = params.get("label", [""])[0].strip()
    mailbox = params.get("mailbox", [""])[0].strip()
    mailbox_labels = {
        "inbox": "Inbox",
        "sent": "Sent",
        "spam": "Spam",
        "trash": "Trash",
        "important": "Important",
    }
    table = "conversation_index"
    filter_specs = [
        ("year", params.get("year", [""])[0].strip()),
        ("domain", params.get("domain", [""])[0].strip()),
        ("user", params.get("user", [""])[0].strip().lower()),
    ]
    active_filters = [(kind, value) for kind, value in filter_specs if value]

    if len(active_filters) == 1 and not label and not mailbox:
        filter_type, filter_value = active_filters[0]
        table = "conversation_filters"
        clauses.append("c.filter_type = ?")
        clauses.append("c.filter_value = ?")
        values.extend([filter_type, filter_value])
    elif active_filters:
        return None
    elif label:
        table = "conversation_labels"
        clauses.append("c.label = ?")
        values.append(label)
    elif mailbox in mailbox_labels:
        table = "conversation_labels"
        clauses.append("c.label = ?")
        values.append(mailbox_labels[mailbox])
    elif mailbox:
        return None

    if params.get("attachments", [""])[0].strip() == "1":
        clauses.append("c.attachment_count > 0")

    return table, clauses, values


def list_conversations_fast(params, table, clauses, values):
    page = max(int(params.get("page", ["1"])[0] or "1"), 1)
    page_size = min(max(int(params.get("page_size", ["50"])[0] or "50"), 10), 100)
    offset = (page - 1) * page_size
    sort = params.get("sort", ["date_desc"])[0]
    order_by = {
        "date_asc": "c.latest_date ASC",
        "date_desc": "c.latest_date DESC",
        "size_desc": "c.total_size_bytes DESC",
        "size_asc": "c.total_size_bytes ASC",
        "sender": "m.from_email ASC, c.latest_date DESC",
        "subject": "m.subject ASC, c.latest_date DESC",
    }.get(sort, "c.latest_date DESC")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = read_sql(
        f"""
        SELECT c.conversation_id, c.message_count, c.latest_date,
               round(c.total_size_bytes / 1024.0 / 1024.0, 3) AS total_size_mb,
               c.latest_message_id, m.from_display, m.from_email, m.from_domain,
               m.subject, m.labels, m.preview, c.attachment_count
        FROM {table} c
        JOIN messages m ON m.id = c.latest_message_id
        {where}
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
        """,
        (*values, page_size, offset),
    )
    total = one_sql(f"SELECT count(*) AS n FROM {table} c {where}", values)["n"]
    return {"rows": rows, "page": page, "page_size": page_size, "total": total}


def list_conversations_fts_fast(params, match_query):
    page = max(int(params.get("page", ["1"])[0] or "1"), 1)
    page_size = min(max(int(params.get("page_size", ["50"])[0] or "50"), 10), 100)
    offset = (page - 1) * page_size
    scan_limit = min(max((offset + page_size) * 20, 500), 5000)
    conn = db()
    try:
        hits = conn.execute(
            """
            SELECT m.id, m.date,
                   COALESCE(NULLIF(m.thread_key, ''), 'message:' || m.id) AS conversation_id
            FROM messages_fts f
            JOIN messages m ON m.id = f.rowid
            WHERE messages_fts MATCH ?
            LIMIT ?
            """,
            (match_query, scan_limit),
        ).fetchall()
        seen = set()
        ordered_ids = []
        for hit in hits:
            conversation_id = hit["conversation_id"]
            if conversation_id in seen:
                continue
            seen.add(conversation_id)
            ordered_ids.append(conversation_id)
        page_ids = ordered_ids[offset : offset + page_size]
        if not page_ids:
            rows = []
        else:
            placeholders = ",".join("?" for _ in page_ids)
            row_map = {
                row["conversation_id"]: dict(row)
                for row in conn.execute(
                    f"""
                    SELECT c.conversation_id, c.message_count, c.latest_date,
                           round(c.total_size_bytes / 1024.0 / 1024.0, 3) AS total_size_mb,
                           c.latest_message_id, m.from_display, m.from_email, m.from_domain,
                           m.subject, m.labels, m.preview, c.attachment_count
                    FROM conversation_index c
                    JOIN messages m ON m.id = c.latest_message_id
                    WHERE c.conversation_id IN ({placeholders})
                    """,
                    page_ids,
                )
            }
            rows = [row_map[conversation_id] for conversation_id in page_ids if conversation_id in row_map]
        total = conn.execute("SELECT count(*) AS n FROM messages_fts WHERE messages_fts MATCH ?", (match_query,)).fetchone()["n"]
    finally:
        conn.close()
    return {"rows": rows, "page": page, "page_size": page_size, "total": total, "total_kind": "matching_messages"}


def list_conversations(params):
    q = params.get("q", [""])[0].strip()
    simple_search_only = q and ":" not in q and not any(
        params.get(key, [""])[0].strip()
        for key in ("year", "domain", "user", "date_from", "date_to", "label", "mailbox", "attachments")
    )
    if simple_search_only:
        match_query = fts_query(q)
        if match_query:
            return list_conversations_fts_fast(params, match_query)

    fast_filter = fast_conversation_filter(params)
    if fast_filter:
        return list_conversations_fast(params, *fast_filter)

    page = max(int(params.get("page", ["1"])[0] or "1"), 1)
    page_size = min(max(int(params.get("page_size", ["50"])[0] or "50"), 10), 100)
    offset = (page - 1) * page_size
    sort = params.get("sort", ["date_desc"])[0]
    order_by = {
        "date_asc": "r.latest_date ASC",
        "date_desc": "r.latest_date DESC",
        "size_desc": "r.total_size_bytes DESC",
        "size_asc": "r.total_size_bytes ASC",
        "sender": "r.from_email ASC, r.latest_date DESC",
        "subject": "r.subject ASC, r.latest_date DESC",
    }.get(sort, "r.latest_date DESC")

    where, values = build_where(params)
    base = f"""
        WITH filtered AS (
          SELECT m.id, m.date, m.size_bytes, m.from_email, m.from_display,
                 m.from_domain, m.subject, m.labels, m.preview,
                 COALESCE(NULLIF(m.thread_key, ''), 'message:' || m.id) AS conversation_id
          FROM messages m
          {where}
        ),
        ranked AS (
          SELECT f.*,
                 count(*) OVER (PARTITION BY conversation_id) AS message_count,
                 max(date) OVER (PARTITION BY conversation_id) AS latest_date,
                 sum(size_bytes) OVER (PARTITION BY conversation_id) AS total_size_bytes,
                 row_number() OVER (PARTITION BY conversation_id ORDER BY date DESC, id DESC) AS rn
          FROM filtered f
        ),
        attachment_counts AS (
          SELECT r.conversation_id, count(a.id) AS attachment_count
          FROM ranked r
          LEFT JOIN attachments a ON a.message_id = r.id
          GROUP BY r.conversation_id
        )
    """
    rows = read_sql(
        base
        + f"""
        SELECT r.conversation_id, r.message_count, r.latest_date,
               round(r.total_size_bytes / 1024.0 / 1024.0, 3) AS total_size_mb,
               r.id AS latest_message_id, r.from_display, r.from_email,
               r.from_domain, r.subject, r.labels, r.preview,
               COALESCE(ac.attachment_count, 0) AS attachment_count
        FROM ranked r
        LEFT JOIN attachment_counts ac ON ac.conversation_id = r.conversation_id
        WHERE r.rn = 1
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
        """,
        (*values, page_size, offset),
    )
    total = one_sql(
        f"""
        WITH filtered AS (
          SELECT m.id, COALESCE(NULLIF(m.thread_key, ''), 'message:' || m.id) AS conversation_id
          FROM messages m
          {where}
        )
        SELECT count(DISTINCT conversation_id) AS n FROM filtered
        """,
        values,
    )["n"]
    return {"rows": rows, "page": page, "page_size": page_size, "total": total}


def attachments_by_message(message_ids):
    if not message_ids:
        return {}
    placeholders = ",".join("?" for _ in message_ids)
    grouped = {message_id: [] for message_id in message_ids}
    rows = read_sql(
        f"""
        SELECT message_id, filename, path, content_type, size_mb, size_bytes
        FROM attachments
        WHERE message_id IN ({placeholders})
        ORDER BY message_id, id
        """,
        message_ids,
    )
    for row in rows:
        message_id = row.pop("message_id")
        grouped.setdefault(message_id, []).append(row)
    return grouped


def conversation_message_rows(conversation_id):
    if conversation_id.startswith("message:") and conversation_id[8:].isdigit():
        return read_sql(
            f"""
            SELECT {MESSAGE_SUMMARY_COLUMNS}
            FROM messages
            WHERE id = ?
            ORDER BY date ASC, id ASC
            """,
            (int(conversation_id[8:]),),
        )
    return read_sql(
        f"""
        SELECT {MESSAGE_SUMMARY_COLUMNS}
        FROM messages
        WHERE thread_key = ?
        ORDER BY date ASC, id ASC
        """,
        (conversation_id,),
    )


def facets():
    self_emails = set(ACCOUNT_EMAILS)
    for row in read_sql(
        """
        SELECT DISTINCT LOWER(m.from_email) AS email
        FROM messages m
        JOIN message_labels ml ON ml.message_id = m.id
        WHERE ml.label = 'Sent' AND m.from_email <> ''
        """
    ):
        self_emails.add(row["email"])
    user_clauses = []
    user_params = []
    if self_emails:
        placeholders = ",".join("?" for _ in self_emails)
        user_clauses.append(f"email NOT IN ({placeholders})")
        user_params.extend(sorted(self_emails))
    if TOP_USER_INCLUDE_PATTERNS:
        user_clauses.append("(" + " OR ".join("email LIKE ?" for _ in TOP_USER_INCLUDE_PATTERNS) + ")")
        user_params.extend(TOP_USER_INCLUDE_PATTERNS)
    for pattern in TOP_USER_EXCLUDE_PATTERNS:
        user_clauses.append("email NOT LIKE ?")
        user_params.append(f"%{pattern}%")
    user_where = "WHERE " + " AND ".join(user_clauses) if user_clauses else ""
    return {
        "years": read_sql(
            "SELECT year AS name, count(*) AS count FROM messages WHERE year <> '' GROUP BY year ORDER BY year DESC"
        ),
        "users": read_sql(
            f"SELECT email AS name, count(*) AS count FROM message_users {user_where} GROUP BY email ORDER BY count DESC LIMIT 40",
            user_params,
        ),
        "domains": read_sql(
            "SELECT from_domain AS name, count(*) AS count FROM messages WHERE from_domain <> '' GROUP BY from_domain ORDER BY count DESC LIMIT 25"
        ),
        "labels": read_sql(
            "SELECT label AS name, count(*) AS count FROM message_labels GROUP BY label ORDER BY count DESC"
        ),
    }


def message_detail(message_id):
    message = one_sql(f"SELECT {MESSAGE_SUMMARY_COLUMNS} FROM messages WHERE id = ?", (message_id,))
    if not message:
        return None
    message["body_url"] = body_url(message)
    message["attachments"] = attachments_by_message([message_id]).get(message_id, [])
    return message


def conversation_detail(conversation_id):
    rows = conversation_message_rows(conversation_id)
    if not rows:
        return None
    attachments = attachments_by_message([row["id"] for row in rows])
    for row in rows:
        row["body_url"] = body_url(row)
        row["attachments"] = attachments.get(row["id"], [])
    return {
        "conversation_id": conversation_id,
        "subject": rows[-1].get("subject") or rows[0].get("subject") or "(no subject)",
        "message_count": len(rows),
        "messages": rows,
    }


def body_url(message):
    path = message.get("body_html_path") or ""
    if path:
        return "/file/" + quote(path, safe="/")
    return f"/body/{message['id']}"


def message_body_html(message_id):
    message = one_sql("SELECT id, body_html, body_html_path FROM messages WHERE id = ?", (message_id,))
    if not message:
        return None
    if message.get("body_html"):
        return rewrite_inline_cids(f"body/{message_id}", message["body_html"])
    rel = message.get("body_html_path") or ""
    if rel:
        target = (DATA_DIR / rel).resolve()
        if is_relative_to(target, DATA_DIR) and target.exists():
            return rewrite_inline_cids(rel, target.read_text(encoding="utf-8", errors="replace"))
    return "<em>No displayable body.</em>"


def rewrite_inline_cids(rel_path, html_text):
    match = re.match(r"^messages/(\d+)/body\.html$", rel_path.replace("\\", "/"))
    if not match:
        return html_text

    message_id = int(match.group(1))
    attachments = read_sql(
        "SELECT filename,path FROM attachments WHERE message_id = ? ORDER BY id",
        (message_id,),
    )
    cid_map = {}
    filename_map = {}
    for attachment in attachments:
        filename = attachment.get("filename") or Path(attachment.get("path") or "").name
        if not filename:
            continue
        local_url = "/file/" + quote(attachment["path"], safe="/")
        names = {filename.lower(), Path(filename).name.lower(), Path(filename).stem.lower()}
        for name in names:
            cid_map.setdefault(name, local_url)
            filename_map.setdefault(name, local_url)

    raw_path = DATA_DIR / "messages" / f"{message_id:06d}" / "raw.eml"
    if raw_path.exists():
        try:
            msg = BytesParser(policy=policy.default).parsebytes(raw_path.read_bytes())
            for part in msg.walk():
                content_id = part.get("Content-ID")
                filename = part.get_filename()
                if not content_id or not filename:
                    continue
                local_url = filename_map.get(filename.lower()) or filename_map.get(Path(filename).name.lower())
                if local_url:
                    cid_map.setdefault(content_id.strip("<>").lower(), local_url)
        except Exception:
            pass

    if not cid_map:
        return html_text

    def replace(match):
        cid = unquote(match.group(1)).strip("<>")
        keys = [cid.lower(), cid.split("@", 1)[0].lower(), Path(cid.split("@", 1)[0]).stem.lower()]
        for key in keys:
            if key in cid_map:
                return cid_map[key]
        return match.group(0)

    return re.sub(r"(?i)cid:([^\"'\s>)]+)", replace, html_text)


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gmail Takeout Viewer</title>
<style>
:root{--bg:#f4f6f8;--panel:#fff;--line:#d9dee5;--text:#202124;--muted:#67727e;--accent:#0b57d0;--accent-bg:#e8f0fe;--chip:#eef2f6;--nav-w:250px;--list-w:520px}
*{box-sizing:border-box} html,body{height:100%;overflow:hidden} body{margin:0;font-family:Segoe UI,Arial,sans-serif;color:var(--text);background:var(--bg)}
.app{display:grid;grid-template-columns:var(--nav-w) 6px var(--list-w) 6px minmax(360px,1fr);height:100vh;overflow:hidden}
aside{border-right:1px solid var(--line);background:#fbfcfd;padding:16px 12px;overflow-y:auto;min-height:0}
.resizer{background:#eef1f4;cursor:col-resize;min-width:6px}.resizer:hover,.resizer.dragging{background:#c8d6ee}
.brand{font-size:21px;font-weight:650;margin:4px 10px 4px}.subbrand{font-size:12px;color:var(--muted);margin:0 10px 18px}
.filter{display:flex;justify-content:space-between;gap:8px;width:100%;border:0;background:transparent;text-align:left;padding:8px 10px;border-radius:18px;cursor:pointer;color:var(--text);font-size:14px}
.filter:hover,.filter.active{background:var(--accent-bg);color:#174ea6}.group{margin:20px 0 7px 10px;color:var(--muted);font-size:11px;text-transform:uppercase;font-weight:650;letter-spacing:.04em}
details{margin-top:12px}summary{cursor:pointer;list-style:none;margin:0 0 7px 10px;color:var(--muted);font-size:11px;text-transform:uppercase;font-weight:650;letter-spacing:.04em;display:flex;align-items:center;gap:6px}summary::-webkit-details-marker{display:none}summary::before{content:'>';font-size:12px;color:#8894a1;transition:transform .12s ease}details[open] summary::before{transform:rotate(90deg)}
.list{border-right:1px solid var(--line);background:var(--panel);display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden}
.toolbar{padding:12px;border-bottom:1px solid var(--line);display:grid;grid-template-rows:auto auto;gap:9px}
.search-row{display:grid;grid-template-columns:1fr 104px 92px;gap:8px;align-items:center}.filter-row{display:grid;grid-template-columns:1fr 126px 126px 88px;gap:8px;align-items:center}
.scope{display:flex;align-items:center;gap:6px;min-width:0;overflow:hidden}.scope-label{font-size:12px;color:var(--muted);white-space:nowrap}.scope-chips{display:flex;gap:6px;min-width:0;overflow:hidden}.scope-chip{background:var(--accent-bg);color:#174ea6;border-radius:14px;padding:5px 9px;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:220px}.loading .messages{opacity:.55}.loading .status-loading{display:inline}.status-loading{display:none;color:var(--accent);font-weight:600}
.scope-chip button{border:0;background:transparent;color:#174ea6;padding:0 0 0 6px;border-radius:0;font-size:13px;line-height:1}
input,select,button{border:1px solid var(--line);border-radius:18px;padding:8px 12px;background:#fff;min-width:0;font:inherit}
input:focus,select:focus{outline:2px solid #c2dbff;border-color:#86b7ff}button{cursor:pointer}
.messages{overflow-y:auto;min-height:0;flex:1}.pager{padding:10px 12px;border-top:1px solid var(--line);color:var(--muted);font-size:13px;display:flex;justify-content:space-between;align-items:center;gap:8px}
.pager button{min-width:72px}.page-jump{display:flex;align-items:center;gap:6px;min-width:150px;justify-content:center}.page-jump input{width:68px;text-align:center;padding:6px 8px}.row{padding:13px 15px;border-bottom:1px solid #edf0f2;cursor:pointer;border-left:3px solid transparent}
.row:hover{background:#f8fbff}.row.active{background:#eef5ff;border-left-color:var(--accent)}
.subject{font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.meta,.preview,.chips{font-size:12px;color:var(--muted);margin-top:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip{display:inline-block;background:var(--chip);border-radius:12px;padding:2px 7px;margin-right:4px;color:#46515c}.detail{overflow-y:auto;background:var(--panel);padding:24px 30px;min-height:0}
.detail h1{font-size:23px;line-height:1.25;font-weight:560;margin:0 0 16px}.kv{color:var(--muted);font-size:13px;margin:5px 0}.kv b{color:#3c4043;font-weight:600}
.attachments{margin:18px 0;display:flex;flex-wrap:wrap;gap:8px}.att{border:1px solid var(--line);border-radius:8px;padding:9px 11px;text-decoration:none;color:#202124;background:#fafafa;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.message-card{border:1px solid var(--line);border-radius:8px;margin:16px 0;background:#fff}.message-head{padding:12px 14px;border-bottom:1px solid #edf0f2}.message-body{padding:0 14px 14px}
.body-toggle{margin:12px 0;border-radius:8px}.message-card.latest .message-body .body-toggle{display:none}
.att:hover{border-color:#9bbcf3;background:#f4f8ff}.body-frame{width:100%;min-height:520px;border:1px solid var(--line);border-radius:8px;background:#fff;overflow:hidden}.empty{padding:32px;color:var(--muted)}.small{font-size:12px;color:var(--muted)}
</style>
</head>
<body>
<div class="app">
  <aside>
    <div class="brand">Gmail Takeout Viewer</div>
    <div class="subbrand">Local Gmail archive viewer</div>
    <button class="filter active" data-type="" data-value="">All mail</button>
    <button class="filter" data-type="mailbox" data-value="inbox">Inbox</button>
    <button class="filter" data-type="mailbox" data-value="sent">Sent</button>
    <button class="filter" data-type="mailbox" data-value="important">Important</button>
    <button class="filter" data-type="mailbox" data-value="spam">Spam</button>
    <button class="filter" data-type="mailbox" data-value="trash">Trash</button>
    <button class="filter" data-type="attachments" data-value="1">Has attachments</button>
    <details><summary>Years</summary><div id="years"></div></details>
    <details><summary>Labels</summary><div id="labels"></div></details>
    <details><summary>Top users</summary><div id="users"></div></details>
    <details><summary>Top domains</summary><div id="domains"></div></details>
  </aside>
  <div class="resizer" data-resize="nav" title="Drag to resize sidebar"></div>
  <section class="list">
    <div class="toolbar">
      <div class="search-row">
        <input id="q" placeholder="Search within current filters">
        <select id="sort">
          <option value="date_desc">Newest</option><option value="date_asc">Oldest</option>
          <option value="size_desc">Largest</option>
        </select>
        <button id="searchBtn">Search</button>
      </div>
      <div class="filter-row">
        <div class="scope"><span class="scope-label">Filters</span><div id="scopeChips" class="scope-chips"></div></div>
        <input id="dateFrom" type="date" title="Start date">
        <input id="dateTo" type="date" title="End date">
        <button id="clearFilters">Clear</button>
      </div>
    </div>
    <div id="messages" class="messages"></div>
    <div class="pager"><button id="prev">Prev</button><div class="page-jump"><span id="status"></span><input id="pageJump" type="number" min="1" value="1" title="Page"></div><button id="next">Next</button></div>
  </section>
  <div class="resizer" data-resize="list" title="Drag to resize message list"></div>
  <main id="detail" class="detail"><div class="empty">Select a message.</div></main>
</div>
<script>
let state={page:1,pageSize:50,sort:'date_desc',q:'',dateFrom:'',dateTo:'',filters:{},active:null,total:0,pageCount:1,loading:false,requestId:0};
const $=id=>document.getElementById(id);
function esc(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function api(path){const r=await fetch(path); if(!r.ok) throw new Error(await r.text()); return await r.json();}
function query(){const p=new URLSearchParams({page:state.page,page_size:state.pageSize,sort:state.sort}); if(state.q)p.set('q',state.q); if(state.dateFrom)p.set('date_from',state.dateFrom); if(state.dateTo)p.set('date_to',state.dateTo); Object.entries(state.filters).forEach(([k,v])=>{if(v)p.set(k,v);}); return p;}
async function loadFacets(){const f=await api('/api/facets'); renderFacet('years',f.years,'year'); renderFacet('users',f.users,'user'); renderFacet('domains',f.domains,'domain'); renderFacet('labels',f.labels,'label'); bindFilters();}
function renderFacet(id,rows,type){const limit=id==='labels'?40:rows.length; const visible=rows.slice(0,limit); const more=rows.length>limit?`<div class="small" style="padding:6px 10px">${rows.length-limit} more hidden</div>`:''; $(id).innerHTML=visible.map(r=>`<button class="filter" data-type="${type}" data-value="${esc(r.name)}"><span>${esc(r.name)}</span><span class="small">${r.count}</span></button>`).join('')+more;}
function filterLabel(type,value){const names={mailbox:'Mailbox',label:'Label',year:'Year',user:'User',domain:'Domain',attachments:'Has attachments'}; if(!type)return 'All mail'; return type==='attachments'?'Has attachments':`${names[type]||type}: ${value}`;}
function chip(label,type){return `<span class="scope-chip">${esc(label)}<button data-clear="${esc(type)}" title="Remove filter">x</button></span>`;}
function bindChipClears(){document.querySelectorAll('[data-clear]').forEach(b=>b.onclick=()=>{const t=b.dataset.clear; if(t==='q'){state.q=''; $('q').value='';} else if(t==='dateFrom'){state.dateFrom=''; $('dateFrom').value='';} else if(t==='dateTo'){state.dateTo=''; $('dateTo').value='';} else {delete state.filters[t];} state.page=1; renderScope(); loadList('updated filters');});}
function renderScope(){const chips=[]; Object.entries(state.filters).forEach(([k,v])=>{if(v)chips.push(chip(filterLabel(k,v),k));}); if(state.q)chips.push(chip(`Search: ${state.q}`,'q')); if(state.dateFrom)chips.push(chip(`From ${state.dateFrom}`,'dateFrom')); if(state.dateTo)chips.push(chip(`To ${state.dateTo}`,'dateTo')); $('scopeChips').innerHTML=chips.join('')||'<span class="small">All mail</span>'; bindChipClears(); document.querySelectorAll('.filter').forEach(b=>{const t=b.dataset.type||''; const v=b.dataset.value||''; b.classList.toggle('active',(!t&&!Object.keys(state.filters).length)||(state.filters[t]===v));});}
function activateFilter(b,load=true){const type=b.dataset.type||''; const value=b.dataset.value||''; state.page=1; state.active=null; if(!type){state.filters={};} else if(type==='attachments'){state.filters.attachments=state.filters.attachments==='1'?'':'1'; if(!state.filters.attachments)delete state.filters.attachments;} else {state.filters[type]=value;} renderScope(); if(load)loadList(filterLabel(type,value));}
function bindFilters(){document.querySelectorAll('.filter').forEach(b=>b.onclick=()=>activateFilter(b));}
function activateDefaultFilter(){const b=document.querySelector('.filter[data-type=""][data-value=""]'); if(b)activateFilter(b,false);}
async function loadList(reason='conversations'){const requestId=++state.requestId; state.loading=true; document.body.classList.add('loading'); $('messages').innerHTML=`<div class="empty">Loading ${esc(reason)}...</div>`; $('status').innerHTML='<span class="status-loading">Loading...</span>'; $('prev').disabled=true; $('next').disabled=true; renderScope(); try{const data=await api('/api/conversations?'+query().toString()); if(requestId!==state.requestId)return; state.total=data.total; state.page=data.page; state.pageSize=data.page_size; state.pageCount=Math.max(1,Math.ceil(data.total/data.page_size)); const rows=data.rows; $('messages').innerHTML=rows.map(m=>conversationRow(m)).join('')||'<div class="empty">No messages.</div>'; document.querySelectorAll('.row').forEach(r=>r.onclick=()=>showConversation(r.dataset.id)); const start=data.total?(data.page-1)*data.page_size+1:0; const end=Math.min(data.page*data.page_size,data.total); $('status').textContent=data.total?`${start}-${end} of ${data.total} | Page ${data.page}/${state.pageCount}`:'0 messages'; $('pageJump').value=data.page; $('pageJump').max=state.pageCount; $('prev').disabled=state.page<=1; $('next').disabled=end>=data.total;}catch(e){if(requestId===state.requestId){$('messages').innerHTML=`<div class="empty">Load failed: ${esc(e.message)}</div>`; $('status').textContent='Load failed';}}finally{if(requestId===state.requestId){state.loading=false; document.body.classList.remove('loading');}}}
function conversationRow(m){return `<div class="row ${m.conversation_id===state.active?'active':''}" data-id="${esc(m.conversation_id)}"><div class="subject">${esc(m.subject||'(no subject)')} ${m.message_count>1?`<span class="small">(${m.message_count})</span>`:''}</div><div class="meta">${esc(m.from_display)} - ${esc(m.latest_date)} - ${m.total_size_mb} MB</div><div class="preview">${esc(m.preview)}</div><div class="chips">${(m.labels||'').split(',').filter(Boolean).slice(0,5).map(l=>`<span class="chip">${esc(l.trim())}</span>`).join('')} ${m.attachment_count?`<span class="chip">${m.attachment_count} attachment(s)</span>`:''}</div></div>`;}
function resizeFrame(frame){try{const doc=frame.contentDocument||frame.contentWindow.document; const h=Math.max(520, doc.documentElement.scrollHeight, doc.body.scrollHeight); frame.style.height=(h+24)+'px';}catch(e){}}
function setActiveRow(id){state.active=id; document.querySelectorAll('.row').forEach(r=>r.classList.toggle('active', r.dataset.id===String(id)));}
async function show(id){setActiveRow(id); $('detail').innerHTML='<div class="empty">Loading message...</div>'; const m=await api('/api/message/'+id); const atts=m.attachments.map(a=>`<a class="att" href="/file/${encodeURIComponent(a.path)}" target="_blank">${esc(a.filename)} - ${a.size_mb} MB</a>`).join(''); $('detail').innerHTML=`<h1>${esc(m.subject||'(no subject)')}</h1><div class="kv"><b>From:</b> ${esc(m.from_display)}</div><div class="kv"><b>To:</b> ${esc(m.to_text)}</div><div class="kv"><b>Date:</b> ${esc(m.date)}</div><div class="kv"><b>Size:</b> ${m.size_mb} MB</div><div class="chips">${(m.labels||'').split(',').filter(Boolean).map(l=>`<span class="chip">${esc(l.trim())}</span>`).join('')}</div>${atts?`<div class="attachments">${atts}</div>`:''}<iframe class="body-frame" sandbox="allow-same-origin" onload="resizeFrame(this)" src="${esc(m.body_url)}"></iframe>`;}
function bodySlot(m,open){return open?`<iframe class="body-frame" sandbox="allow-same-origin" onload="resizeFrame(this)" src="${esc(m.body_url)}"></iframe>`:`<button class="body-toggle" data-body-url="${esc(m.body_url)}">Open body</button>`;}
function bindBodyToggles(){document.querySelectorAll('.body-toggle').forEach(b=>b.onclick=()=>{b.outerHTML=`<iframe class="body-frame" sandbox="allow-same-origin" onload="resizeFrame(this)" src="${esc(b.dataset.bodyUrl)}"></iframe>`;});}
async function showConversation(id){setActiveRow(id); $('detail').innerHTML='<div class="empty">Loading conversation...</div>'; const c=await api('/api/conversation/'+encodeURIComponent(id)); const lastIndex=c.messages.length-1; const cards=c.messages.map((m,i)=>{const atts=m.attachments.map(a=>`<a class="att" href="/file/${encodeURIComponent(a.path)}" target="_blank">${esc(a.filename)} - ${a.size_mb} MB</a>`).join(''); return `<section class="message-card ${i===lastIndex?'latest':''}"><div class="message-head"><div class="kv"><b>From:</b> ${esc(m.from_display)}</div><div class="kv"><b>To:</b> ${esc(m.to_text)}</div><div class="kv"><b>Date:</b> ${esc(m.date)}</div><div class="kv"><b>Size:</b> ${m.size_mb} MB</div>${atts?`<div class="attachments">${atts}</div>`:''}</div><div class="message-body">${bodySlot(m,i===lastIndex)}</div></section>`;}).join(''); $('detail').innerHTML=`<h1>${esc(c.subject||'(no subject)')}</h1><div class="kv"><b>Conversation:</b> ${c.message_count} message(s)</div>${cards}`; bindBodyToggles();}
$('searchBtn').onclick=()=>{state.q=$('q').value.trim(); state.page=1; loadList(state.q?`search "${state.q}"`:'conversations');};
$('q').addEventListener('keydown',e=>{if(e.key==='Enter')$('searchBtn').click();});
$('sort').onchange=()=>{state.sort=$('sort').value; state.page=1; loadList('sorted conversations');};
$('dateFrom').onchange=()=>{state.dateFrom=$('dateFrom').value; state.page=1; loadList('date filter');}; $('dateTo').onchange=()=>{state.dateTo=$('dateTo').value; state.page=1; loadList('date filter');};
$('clearFilters').onclick=()=>{state.filters={}; state.q=''; state.dateFrom=''; state.dateTo=''; state.page=1; $('q').value=''; $('dateFrom').value=''; $('dateTo').value=''; renderScope(); loadList('all mail');};
$('pageJump').addEventListener('keydown',e=>{if(e.key==='Enter'){const page=Math.min(Math.max(parseInt($('pageJump').value||'1',10),1),state.pageCount); state.page=page; loadList('page '+page);}});
$('pageJump').addEventListener('change',()=>{const page=Math.min(Math.max(parseInt($('pageJump').value||'1',10),1),state.pageCount); state.page=page; loadList('page '+page);});
$('prev').onclick=()=>{if(state.page>1){state.page--;loadList('previous page');}}; $('next').onclick=()=>{if(state.page<state.pageCount){state.page++;loadList('next page');}};
function initResizers(){const root=document.documentElement; const savedNav=localStorage.getItem('gmailLocalNavW'); const savedList=localStorage.getItem('gmailLocalListW'); if(savedNav)root.style.setProperty('--nav-w',savedNav+'px'); if(savedList)root.style.setProperty('--list-w',savedList+'px'); document.querySelectorAll('.resizer').forEach(handle=>{handle.addEventListener('pointerdown',e=>{e.preventDefault(); handle.classList.add('dragging'); const type=handle.dataset.resize; const startX=e.clientX; const startNav=parseInt(getComputedStyle(root).getPropertyValue('--nav-w'),10); const startList=parseInt(getComputedStyle(root).getPropertyValue('--list-w'),10); handle.setPointerCapture(e.pointerId); const move=ev=>{if(type==='nav'){const w=Math.min(Math.max(startNav+ev.clientX-startX,180),420); root.style.setProperty('--nav-w',w+'px'); localStorage.setItem('gmailLocalNavW',w);}else{const w=Math.min(Math.max(startList+ev.clientX-startX,360),900); root.style.setProperty('--list-w',w+'px'); localStorage.setItem('gmailLocalListW',w);}}; const up=ev=>{handle.classList.remove('dragging'); handle.releasePointerCapture(ev.pointerId); handle.removeEventListener('pointermove',move); handle.removeEventListener('pointerup',up);}; handle.addEventListener('pointermove',move); handle.addEventListener('pointerup',up);});});}
async function init(){initResizers(); bindFilters(); renderScope(); $('messages').innerHTML='<div class="empty">Loading all mail...</div>'; await loadFacets(); renderScope(); await loadList('all mail');}
init();
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/messages":
            try:
                json_response(self, list_messages(parse_qs(parsed.query)))
            except sqlite3.OperationalError as exc:
                json_response(self, {"error": str(exc)}, 400)
            return

        if parsed.path == "/api/conversations":
            try:
                json_response(self, list_conversations(parse_qs(parsed.query)))
            except sqlite3.OperationalError as exc:
                json_response(self, {"error": str(exc)}, 400)
            return

        if parsed.path == "/api/facets":
            json_response(self, facets())
            return

        if parsed.path.startswith("/api/message/"):
            message_id = int(parsed.path.rsplit("/", 1)[-1])
            message = message_detail(message_id)
            json_response(self, message if message else {"error": "not found"}, 200 if message else 404)
            return

        if parsed.path.startswith("/api/conversation/"):
            conversation_id = unquote(parsed.path[len("/api/conversation/") :])
            conversation = conversation_detail(conversation_id)
            json_response(self, conversation if conversation else {"error": "not found"}, 200 if conversation else 404)
            return

        if parsed.path.startswith("/body/"):
            try:
                message_id = int(parsed.path.rsplit("/", 1)[-1])
            except ValueError:
                self.send_error(404)
                return
            html_text = message_body_html(message_id)
            if html_text is None:
                self.send_error(404)
                return
            data = html_text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path.startswith("/file/"):
            rel = unquote(parsed.path[len("/file/") :])
            target = (DATA_DIR / rel).resolve()
            if not is_relative_to(target, DATA_DIR) or not target.exists():
                self.send_error(404)
                return
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if content_type == "text/html":
                data = rewrite_inline_cids(rel, target.read_text(encoding="utf-8", errors="replace")).encode("utf-8")
            else:
                data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type if content_type != "text/html" else "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_error(404)


def main():
    global READONLY_DB
    if not DB_PATH.exists():
        raise SystemExit(f"Missing database: {DB_PATH}")
    if os.environ.get("GMAIL_VIEWER_READONLY", "").strip() == "1":
        READONLY_DB = True
        print("Using read-only immutable SQLite mode.")
    else:
        try:
            ensure_performance_schema()
        except sqlite3.OperationalError as exc:
            if "disk i/o error" not in str(exc).lower() or not schema_ready_readonly():
                raise
            READONLY_DB = True
            print("SQLite write/open path failed on this drive; using read-only immutable mode.")
    port = int(os.environ.get("GMAIL_VIEWER_PORT") or find_port())
    server = ThreadingHTTPServer((HOST, port), Handler)
    url = f"http://{HOST}:{port}/"
    print(f"Gmail Takeout Viewer running at {url}")
    print(f"Data directory: {DATA_DIR}")
    print("Press Ctrl+C to stop.")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
