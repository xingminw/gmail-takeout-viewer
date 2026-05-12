# Gmail Takeout Viewer

[English README](README.md)

Gmail Takeout Viewer 是一个本地运行的 Gmail Takeout MBOX 浏览器。它会把 Gmail 导出的 `.mbox` 文件导入到 SQLite 索引和本地归档文件中，然后在 `127.0.0.1` 启动一个类似 Gmail 的浏览界面。

这个仓库只应该包含代码和文档。真实邮件数据、SQLite 数据库、附件、原始 `.eml` 文件、本地配置和导出的归档数据都会被 `.gitignore` 排除。

![Gmail Takeout Viewer showing a filtered local archive](docs/assets/gmail-takeout-viewer-real-example.png)

## 项目状态

这个项目已经可以用于个人本地邮件归档浏览，目前定位是早期公开版本。请把它当成一个本地桌面工具，而不是多人在线 webmail 服务。

安全和隐私说明见 [SECURITY.md](SECURITY.md)。

## 环境要求

- Python 3.9 或更新版本
- 不需要额外 pip 依赖

这个 app 只使用 Python 标准库。

可选：以 editable 模式安装：

```sh
python -m pip install -e .
```

安装后会提供这些命令：

```text
gmail-takeout-import
gmail-takeout-viewer
gmail-takeout-stats
```

## 快速开始

1. 复制配置模板：

```sh
cp config.example.json config.json
```

Windows PowerShell：

```powershell
Copy-Item config.example.json config.json
```

2. 编辑 `config.json`，填入你自己的邮箱地址：

```json
{
  "account_emails": [
    "your-address@example.com"
  ],
  "top_user_include_patterns": [
    "%.edu"
  ],
  "top_user_exclude_patterns": [
    "noreply",
    "no-reply",
    "donotreply",
    "newsletter",
    "promo",
    "marketing",
    "offers",
    "rewards",
    "shop",
    "notification"
  ]
}
```

`account_emails` 用来判断 Sent/Received 行为，并且会把你自己的地址从 Top users 里隐藏。`top_user_include_patterns` 和 `top_user_exclude_patterns` 是 SQLite `LIKE` pattern，用来调整 Top users 列表。如果你想显示所有未被排除的联系人，可以删除 `top_user_include_patterns` 或设置为 `[]`。

3. 导入 MBOX。默认使用 compact storage：邮件 HTML 正文存到 SQLite，不为每封邮件复制 `raw.eml`，MBOX byte offset 会被记录，附件会去重后存到 `blobs/aa/bb/<sha256>.blob`：

```sh
python -B import_mbox.py "/path/to/all-mail.mbox" --rebuild
```

Windows：

```powershell
py -B import_mbox.py "C:\path\to\all-mail.mbox" --rebuild
```

正式导入时建议打开进度、校验和 summary：

```powershell
py -B import_mbox.py "C:\path\to\all-mail.mbox" --rebuild --progress 1000 --commit-every 500
```

如果想使用旧的“每封邮件一个文件夹”布局，加 `--storage legacy` 或 `--legacy`。Legacy 模式会写入 `messages/000001/body.html`、`messages/000001/raw.eml` 和每封邮件自己的附件文件。

如果导入中断，可以从已导入的最大 message id 继续：

```powershell
py -B import_mbox.py "C:\path\to\all-mail.mbox" --resume --progress 1000 --commit-every 500
```

如果只有少量 message 失败，修好 importer 后可以只修复指定 MBOX index：

```powershell
py -B import_mbox.py "C:\path\to\all-mail.mbox" --resume --only-indexes 35472,35475
```

如果想先测试一部分数据，不覆盖当前数据：

```powershell
py -B import_mbox.py "C:\path\to\all-mail.mbox" --out-dir ".\test_import_20000" --rebuild --limit 20000 --progress 2000
$env:GMAIL_VIEWER_DATA_DIR = ".\test_import_20000"
py -B app.py
```

导入报告会写入 `reports/`，包括 `import_summary.json`；如果解析失败，也会生成 `import_errors.jsonl`。

4. 启动浏览器界面。

Windows 双击：

```text
start.bat
```

Windows 使用内置 portable Python runtime 时，双击：

```text
start_portable.bat
```

第一次安装 Windows portable Python runtime：

```powershell
powershell -ExecutionPolicy Bypass -File tools\bootstrap_portable_windows.ps1
```

macOS 双击：

```text
start.command
```

如果 macOS 提示文件不可执行，在项目目录运行一次：

```sh
chmod +x start.command portable/launch.command start.sh portable/launch.sh
```

如果启动器打开后提示 `Missing database`，说明当前目录还没有 `gmail_index.sqlite`。请先导入 MBOX，或者启动前把 `GMAIL_VIEWER_DATA_DIR` 指向包含 `gmail_index.sqlite` 的数据目录。

macOS 启动器也会自动查找常见 portable 结构，比如 app 目录下的 `gmail_index.sqlite`、`data/gmail_index.sqlite`、`archive/gmail_index.sqlite`，或者同级的 `data/` 目录。

macOS 或 Linux 终端启动：

```sh
sh start.sh
```

跨平台 Python 入口：

```sh
python -B start.py
```

如果已经 `pip install -e .`：

```sh
gmail-takeout-viewer
```

Portable 相对路径启动入口：

```sh
python -B portable/launch.py --data-dir .
```

`portable/` 目录还包含 `launch.bat`、`launch.command` 和 `launch.sh`。它们会基于 app 所在目录解析路径，设置 `GMAIL_VIEWER_DATA_DIR`，启动本地 app，并让 `app.py` 打开浏览器。移动 portable archive 时，请把 app 文件夹、`gmail_index.sqlite` 和 `blobs/` 一起移动。

