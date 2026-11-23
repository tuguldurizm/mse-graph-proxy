"""FRC (Financial Regulatory Commission, frc.mn): a first look at its data (issue #10, "More sources").

frc.mn is an Angular site. Its pages load everything from a JSON API on a separate port,
`https://www.frc.mn:5001/api/` (the site's `environment.apiUrl`), with the calls

    menu?site=main&lang=mn&status=1         the site's menu (every section)
    document?menuid=<id>&site=main&lang=mn  the documents filed under one menu (files, decisions)
    news?menuid=<id>&site=main&lang=mn&page=<n>   the posts under one menu
    document/get?id=<id>, news/get?id=<id>  one document or post

Claude Code cloud sessions can't reach that port (their proxy only carries HTTPS on 443), so this
module runs where the data is reachable: the hosted pipeline on Render (`frc_step` in
pipeline.py), or `python -m mse_graph.cli frc-probe` on a machine with open egress. For now it
only *describes* what the API returns, as `FRC layout` log lines (the menu tree, then the
listings of the menus whose titles look like disclosures: large holdings, insiders, issuers'
filings), so the parser for the disclosures can be written against real data. Nothing is stored
except the time of the last probe (`(:Meta {key: 'frc'})`).

frc.mn has no robots.txt. The probe is gentle anyway: one request at a time, FRC_DELAY (5 s)
apart, at most FRC_MAX_REQUESTS (15) per probe, at most once per FRC_MIN_INTERVAL_HOURS (20).
Source credit: Санхүүгийн зохицуулах хороо (Financial Regulatory Commission), frc.mn.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger(__name__)

API = os.getenv("FRC_API", "https://www.frc.mn:5001/api/")
SITE = {"site": "main", "lang": "mn"}

# The menus worth reading, most useful first (the first probe, 2026-11-23, listed FRC's 569 menus: no
# large-holding or insider section; decisions, violations, sanctions and licence holders are there).
TARGETS = (
    r"хорооны шийдвэр$",                       # the commission's decisions
    r"зөрчлийн мэдээлэл",                      # violations
    r"сахилгын шийтгэл",                       # disciplinary sanctions
    r"тусгай зөвшөөрөлтэй хуулийн этгээд",     # licensed legal entities
    r"тусгай зөвшөөрөл эзэмшигчид",            # licence holders
    r"үнэт цаасны компани",                    # securities firms (brokers, custodians: nominees, #39)
    r"хүчингүй болсон хуулийн этгээд",         # revoked licences, legal entities
    r"хувьцаа эзэмшигч|дотоод мэдээлэл|insider|large hold",   # in case such a section appears
)
DISCLOSURE = re.compile("|".join(f"(?:{t})" for t in TARGETS), re.I)
# bump to probe again at the next start, whatever FRC_MIN_INTERVAL_HOURS says
PROBE_VERSION = 2


def rank(title: str) -> int:
    """The position of the first TARGETS pattern the title matches (len(TARGETS) for none)."""
    return next((i for i, t in enumerate(TARGETS) if re.search(t, title or "", re.I)), len(TARGETS))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _client_get(timeout: float = 60.0) -> Callable[[str], str]:
    """A plain GET (direct, not through MSE_PROXY_URL: the fetch proxy only allows mse.mn and
    opendatalab.mn). Returns the body; raises on an HTTP error."""
    import httpx

    client = httpx.Client(timeout=httpx.Timeout(timeout, connect=20), follow_redirects=True,
                          headers={"User-Agent": "mse-graph/0.1 (research crawler)",
                                   "Content-Type": "application/json"})

    def get(url: str) -> str:
        r = client.get(url)
        r.raise_for_status()
        return r.text
    return get


def url(path: str, **params) -> str:
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{API.rstrip('/')}/{path}" + (f"?{q}" if q else "")


def records(obj, depth: int = 0) -> list[dict]:
    """The list of records in an API answer: the answer itself when it is a list, else the
    first list of objects found under its keys (`data`, `result`, `items`, ...)."""
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict) and depth < 3:
        for k in ("data", "result", "results", "items", "list", "rows", "value", "content"):
            if k in obj:
                found = records(obj[k], depth + 1)
                if found:
                    return found
        for v in obj.values():
            found = records(v, depth + 1)
            if found:
                return found
    return []


_ID = ("id", "ids", "_id", "menuid", "menuId", "menu_id")   # FRC's menu calls it `iDs`
_TITLE = ("title", "name", "menuname", "menuName", "text", "label", "caption")
_PARENT = ("parentid", "parentId", "parent_id", "parent", "pid")


def _first(rec: dict, names) -> object:
    low = {k.lower(): v for k, v in rec.items()}
    for n in names:
        if n.lower() in low and low[n.lower()] not in (None, ""):
            return low[n.lower()]
    return None


def menu_items(obj) -> list[dict]:
    """Every menu entry, children included, as {id, title, parent}."""
    out: list[dict] = []

    def walk(items, parent=None):
        for m in items:
            mid, title = _first(m, _ID), _first(m, _TITLE)
            out.append({"id": mid, "title": str(title or "").strip(), "parent": _first(m, _PARENT) or parent,
                        "kind": m.get("menutid"), "link": m.get("menulink") or ""})
            for k, v in m.items():
                if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                    walk(v, mid)
    walk(records(obj))
    return out


def describe(obj, what: str) -> None:
    """Log the shape of one API answer (`FRC layout`): its top keys, how many records, their
    keys and the first two, truncated."""
    recs = records(obj)
    top = list(obj)[:30] if isinstance(obj, dict) else f"list[{len(obj) if isinstance(obj, list) else '?'}]"
    keys = sorted({k for r in recs[:20] for k in r})[:50]
    log.info("FRC layout %s: top=%s records=%d keys=%s first=%s", what, top, len(recs), keys,
             json.dumps(recs[:2], ensure_ascii=False, default=str)[:1500])


def probe(get: Callable[[str], str] | None = None, max_requests: int | None = None,
          delay: float | None = None, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Read the menu, then the document and post listings of the menus that look like
    disclosures, logging each answer's layout. Returns a summary (counts, the chosen menus and
    any errors); never raises for an HTTP or JSON error."""
    get = get or _client_get()
    max_requests = int(max_requests if max_requests is not None else _env_float("FRC_MAX_REQUESTS", 15))
    delay = _env_float("FRC_DELAY", 5.0) if delay is None else delay
    report: dict = {"api": API, "requests": 0, "menus": 0, "candidates": [], "listings": {}, "errors": []}

    def fetch(path: str, **params):
        if report["requests"] >= max_requests:
            return None
        if report["requests"]:
            sleep(delay)
        report["requests"] += 1
        u = url(path, **params)
        try:
            return json.loads(get(u))
        except Exception as exc:          # an HTTP error, a timeout, not JSON: note it and go on
            report["errors"].append(f"{path}: {type(exc).__name__}: {str(exc)[:200]}")
            log.warning("FRC layout %s failed: %s", u, exc)
            return None

    menu = fetch("menu", **SITE, status=1)
    if menu is None:
        return report
    describe(menu, "menu")
    items = menu_items(menu)
    report["menus"] = len(items)
    for i in range(0, len(items), 25):                    # the whole tree, 25 entries a line
        log.info("FRC layout menu %d-%d: %s", i + 1, min(i + 25, len(items)),
                 json.dumps([[m["id"], m["parent"], m["title"][:60], m["kind"]] + ([m["link"][:60]] if m["link"] else [])
                             for m in items[i:i + 25]], ensure_ascii=False))
    cands = sorted((m for m in items if m["id"] is not None and DISCLOSURE.search(m["title"])),
                   key=lambda m: rank(m["title"]))
    report["candidates"] = [[m["id"], m["title"][:60], m["kind"], m["link"][:80]] for m in cands]
    for m in cands:
        if report["requests"] >= max_requests:
            break
        if m["link"]:                       # a link elsewhere (e.g. the licence registry): nothing to list here
            continue
        # a `list` menu holds posts, a `doc` / `file` menu documents; anything else: try both
        kinds = (("news", {"page": 0}),) if m["kind"] == "list" else \
                (("document", {}),) if m["kind"] in ("doc", "document", "file") else \
                (("document", {}), ("news", {"page": 0}))
        for kind, extra in kinds:
            ans = fetch(kind, menuid=m["id"], **SITE, **extra)
            if ans is None:
                continue
            n = len(records(ans))
            report["listings"][f"{kind}:{m['id']}"] = n
            if n:
                describe(ans, f"{kind} menu={m['id']} {m['title'][:40]!r}")
    log.info("FRC layout summary: %s", json.dumps(report, ensure_ascii=False)[:3000])
    return report


def hours_since_probe(g) -> float | None:
    """Hours since the last probe of this PROBE_VERSION (None: never, or an older version)."""
    rows = g.run("MATCH (m:Meta {key: 'frc'}) RETURN m.at AS at, m.version AS v")
    if not rows or not rows[0]["at"] or rows[0]["v"] != PROBE_VERSION:
        return None
    return (datetime.now(timezone.utc) - datetime.fromisoformat(rows[0]["at"])).total_seconds() / 3600


def mark_probe(g, report: dict) -> None:
    g.run("MERGE (m:Meta {key: 'frc'}) SET m.at = $at, m.version = $v, m.requests = $r, m.menus = $n, "
          "m.errors = size($e)", at=datetime.now(timezone.utc).isoformat(), v=PROBE_VERSION, r=report["requests"],
          n=report["menus"], e=report["errors"])
