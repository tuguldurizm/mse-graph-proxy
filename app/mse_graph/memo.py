"""A short-lived memo for whole-market computations that the per-company views share.

Clicking a company asks for its flags, concentration, turnover and market panel. Each of those
used to recompute something for the whole market (every ownership loop, cross-holding, state
look-through, insider and competitor tie, the coverage rows of every listed company) just to pick
out one company's part. On Render's free plan (0.15 CPU) that made a click take many seconds.

`cached(g, name, fn, *args)` keeps such a result for `ANALYSIS_CACHE_SECONDS` (1800; 0 turns the
memo off). An entry is dropped as soon as this process writes to the graph through any `Graph`
(`Graph.generation` moves on every write query), and `clear()` drops everything (the pipeline
also calls it after a run). Writes made by another process (the daily job, which writes to
Aura directly) show up once the entry expires.

Callers must treat the cached values as read-only: they are shared between requests.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

_lock = threading.Lock()
_store: dict[tuple, tuple[int, float, Any]] = {}     # key -> (generation, stored at, value)
_key_locks: dict[tuple, threading.Lock] = {}


def ttl() -> float:
    try:
        return float(os.environ.get("ANALYSIS_CACHE_SECONDS", "1800"))
    except ValueError:
        return 1800.0


def clear() -> None:
    with _lock:
        _store.clear()


def _fresh(hit, gen: int, limit: float) -> bool:
    return hit is not None and hit[0] == gen and time.monotonic() - hit[1] < limit


def cached(g, name: str, fn: Callable[..., Any], *args) -> Any:
    """fn(g, *args), remembered per graph, name and arguments. Two requests that miss at the same
    time compute it once: the second waits for the first."""
    limit = ttl()
    if limit <= 0:
        return fn(g, *args)
    key = (id(g), name, args)
    gen = getattr(g, "generation", 0)
    hit = _store.get(key)
    if _fresh(hit, gen, limit):
        return hit[2]
    with _lock:
        klock = _key_locks.setdefault(key, threading.Lock())
    with klock:
        gen = getattr(g, "generation", 0)
        hit = _store.get(key)
        if _fresh(hit, gen, limit):
            return hit[2]
        value = fn(g, *args)
        # a write while it ran moves the generation on: store it under the older one, so the next
        # call computes it again
        with _lock:
            _store[key] = (gen, time.monotonic(), value)
        return value
