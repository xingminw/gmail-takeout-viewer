# Gmail Takeout Archive Builder

[English README](README.md)

这个仓库的职责很简单：把 Gmail Takeout 导出的 `.mbox` 文件转换成一个 portable、self-sufficient 的本地邮件归档文件夹。

这个仓库本身不是邮件归档，也不是日常双击启动的目录。生成出来的输出文件夹会包含自己的 app 副本、启动器、SQLite 索引、源 MBOX 副本、附件、报告和日志。生成后，这个文件夹可以移动，也可以在没有本仓库的情况下单独打开。打开生成后的归档仍然要求那台机器上有 Python 3.9 或更新版本。

## 基本用法

从 Gmail Takeout MBOX 生成归档：

```sh
python -B tools/build_archive.py "/path/to/all-mail.mbox" --out "/path/to/MailArchive" --rebuild
```

生成后的目录结构：

```text
MailArchive/
  .mail-archive-builder.json  用于安全重建的 marker
  Start Mail Viewer.command   macOS 双击启动器
  Start Mail Viewer.sh        macOS/Linux 终端启动器
  Start Mail Viewer.bat       Windows 启动器
  app/                        复制进去的 viewer/importer 代码
  data/                       SQLite 索引、报告、附件 blobs
  source/                     复制进去的源 MBOX
  logs/                       导入日志
```

viewer 只在本机 `127.0.0.1` 运行，并在本地浏览器打开。它不是公网网站。

## 小样例

仓库里包含一个假的 10 封邮件 MBOX，以及由它生成出来的小型归档：

```text
examples/sample_10.mbox
examples/sample_archive/
```

重新生成这个样例归档：

```sh
python -B tools/build_archive.py examples/sample_10.mbox --out examples/sample_archive --rebuild --limit 10
```

macOS 上可以双击 `examples/sample_archive/Start Mail Viewer.command`，也可以运行：

```sh
sh examples/sample_archive/Start\ Mail\ Viewer.sh
```

仓库里保留的 sample output 本身就是完整独立的归档：

```text
examples/sample_archive/
  .mail-archive-builder.json
  Start Mail Viewer.command
  Start Mail Viewer.sh
  Start Mail Viewer.bat
  app/
    app.py
    import_mbox.py
    analyze_mbox_stats.py
    portable_launch.py
  data/
    gmail_index.sqlite
    reports/import_summary.json
  logs/
    import.log
  source/
    sample_10.mbox
```

![Sample archive viewer](assets/example-viewer-list.png)

## 常用选项

只导入前 N 封邮件：

```sh
python -B tools/build_archive.py input.mbox --out MailArchive --rebuild --limit 100
```

使用默认的 compact storage：

```sh
python -B tools/build_archive.py input.mbox --out MailArchive --rebuild --storage compact
```

使用旧的每封邮件一个文件夹布局：

```sh
python -B tools/build_archive.py input.mbox --out MailArchive --rebuild --storage legacy
```

如果需要更底层的修复或断点续导，可以直接用 importer：

```sh
python -B viewer/import_mbox.py input.mbox --out-dir MailArchive/data --resume --progress 1000 --commit-every 500
```

## 仓库边界

这个仓库包含 builder、importer、会被复制到生成归档里的 viewer 源码、测试和样例。

这个仓库不应该包含真实邮件数据。真实 Gmail 导出、私有生成归档、SQLite 数据库、附件和本地配置默认都会被忽略。被跟踪的 `.mbox` 只应该是 `examples/` 下面的假样例。

主要源码：

```text
tools/build_archive.py       主程序：从 MBOX 生成独立归档文件夹
tools/archive_templates/     会被复制进归档的启动器和 portable launch 模板
viewer/app.py                会被复制到生成归档里的 app/app.py
viewer/import_mbox.py        会被复制到生成归档里，并负责导入邮件
viewer/analyze_mbox_stats.py 只读 header 的 MBOX 统计辅助工具
examples/                    假样例输入和生成出来的小样例归档
tests/                       端到端测试和 importer 测试
```

## 开发测试

运行端到端测试：

```sh
python -m unittest tests.test_compact_archive tests.test_build_archive_e2e
```

端到端测试会从 `examples/sample_10.mbox` 生成一个独立归档，启动生成出来的 localhost app，并验证浏览器页面和 conversation API 可用。