app 会在 `127.0.0.1` 上自动选择一个可用端口启动本地 web server，然后打开浏览器。按 `Ctrl+C` 停止服务。

如果数据不在源码目录，启动前设置 `GMAIL_VIEWER_DATA_DIR`。

## 文件结构

仓库内跟踪的源码文件：

```text
app.py                 本地 web app 和浏览器 UI
import_mbox.py         将 Gmail Takeout MBOX 导入 viewer 格式
analyze_mbox_stats.py  只读 header 的 MBOX 统计工具，不提取邮件正文
portable/              portable archive 使用的相对路径启动器
start.bat              Windows 启动器
start_portable.bat     使用 runtime/python-windows-x64 的 Windows 启动器
start.command          macOS 双击启动器
start.sh               macOS/Linux 启动器
start.py               跨平台 Python 启动入口
config.example.json    本地账号配置模板
requirements.txt       说明项目不需要额外依赖
tools/                 构建 portable runtime 的辅助脚本
```

本地生成并被 git 忽略的数据：

```text
config.json
gmail_index.sqlite
gmail_index.sqlite-shm
gmail_index.sqlite-wal
messages/
blobs/
runtime/
*.mbox
*.eml
```

## 功能

- 按 conversation 浏览邮件
- 搜索 subject、sender、recipients、labels、preview 和 body text
- 按 newest、oldest、largest 排序，并支持日期范围过滤
- 支持 Inbox、Sent、Important、Spam、Trash、year、Gmail label、sender domain、attachment 等筛选
- Top users 列表会统计常见联系人，并可以排除自己的账号和低价值发件人
- conversation 结果支持跳页
- sidebar、conversation list、message detail 三栏可调整宽度
- 为大归档构建索引，加速 conversation list、label、year、domain、Top users 等查询
- 显示 active filter chips，并支持逐个清除 filter
- 大查询加载时会显示 loading 反馈
- 支持 HTML 正文显示
- 支持附件链接
- 默认使用 compact storage，也可以通过 `--storage legacy` 保留 legacy `raw.eml` 文件布局

## 搜索语法

搜索框支持普通关键词，也支持一小部分 Gmail 风格 operator：

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

可以组合使用：

```text
from:example.edu subject:review has:attachment
category:Promotions older:2025-01-01
larger:5M invoice
```

这不是完整的 Gmail 搜索语言，而是一个基于本地 SQLite 的子集，用于浏览 Takeout 归档。

## 数据模型

SQLite 存储可搜索、可排序的元数据：

```text
messages
attachments
message_labels
conversation_index
conversation_labels
conversation_filters
message_users
messages_fts
```

默认的 compact mode 会把 searchable metadata、body text、display HTML 存入 SQLite，记录源 MBOX 的路径、byte offset 和 byte length，并将附件作为去重 blob 保存：

```text
gmail_index.sqlite
blobs/aa/bb/<sha256>.blob
```

Compact mode 不会为每封邮件创建 `messages/000001/` 目录，也不会复制每封邮件的 `raw.eml`。请保留原始 MBOX 作为真正的 source-of-truth backup；未来的 raw-message export 工具可以利用已记录的 MBOX offsets。

Legacy mode 会把较大的展示文件保存在磁盘：

```text
messages/000001/body.html
messages/000001/attachments/...
messages/000001/raw.eml
```

Legacy mode 会让数据库更小，也方便逐封查看附件文件，但会产生大量小文件。

app 打开已有数据库时，会自动创建或刷新派生性能表，比如 `message_labels`、`conversation_index`、`conversation_labels`、`conversation_filters` 和 `message_users`。导入后第一次启动可能需要花一些时间构建这些索引；之后 All Mail、label、year、domain、Top users 等列表会快很多。普通关键词搜索使用 SQLite FTS；Gmail 风格 operator 搜索使用上面描述的本地 SQL 子集。

## Conversations

MBOX 存的是单封邮件。Gmail 的 conversation view 是基于这些 header 做出的 UI 分组：

```text
Message-ID
In-Reply-To
References
Subject
```

Importer 会保存 `in_reply_to`、`references_text` 和 `thread_key`。Conversation mode 通过 `thread_key` 分组，并按时间顺序打开该 thread 中的所有邮件。这会近似 Gmail conversation，但普通 Takeout MBOX 不包含 Gmail 内部 thread id，所以少数边界情况可能和 Gmail 不完全一致。

## 信息保留

请保留原始 MBOX 作为 source-of-truth backup。

Importer 会保留足够用于本地浏览和后续导出的数据：

- SQLite 中的可搜索 metadata
- SQLite 中的可搜索 plain text body
- 用于显示的 sanitized HTML body，compact mode 存在 SQLite，legacy mode 存为 `body.html`
- compact mode 中去重保存的附件 blob，或 legacy mode 中逐封邮件保存的附件文件
- raw RFC 822 message bytes 只在 legacy mode 中保存为 `raw.eml`；compact mode 保存 MBOX source path、byte offset 和 byte length
- SQLite 中保存原始 MBOX `From ` separator line
- `In-Reply-To` 和 `References` thread metadata

viewer 的展示是简化且经过处理的。它不试图把原始 MBOX byte stream 原样拆成可还原的一组文件。Legacy mode 下可以利用 `raw.eml` 和保存的 MBOX separator line 重新导出类似 MBOX 的文件，但最安全的无损备份仍然是原始 `.mbox` 文件。

## 隐私

不要提交生成的数据。`.gitignore` 有意设置得比较严格，确保邮件数据、附件、SQLite 索引、原始 `.eml` 文件、MBOX 文件和本地配置不会进入仓库。
