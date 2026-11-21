"""Split the companies into N stable slices, so a daily run can cover all of them over N days.

    --shard 3/7     slice 3 of 7 (0-based)
    --shard auto    today's slice of 7: Monday = 0 ... Sunday = 6, so the whole set is covered weekly

A company always lands in the same slice (CRC32 of its ticker or registration number), whatever
else is added to or removed from the list. A partial scrape is safe for the history: only
companies whose pages were all fetched can close edges (see Dataset.snapshots).
"""
from __future__ import annotations

import re
import zlib
from datetime import date


def parse(spec: str | None, today: date | None = None) -> tuple[int, int] | None:
    if not spec:
        return None
    spec = spec.strip().lower()
    if spec == "auto":
        return (today or date.today()).weekday(), 7
    m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", spec)
    if not m or not 0 <= int(m.group(1)) < int(m.group(2)):
        raise ValueError(f"bad shard {spec!r}: use K/N with 0 <= K < N, or 'auto'")
    return int(m.group(1)), int(m.group(2))


def slot(key: str, n: int) -> int:
    return zlib.crc32(key.strip().upper().encode("utf-8")) % n


def select(items: list, shard: tuple[int, int] | None, key=lambda x: x) -> list:
    if not shard:
        return items
    k, n = shard
    return [x for x in items if slot(key(x), n) == k]
