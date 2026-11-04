"""접두사 + ULID 식별자 (structure.md 3장)."""

from __future__ import annotations

import re
import secrets
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")

PREFIXES = {
    "organization": "org", "project": "prj", "session": "ses", "device": "dev", "asset": "ast",
    "upload": "upl", "event": "evt", "share_request": "shr", "dataset": "dst", "dataset_version": "dsv",
    "access_request": "acr", "grant": "grt", "distribution": "dlv", "job": "job", "record": "rec",
    "report": "rpt", "webhook": "whk", "api_key": "key", "user": "usr", "audit": "aud",
    "agreement": "agr", "integration_card": "icd", "break_glass": "brk",
}


def new_ulid(now_ms: int | None = None) -> str:
    ts = int(time.time() * 1000) if now_ms is None else now_ms
    value = (ts & ((1 << 48) - 1)) << 80 | secrets.randbits(80)
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(out))


def new_id(kind: str) -> str:
    return f"{PREFIXES[kind]}_{new_ulid()}"


def is_ulid(value: str | None) -> bool:
    return bool(value) and bool(ULID_RE.match(value or ""))


def has_prefix(value: str, kind: str) -> bool:
    prefix = PREFIXES[kind] + "_"
    return value.startswith(prefix) and is_ulid(value[len(prefix):])
