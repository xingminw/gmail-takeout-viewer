import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path


def clean(value):
    if value is None:
        return ""
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def parse_from(value):
    parsed = getaddresses([clean(value)])
    if not parsed:
        return "", "", ""
    name, addr = parsed[0]
    addr = addr.lower().strip()
    domain = addr.split("@", 1)[1] if "@" in addr else ""
    display = f"{name} <{addr}>" if name and addr else name or addr
    return display, addr, domain


def parse_date(value):
    value = clean(value)
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def add_labels(counter, size_counter, labels_text, size):
    for label in labels_text.split(","):
        label = label.strip()
        if label:
            counter[label] += 1
            size_counter[label] += size


def iter_header_rows(path, progress):
    parser = BytesParser(policy=policy.default)
    current_start = None
    header_lines = []
    parsed = None
    in_header = False
    index = 0

    with path.open("rb") as handle:
        while True:
            line_start = handle.tell()
            line = handle.readline()
            if not line:
                if current_start is not None and parsed is not None:
                    index += 1
                    yield index, handle.tell() - current_start, parsed
                break

            if line.startswith(b"From "):
                if current_start is not None and parsed is not None:
                    index += 1
                    yield index, line_start - current_start, parsed
                    if progress and index % progress == 0:
                        print(f"processed={index}", flush=True)
                current_start = line_start
                header_lines = []
                parsed = None
                in_header = True
                continue

            if current_start is None or not in_header:
                continue

            header_lines.append(line)
            if line in (b"\n", b"\r\n"):
                parsed = parser.parsebytes(b"".join(header_lines), headersonly=True)
                in_header = False


def top(counter, size_counter, limit):
    rows = []
    for key, count in counter.most_common(limit):
        rows.append(
            {
                "name": key,
                "count": count,
                "mb": round(size_counter[key] / 1024 / 1024, 2),
            }
        )
    return rows


def write_csv(path, rows):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="Analyze a Gmail Takeout MBOX without extracting bodies.")
    parser.add_argument("mbox", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("reports") / "mbox_stats")
    parser.add_argument("--progress", type=int, default=10000)
    parser.add_argument("--top", type=int, default=100)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    total_size = 0
    messages = 0
    min_date = None
    max_date = None
    by_year = Counter()
    by_year_size = Counter()
    labels = Counter()
    labels_size = Counter()
    domains = Counter()
    domains_size = Counter()
    senders = Counter()
    senders_size = Counter()
    largest = []

    for index, size, msg in iter_header_rows(args.mbox, args.progress):
        messages = index
        total_size += size
        date = parse_date(msg.get("Date"))
        year = str(date.year) if date else "(unknown)"
        if date:
            min_date = date if min_date is None or date < min_date else min_date
            max_date = date if max_date is None or date > max_date else max_date
        by_year[year] += 1
        by_year_size[year] += size

        display, email, domain = parse_from(msg.get("From"))
        sender = email or display or "(unknown)"
        domain = domain or "(unknown)"
        senders[sender] += 1
        senders_size[sender] += size
        domains[domain] += 1
        domains_size[domain] += size

        label_text = clean(msg.get("X-Gmail-Labels"))
        add_labels(labels, labels_size, label_text, size)

        largest.append(
            {
                "index": index,
                "mb": round(size / 1024 / 1024, 2),
                "date": date.isoformat(sep=" ", timespec="seconds") if date else "",
                "from": sender,
                "domain": domain,
                "subject": clean(msg.get("Subject")),
                "labels": label_text,
            }
        )
        largest.sort(key=lambda row: row["mb"], reverse=True)
        del largest[args.top :]

    summary = {
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "mbox": str(args.mbox),
        "messages": messages,
        "total_gb": round(total_size / 1024 / 1024 / 1024, 2),
        "avg_kb": round(total_size / max(messages, 1) / 1024, 1),
        "earliest_date": min_date.isoformat(sep=" ", timespec="seconds") if min_date else "",
        "latest_date": max_date.isoformat(sep=" ", timespec="seconds") if max_date else "",
        "top_labels": top(labels, labels_size, args.top),
        "top_domains_by_count": top(domains, domains_size, args.top),
        "top_senders_by_count": top(senders, senders_size, args.top),
        "top_domains_by_size": sorted(top(domains, domains_size, args.top), key=lambda row: row["mb"], reverse=True),
        "top_senders_by_size": sorted(top(senders, senders_size, args.top), key=lambda row: row["mb"], reverse=True),
        "by_year": sorted(top(by_year, by_year_size, 100), key=lambda row: row["name"]),
        "largest_messages": largest,
    }

    (args.out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(args.out_dir / "top_labels.csv", summary["top_labels"])
    write_csv(args.out_dir / "top_domains_by_size.csv", summary["top_domains_by_size"])
    write_csv(args.out_dir / "top_senders_by_size.csv", summary["top_senders_by_size"])
    write_csv(args.out_dir / "top_domains_by_count.csv", summary["top_domains_by_count"])
    write_csv(args.out_dir / "top_senders_by_count.csv", summary["top_senders_by_count"])
    write_csv(args.out_dir / "by_year.csv", summary["by_year"])
    write_csv(args.out_dir / "largest_messages.csv", summary["largest_messages"])

    print(json.dumps({k: summary[k] for k in ("messages", "total_gb", "avg_kb", "earliest_date", "latest_date")}, ensure_ascii=False, indent=2))
    print(f"out_dir={args.out_dir}")


if __name__ == "__main__":
    main()
