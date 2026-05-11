# Security and Privacy

Gmail Takeout Viewer is designed as a local-only tool for browsing a personal Gmail Takeout MBOX archive.

## Local server boundary

The viewer binds to `127.0.0.1` and is intended for use on the same machine. Do not expose the server directly to a network or the public internet.

## Data handling

The repository should contain only source code and documentation. Mail data, SQLite databases, attachments, raw `.eml` files, reports, exports, runtime folders, and local configuration are ignored by git.

Before publishing a fork or opening an issue, check that no generated archive data has been added:

```sh
git status --short
```

## Email HTML

Imported HTML is simplified and script-like content is stripped, but email HTML is still untrusted content. The viewer renders message bodies in sandboxed iframes and should be treated as a local archive viewer, not as a hardened multi-user webmail service.

## Attachments

Attachments are served from the configured local data directory only. Do not point the viewer at a directory that contains files you do not want accessible through the local web UI.

## Reporting issues

For security-sensitive reports, avoid attaching real mailbox files, screenshots with private content, or generated reports that include senders, subjects, domains, or message metadata. Use synthetic MBOX samples whenever possible.
