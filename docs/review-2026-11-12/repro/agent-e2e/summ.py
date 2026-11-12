#!/usr/bin/env python3
"""Summarise http-trace.log: one row per (label, method, normalised path, status) with counts.
usage: summ.py LOG [--from HH:MM:SS] [--to HH:MM:SS] [--exclude HH:MM:SS-HH:MM:SS ...]"""
import re
import sys
from collections import Counter

log = sys.argv[1]
args = sys.argv[2:]
frm = to = None
excl = []
i = 0
while i < len(args):
    if args[i] == "--from":
        frm = args[i + 1]; i += 2
    elif args[i] == "--to":
        to = args[i + 1]; i += 2
    elif args[i] == "--exclude":
        a, b = args[i + 1].split("-"); excl.append((a, b)); i += 2
    else:
        i += 1

rows = Counter()
for line in open(log, encoding="utf-8"):
    m = re.match(r"(\d\d:\d\d:\d\d)\.\d+ (API|S3) (\S+) (\S+) req_cl=(\d+) -> (\d+)", line)
    if not m:
        continue
    t, label, method, path, cl, status = m.groups()
    if frm and t < frm or to and t > to or any(a <= t <= b for a, b in excl):
        continue
    path = re.sub(r"(dev|ses|ast|upl|evt|prj|org)_[0-9A-Z]{26}", lambda x: x.group(1) + "_<id>", path)
    if label == "S3":
        path = re.sub(r"^/(raw|proc)/.*?/(ORIGINAL|CLIP|PROXY)\?partNumber=\d+$", r"/\1/<key>/\2?partNumber=N", path)
    rows[(label, method, path, status)] += 1
for (label, method, path, status), n in sorted(rows.items()):
    print(f"{n:4d}  {label:3s} {method:6s} {path:70s} -> {status}")
print("status totals:", dict(Counter(k[3] for k, n in rows.items() for _ in range(n))))
