# Mail Backup Local Viewer

A local-only viewer for Gmail Takeout MBOX exports. It imports an MBOX into a SQLite index plus local message files, then serves a Gmail-like browser UI on `127.0.0.1`.

The repository contains only application code and documentation. Real mail data, SQLite indexes, attachments, raw `.eml` files, and local config are intentionally ignored by git.

## Requirements

- Python 3.9 or newer
- No pip packages are required

The app uses only Python standard-library modules.

## Quick Start

1. Copy the sample config:

```sh
cp config.example.json config.json
```

On Windows PowerShell:

```powershell
Copy-Item config.example.json config.json
```

2. Edit `config.json` and set your own account email addresses:

```json
{
  "account_emails": [
    "your-address@example.com"
  ]
}
```

3. Import an MBOX:

```sh
python -B import_mbox.py "/path/to/all-mail.mbox" --rebuild
```

On Windows:

```powershell
py -B import_mbox.py "C:\path\to\all-mail.mbox" --rebuild
```

Production import with progress, validation, and a final summary:

```powershell
py -B import_mbox.py "C:\path\to\all-mail.mbox" --rebuild --progress 1000 --commit-every 500
```

If an import is interrupted, continue from the largest message id already stored:

```powershell
py -B import_mbox.py "C:\path\to\all-mail.mbox" --resume --progress 1000 --commit-every 500
```

If a small number of messages failed and you want to repair only those MBOX indexes after fixing the importer:

```powershell
py -B import_mbox.py "C:\path\to\all-mail.mbox" --resume --only-indexes 35472,35475
```

For testing a larger sample without replacing the current data:

```powershell
py -B import_mbox.py "C:\path\to\all-mail.mbox" --out-dir ".\test_import_20000" --rebuild --limit 20000 --progress 2000
$env:GMAIL_VIEWER_DATA_DIR = ".\test_import_20000"
py -B app.py
```

Import reports are written under `reports/`, including `import_summary.json` and `import_errors.jsonl` when parsing errors occur.

4. Start the viewer.

On Windows, double-click:

```text
start.bat
```

On Windows with bundled portable Python, double-click:

```text
start_portable.bat
```

To install the bundled Windows Python runtime into `runtime/python-windows-x64`, run once:

```powershell
powershell -ExecutionPolicy Bypass -File tools\bootstrap_portable_windows.ps1
```

On macOS, double-click:

```text
start.command
```

If macOS says the file is not executable, run this once in Terminal from the project folder:

```sh
chmod +x start.command
```

On macOS or Linux from Terminal:

```sh
sh start.sh
```

Cross-platform Python entrypoint:

```sh
python -B start.py
```

The app starts a local web server on `127.0.0.1` using an automatically selected free port, then opens your browser. Press `Ctrl+C` to stop it.

To open data stored outside the source folder, set `GMAIL_VIEWER_DATA_DIR` before launching the app.

## Files

Tracked source files:

```text
app.py                 Local web app and browser UI
import_mbox.py         Imports a Gmail Takeout MBOX into the viewer format
start.bat              Windows launcher
start_portable.bat     Windows launcher using bundled runtime/python-windows-x64 when present
start.command          macOS double-click launcher
start.sh               macOS/Linux launcher
start.py               Cross-platform Python entrypoint
config.example.json    Example local account config
requirements.txt       Notes that no packages are required
tools/                 Helper scripts for building portable runtime folders
```

Generated local data, ignored by git:

```text
config.json
gmail_index.sqlite
gmail_index.sqlite-shm
gmail_index.sqlite-wal
messages/
runtime/
*.mbox
*.eml
```

## Features

- Conversation-based browsing
- Local search over subject, sender, recipients, labels, preview, and body text
- Sorting by date, size, sender, or subject, with optional date range filtering
- Filters for Inbox, Sent, Important, Spam, Trash, year, Gmail label, sender domain, and attachments
- Top users filter for frequent senders/recipients, excluding your configured account emails and inferred Sent-mail accounts
- Page jump controls for conversation results
- Resizable sidebar, conversation list, and message detail columns
- Indexed label filtering and optimized conversation list queries for larger archives
- HTML body display
- Extracted attachment links
- Raw message preservation as `raw.eml`

## Search Syntax

The search box supports plain keywords and a small Gmail-like operator set:

```text
review deadline
from:example.edu
to:your-address@example.com
subject:review
label:Important
category:Promotions
has:attachment
larger:10M
smaller:500K
older:2025-01-01
newer:2024-01-01
year:2026
```

Operators can be combined:

```text
from:example.edu subject:review has:attachment
category:Promotions older:2025-01-01
larger:5M invoice
```

This is not Gmail's full search language. It is a local SQLite-backed subset designed for browsing a Takeout archive.

## Data Model

SQLite stores searchable and sortable metadata:

```text
messages
attachments
message_labels
conversation_index
conversation_labels
message_users
messages_fts
```

Large display files stay on disk:

```text
messages/000001/body.html
messages/000001/attachments/...
messages/000001/raw.eml
```

This keeps the database small and makes attachments easy to open with normal desktop apps.

The app automatically creates or refreshes derived performance tables such as `message_labels`, `conversation_index`, `conversation_labels`, and `message_users` when opening an existing database. The first launch after an import may spend a short time building these indexes; later label, All Mail, and Top Users lists should be much faster. Plain keyword searches use SQLite FTS; Gmail-like operator searches use the local SQL subset described above.

## Conversations

MBOX stores individual messages. Gmail's conversation view is a UI grouping built from headers such as:

```text
Message-ID
In-Reply-To
References
Subject
```

The importer stores `in_reply_to`, `references_text`, and `thread_key`. Conversation mode groups messages by `thread_key` and opens all messages in that thread in chronological order. This approximates Gmail conversations, but Gmail's internal thread id is not included in a normal Takeout MBOX, so edge cases may differ from Gmail.

## Information Preservation

Keep the original MBOX as the source-of-truth backup.

The importer preserves enough data for local viewing and later export:

- searchable metadata in SQLite
- searchable plain text body in SQLite
- sanitized HTML body for display
- extracted attachments as files
- raw RFC 822 message bytes as `raw.eml`
- the original MBOX `From ` separator line in SQLite
- thread metadata from `In-Reply-To` and `References`

The viewer display is intentionally simplified and sanitized. It does not preserve the exact original MBOX byte stream as one file. With `raw.eml` plus the saved MBOX separator line, messages can be exported back into an MBOX-like file later, but the safest lossless backup remains the original `.mbox` file.

## Privacy

Do not commit generated data. The `.gitignore` is intentionally strict so mail data, attachments, SQLite indexes, raw `.eml` files, MBOX files, and local config stay out of the repository.
