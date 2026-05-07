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
DB_PATH = APP_DIR / "gmail_index.sqlite"
MESSAGES_DIR = APP_DIR / "messages"
HOST = "127.0.0.1"
CONFIG_PATH = APP_DIR / "config.json"


def load_config():
    if not CONFIG_PATH.exists():
        return {"account_emails": []}
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


CONFIG = load_config()
ACCOUNT_EMAILS = {email.lower() for email in CONFIG.get("account_emails", [])}


def find_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def json_response(handler, payload, status=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_sql(sql, params=()):
    with db() as conn:
        return [dict(row) for row in conn.execute(sql, params)]


def one_sql(sql, params=()):
    with db() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None



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
    apply_search_query(q, clauses, values)

    label = params.get("label", [""])[0].strip()
    if label:
        clauses.append("m.labels LIKE ?")
        values.append(f"%{label}%")

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
        clauses.append("(LOWER(m.from_email) = ? OR LOWER(m.to_text) LIKE ?)")
        values.extend([user, f"%{user}%"])

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


def list_conversations(params):
    page = max(int(params.get("page", ["1"])[0] or "1"), 1)
    page_size = min(max(int(params.get("page_size", ["50"])[0] or "50"), 10), 100)
    offset = (page - 1) * page_size
    sort = params.get("sort", ["date_desc"])[0]
    order_by = {
        "date_asc": "g.latest_date ASC",
        "date_desc": "g.latest_date DESC",
        "size_desc": "g.total_size_bytes DESC",
        "size_asc": "g.total_size_bytes ASC",
        "sender": "latest.from_email ASC, g.latest_date DESC",
        "subject": "latest.subject ASC, g.latest_date DESC",
    }.get(sort, "g.latest_date DESC")

    where, values = build_where(params)
    base = f"""
        WITH filtered AS (
          SELECT m.*,
                 COALESCE(NULLIF(m.thread_key, ''), 'message:' || m.id) AS conversation_id
          FROM messages m
          {where}
        ),
        grouped AS (
          SELECT conversation_id,
                 count(*) AS message_count,
                 max(date) AS latest_date,
                 sum(size_bytes) AS total_size_bytes,
                 max(id) AS fallback_latest_id
          FROM filtered
          GROUP BY conversation_id
        ),
        latest AS (
          SELECT f.*
          FROM filtered f
          JOIN grouped g ON g.conversation_id = f.conversation_id
          WHERE f.id = (
            SELECT f2.id
            FROM filtered f2
            WHERE f2.conversation_id = f.conversation_id
            ORDER BY f2.date DESC, f2.id DESC
            LIMIT 1
          )
        )
    """
    rows = read_sql(
        base
        + f"""
        SELECT g.conversation_id, g.message_count, g.latest_date,
               round(g.total_size_bytes / 1024.0 / 1024.0, 3) AS total_size_mb,
               latest.id AS latest_message_id, latest.from_display, latest.from_email,
               latest.from_domain, latest.subject, latest.labels, latest.preview,
               (SELECT count(*) FROM attachments a JOIN filtered f ON f.id = a.message_id
                WHERE f.conversation_id = g.conversation_id) AS attachment_count
        FROM grouped g
        JOIN latest ON latest.conversation_id = g.conversation_id
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
        """,
        (*values, page_size, offset),
    )
    total = one_sql(
        f"""
        WITH filtered AS (
          SELECT m.*, COALESCE(NULLIF(m.thread_key, ''), 'message:' || m.id) AS conversation_id
          FROM messages m
          {where}
        )
        SELECT count(DISTINCT conversation_id) AS n FROM filtered
        """,
        values,
    )["n"]
    return {"rows": rows, "page": page, "page_size": page_size, "total": total}


def facets():
    labels = {}
    for row in read_sql("SELECT labels FROM messages WHERE labels <> ''"):
        for label in row["labels"].split(","):
            label = label.strip()
            if label:
                labels[label] = labels.get(label, 0) + 1
    self_emails = set(ACCOUNT_EMAILS)
    for row in read_sql("SELECT DISTINCT LOWER(from_email) AS email FROM messages WHERE labels LIKE '%Sent%' AND from_email <> ''"):
        self_emails.add(row["email"])
    users = {}
    for row in read_sql("SELECT from_email, to_text FROM messages"):
        message_users = set()
        from_email = (row.get("from_email") or "").strip().lower()
        if from_email:
            message_users.add(from_email)
        for _, address in getaddresses([row.get("to_text") or ""]):
            address = address.strip().lower()
            if address:
                message_users.add(address)
        for address in message_users:
            if address not in self_emails:
                users[address] = users.get(address, 0) + 1
    return {
        "years": read_sql(
            "SELECT year AS name, count(*) AS count FROM messages WHERE year <> '' GROUP BY year ORDER BY year DESC"
        ),
        "users": [
            {"name": name, "count": count}
            for name, count in sorted(users.items(), key=lambda item: item[1], reverse=True)[:40]
        ],
        "domains": read_sql(
            "SELECT from_domain AS name, count(*) AS count FROM messages WHERE from_domain <> '' GROUP BY from_domain ORDER BY count DESC LIMIT 25"
        ),
        "labels": [
            {"name": name, "count": count}
            for name, count in sorted(labels.items(), key=lambda item: item[1], reverse=True)
        ],
    }


def message_detail(message_id):
    message = one_sql("SELECT * FROM messages WHERE id = ?", (message_id,))
    if not message:
        return None
    message["attachments"] = read_sql(
        "SELECT filename,path,content_type,size_mb,size_bytes FROM attachments WHERE message_id = ? ORDER BY id",
        (message_id,),
    )
    return message


def conversation_detail(conversation_id):
    rows = read_sql(
        """
        SELECT * FROM messages
        WHERE COALESCE(NULLIF(thread_key, ''), 'message:' || id) = ?
        ORDER BY date ASC, id ASC
        """,
        (conversation_id,),
    )
    if not rows:
        return None
    for row in rows:
        row["attachments"] = read_sql(
            "SELECT filename,path,content_type,size_mb,size_bytes FROM attachments WHERE message_id = ? ORDER BY id",
            (row["id"],),
        )
    return {
        "conversation_id": conversation_id,
        "subject": rows[-1].get("subject") or rows[0].get("subject") or "(no subject)",
        "message_count": len(rows),
        "messages": rows,
    }


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

    raw_path = APP_DIR / "messages" / f"{message_id:06d}" / "raw.eml"
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
<title>Gmail Local SQLite Viewer</title>
<style>
:root{--bg:#f4f6f8;--panel:#fff;--line:#d9dee5;--text:#202124;--muted:#67727e;--accent:#0b57d0;--accent-bg:#e8f0fe;--chip:#eef2f6;--nav-w:250px;--list-w:520px}
*{box-sizing:border-box} html,body{height:100%;overflow:hidden} body{margin:0;font-family:Segoe UI,Arial,sans-serif;color:var(--text);background:var(--bg)}
.app{display:grid;grid-template-columns:var(--nav-w) 6px var(--list-w) 6px minmax(360px,1fr);height:100vh;overflow:hidden}
aside{border-right:1px solid var(--line);background:#fbfcfd;padding:16px 12px;overflow-y:auto;min-height:0}
.resizer{background:#eef1f4;cursor:col-resize;min-width:6px}.resizer:hover,.resizer.dragging{background:#c8d6ee}
.brand{font-size:21px;font-weight:650;margin:4px 10px 4px}.subbrand{font-size:12px;color:var(--muted);margin:0 10px 18px}
.filter{display:flex;justify-content:space-between;gap:8px;width:100%;border:0;background:transparent;text-align:left;padding:8px 10px;border-radius:18px;cursor:pointer;color:var(--text);font-size:14px}
.filter:hover,.filter.active{background:var(--accent-bg);color:#174ea6}.group{margin:20px 0 7px 10px;color:var(--muted);font-size:11px;text-transform:uppercase;font-weight:650;letter-spacing:.04em}
details{margin-top:12px}summary{cursor:pointer;list-style:none;margin:0 0 7px 10px;color:var(--muted);font-size:11px;text-transform:uppercase;font-weight:650;letter-spacing:.04em}summary::-webkit-details-marker{display:none}
.list{border-right:1px solid var(--line);background:var(--panel);display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden}
.toolbar{padding:12px;border-bottom:1px solid var(--line);display:grid;grid-template-columns:1fr 132px 126px 126px;gap:8px;align-items:center}
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
.att:hover{border-color:#9bbcf3;background:#f4f8ff}.body-frame{width:100%;min-height:520px;border:1px solid var(--line);border-radius:8px;background:#fff;overflow:hidden}.empty{padding:32px;color:var(--muted)}.small{font-size:12px;color:var(--muted)}
</style>
</head>
<body>
<div class="app">
  <aside>
    <div class="brand">Gmail Local</div>
    <div class="subbrand">SQLite sample viewer</div>
    <button class="filter active" data-type="" data-value="">All mail</button>
    <button class="filter" data-type="mailbox" data-value="inbox">Inbox</button>
    <button class="filter" data-type="mailbox" data-value="sent">Sent</button>
    <button class="filter" data-type="mailbox" data-value="important">Important</button>
    <button class="filter" data-type="mailbox" data-value="spam">Spam</button>
    <button class="filter" data-type="mailbox" data-value="trash">Trash</button>
    <button class="filter" data-type="attachments" data-value="1">Has attachments</button>
    <details open><summary>Years</summary><div id="years"></div></details>
    <details open><summary>Labels</summary><div id="labels"></div></details>
    <details><summary>Top users</summary><div id="users"></div></details>
    <details><summary>Top domains</summary><div id="domains"></div></details>
  </aside>
  <div class="resizer" data-resize="nav" title="Drag to resize sidebar"></div>
  <section class="list">
    <div class="toolbar">
      <input id="q" placeholder='Search, e.g. from:example.edu subject:review has:attachment older:2025-01-01'>
      <select id="sort">
        <option value="date_desc">Newest</option><option value="date_asc">Oldest</option>
        <option value="size_desc">Largest</option><option value="size_asc">Smallest</option>
        <option value="sender">Sender</option><option value="subject">Subject</option>
      </select>
      <input id="dateFrom" type="date" title="Start date">
      <input id="dateTo" type="date" title="End date">
    </div>
    <div id="messages" class="messages"></div>
    <div class="pager"><button id="prev">Prev</button><div class="page-jump"><span id="status"></span><input id="pageJump" type="number" min="1" value="1" title="Page"></div><button id="next">Next</button></div>
  </section>
  <div class="resizer" data-resize="list" title="Drag to resize message list"></div>
  <main id="detail" class="detail"><div class="empty">Select a message.</div></main>
</div>
<script>
let state={page:1,pageSize:50,sort:'date_desc',q:'',dateFrom:'',dateTo:'',filterType:'',filterValue:'',active:null,total:0,pageCount:1};
const $=id=>document.getElementById(id);
function esc(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function api(path){const r=await fetch(path); if(!r.ok) throw new Error(await r.text()); return await r.json();}
function query(){const p=new URLSearchParams({page:state.page,page_size:state.pageSize,sort:state.sort}); if(state.q)p.set('q',state.q); if(state.dateFrom)p.set('date_from',state.dateFrom); if(state.dateTo)p.set('date_to',state.dateTo); if(state.filterType)p.set(state.filterType,state.filterValue); return p;}
async function loadFacets(){const f=await api('/api/facets'); renderFacet('years',f.years,'year'); renderFacet('users',f.users,'user'); renderFacet('domains',f.domains,'domain'); renderFacet('labels',f.labels,'label'); bindFilters();}
function renderFacet(id,rows,type){const limit=id==='labels'?40:rows.length; const visible=rows.slice(0,limit); const more=rows.length>limit?`<div class="small" style="padding:6px 10px">${rows.length-limit} more hidden</div>`:''; $(id).innerHTML=visible.map(r=>`<button class="filter" data-type="${type}" data-value="${esc(r.name)}"><span>${esc(r.name)}</span><span class="small">${r.count}</span></button>`).join('')+more;}
function activateFilter(b,load=true){state.filterType=b.dataset.type||''; state.filterValue=b.dataset.value||''; state.page=1; document.querySelectorAll('.filter').forEach(x=>x.classList.toggle('active',x===b)); if(load)loadList();}
function bindFilters(){document.querySelectorAll('.filter').forEach(b=>b.onclick=()=>activateFilter(b));}
function activateDefaultFilter(){const b=document.querySelector('.filter[data-type=""][data-value=""]'); if(b)activateFilter(b,false);}
async function loadList(){const data=await api('/api/conversations?'+query().toString()); state.total=data.total; state.page=data.page; state.pageSize=data.page_size; state.pageCount=Math.max(1,Math.ceil(data.total/data.page_size)); const rows=data.rows; $('messages').innerHTML=rows.map(m=>conversationRow(m)).join('')||'<div class="empty">No messages.</div>'; document.querySelectorAll('.row').forEach(r=>r.onclick=()=>showConversation(r.dataset.id)); const start=data.total?(data.page-1)*data.page_size+1:0; const end=Math.min(data.page*data.page_size,data.total); $('status').textContent=data.total?`${start}-${end} of ${data.total} | Page ${data.page}/${state.pageCount}`:'0 messages'; $('pageJump').value=data.page; $('pageJump').max=state.pageCount; $('prev').disabled=state.page<=1; $('next').disabled=end>=data.total;}
function conversationRow(m){return `<div class="row ${m.conversation_id===state.active?'active':''}" data-id="${esc(m.conversation_id)}"><div class="subject">${esc(m.subject||'(no subject)')} ${m.message_count>1?`<span class="small">(${m.message_count})</span>`:''}</div><div class="meta">${esc(m.from_display)} - ${esc(m.latest_date)} - ${m.total_size_mb} MB</div><div class="preview">${esc(m.preview)}</div><div class="chips">${(m.labels||'').split(',').filter(Boolean).slice(0,5).map(l=>`<span class="chip">${esc(l.trim())}</span>`).join('')} ${m.attachment_count?`<span class="chip">${m.attachment_count} attachment(s)</span>`:''}</div></div>`;}
function resizeFrame(frame){try{const doc=frame.contentDocument||frame.contentWindow.document; const h=Math.max(520, doc.documentElement.scrollHeight, doc.body.scrollHeight); frame.style.height=(h+24)+'px';}catch(e){}}
function setActiveRow(id){state.active=id; document.querySelectorAll('.row').forEach(r=>r.classList.toggle('active', r.dataset.id===String(id)));}
async function show(id){setActiveRow(id); $('detail').innerHTML='<div class="empty">Loading message...</div>'; const m=await api('/api/message/'+id); const atts=m.attachments.map(a=>`<a class="att" href="/file/${encodeURIComponent(a.path)}" target="_blank">${esc(a.filename)} - ${a.size_mb} MB</a>`).join(''); $('detail').innerHTML=`<h1>${esc(m.subject||'(no subject)')}</h1><div class="kv"><b>From:</b> ${esc(m.from_display)}</div><div class="kv"><b>To:</b> ${esc(m.to_text)}</div><div class="kv"><b>Date:</b> ${esc(m.date)}</div><div class="kv"><b>Size:</b> ${m.size_mb} MB</div><div class="chips">${(m.labels||'').split(',').filter(Boolean).map(l=>`<span class="chip">${esc(l.trim())}</span>`).join('')}</div>${atts?`<div class="attachments">${atts}</div>`:''}<iframe class="body-frame" sandbox="allow-same-origin" onload="resizeFrame(this)" src="/file/${encodeURIComponent(m.body_html_path)}"></iframe>`;}
async function showConversation(id){setActiveRow(id); $('detail').innerHTML='<div class="empty">Loading conversation...</div>'; const c=await api('/api/conversation/'+encodeURIComponent(id)); const cards=c.messages.map(m=>{const atts=m.attachments.map(a=>`<a class="att" href="/file/${encodeURIComponent(a.path)}" target="_blank">${esc(a.filename)} - ${a.size_mb} MB</a>`).join(''); return `<section class="message-card"><div class="message-head"><div class="kv"><b>From:</b> ${esc(m.from_display)}</div><div class="kv"><b>To:</b> ${esc(m.to_text)}</div><div class="kv"><b>Date:</b> ${esc(m.date)}</div><div class="kv"><b>Size:</b> ${m.size_mb} MB</div>${atts?`<div class="attachments">${atts}</div>`:''}</div><div class="message-body"><iframe class="body-frame" sandbox="allow-same-origin" onload="resizeFrame(this)" src="/file/${encodeURIComponent(m.body_html_path)}"></iframe></div></section>`;}).join(''); $('detail').innerHTML=`<h1>${esc(c.subject||'(no subject)')}</h1><div class="kv"><b>Conversation:</b> ${c.message_count} message(s)</div>${cards}`;}
$('q').addEventListener('keydown',e=>{if(e.key==='Enter'){state.q=$('q').value.trim(); state.page=1; loadList();}});
$('sort').onchange=()=>{state.sort=$('sort').value; state.page=1; loadList();};
$('dateFrom').onchange=()=>{state.dateFrom=$('dateFrom').value; state.page=1; loadList();}; $('dateTo').onchange=()=>{state.dateTo=$('dateTo').value; state.page=1; loadList();};
$('pageJump').addEventListener('keydown',e=>{if(e.key==='Enter'){const page=Math.min(Math.max(parseInt($('pageJump').value||'1',10),1),state.pageCount); state.page=page; loadList();}});
$('pageJump').addEventListener('change',()=>{const page=Math.min(Math.max(parseInt($('pageJump').value||'1',10),1),state.pageCount); state.page=page; loadList();});
$('prev').onclick=()=>{if(state.page>1){state.page--;loadList();}}; $('next').onclick=()=>{if(state.page<state.pageCount){state.page++;loadList();}};
function initResizers(){const root=document.documentElement; const savedNav=localStorage.getItem('gmailLocalNavW'); const savedList=localStorage.getItem('gmailLocalListW'); if(savedNav)root.style.setProperty('--nav-w',savedNav+'px'); if(savedList)root.style.setProperty('--list-w',savedList+'px'); document.querySelectorAll('.resizer').forEach(handle=>{handle.addEventListener('pointerdown',e=>{e.preventDefault(); handle.classList.add('dragging'); const type=handle.dataset.resize; const startX=e.clientX; const startNav=parseInt(getComputedStyle(root).getPropertyValue('--nav-w'),10); const startList=parseInt(getComputedStyle(root).getPropertyValue('--list-w'),10); handle.setPointerCapture(e.pointerId); const move=ev=>{if(type==='nav'){const w=Math.min(Math.max(startNav+ev.clientX-startX,180),420); root.style.setProperty('--nav-w',w+'px'); localStorage.setItem('gmailLocalNavW',w);}else{const w=Math.min(Math.max(startList+ev.clientX-startX,360),900); root.style.setProperty('--list-w',w+'px'); localStorage.setItem('gmailLocalListW',w);}}; const up=ev=>{handle.classList.remove('dragging'); handle.releasePointerCapture(ev.pointerId); handle.removeEventListener('pointermove',move); handle.removeEventListener('pointerup',up);}; handle.addEventListener('pointermove',move); handle.addEventListener('pointerup',up);});});}
async function init(){initResizers(); bindFilters(); activateDefaultFilter(); $('messages').innerHTML='<div class="empty">Loading all mail...</div>'; await loadFacets(); activateDefaultFilter(); await loadList();}
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

        if parsed.path.startswith("/file/"):
            rel = unquote(parsed.path[len("/file/") :])
            target = (APP_DIR / rel).resolve()
            if not str(target).startswith(str(APP_DIR)) or not target.exists():
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
    if not DB_PATH.exists():
        raise SystemExit(f"Missing database: {DB_PATH}")
    port = int(os.environ.get("GMAIL_VIEWER_PORT") or find_port())
    server = ThreadingHTTPServer((HOST, port), Handler)
    url = f"http://{HOST}:{port}/"
    print(f"Gmail Local SQLite Viewer running at {url}")
    print("Press Ctrl+C to stop.")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    server.serve_forever()


if __name__ == "__main__":
    main()



