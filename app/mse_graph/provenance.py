"""Where each relationship comes from, in a form the UI can show (issue #10, "Provenance in the UI").

Every OWNS / HOLDS_POSITION / BENEFICIAL_OWNER edge keeps `source` (the source that owns the
edge now) and `sources` (every source that reported it). They are raw strings:

    https://mse.mn/mn/company/...        mse.mn's company page (the main scrape)
    https://open.mse.mn/...              open.mse.mn
    https://www.opendatalab.mn/search/N  an Open Data Lab registry page
    opendatalab:<file>                   an Open Data Lab export loaded from a file
    search:<url>                         a search-engine result quoting <url> (data/manual, unverified)
    https://<other host>/...             a hand-curated dataset row (data/manual, data/mcs)
    DEMO-fictional                       the demo data
    mse.mn | opendatalab.mn | dataset:<host>   fact sources (#38)

`describe` turns one into {source, label, kind, url}; `annotate` adds `provenance` (the
owning source first, then the others) to a row of `Graph.entity`. Only http(s) URLs are
returned as links.
"""
from __future__ import annotations

from urllib.parse import urlparse

DEMO_SOURCE = "DEMO-fictional"            # graph.DEMO_SOURCE (kept here to avoid an import cycle)
UNVERIFIED_PREFIX = "search:"             # manual.UNVERIFIED_PREFIX
REGISTRY_FILE_PREFIX = "opendatalab:"     # opendatalab.import_rows on a file
DATASET_PREFIX = "dataset:"               # graph.company_row_source

# kind -> what the UI calls it
KINDS = {
    "mse": "MSE",
    "registry": "Open Data Lab",
    "registry_file": "Open Data Lab (файл)",
    "dataset": "Гараар цуглуулсан",
    "search": "Хайлтын үр дүн (шалгаагүй)",
    "demo": "Жишээ (зохиомол)",
    "other": "Бусад",
}


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _is_url(s: str) -> bool:
    return s.startswith(("http://", "https://")) and bool(_host(s))


def _kind_of_host(host: str) -> str:
    if host == "mse.mn" or host.endswith(".mse.mn"):
        return "mse"
    if host == "opendatalab.mn" or host.endswith(".opendatalab.mn"):
        return "registry"
    return "dataset"


def describe(source: str | None) -> dict | None:
    """{source, label, kind, url} for one raw source string; None for an empty one."""
    s = (source or "").strip()
    if not s:
        return None
    if s == DEMO_SOURCE:
        return {"source": s, "label": KINDS["demo"], "kind": "demo", "url": None}
    if s.startswith(UNVERIFIED_PREFIX):
        inner = s[len(UNVERIFIED_PREFIX):]
        url = inner if _is_url(inner) else None
        where = _host(inner) if url else inner
        return {"source": s, "label": f"{KINDS['search']}: {where}" if where else KINDS["search"],
                "kind": "search", "url": url}
    if s.startswith(REGISTRY_FILE_PREFIX):
        name = s[len(REGISTRY_FILE_PREFIX):]
        return {"source": s, "label": f"Open Data Lab ({name})" if name else KINDS["registry_file"],
                "kind": "registry_file", "url": None}
    if _is_url(s):
        host = _host(s)
        kind = _kind_of_host(host)
        label = KINDS[kind] if kind == "registry" else host     # mse.mn / open.mse.mn / sg.mn ...
        return {"source": s, "label": label, "kind": kind, "url": s}
    if s.startswith(DATASET_PREFIX):
        host = s[len(DATASET_PREFIX):]
        return {"source": s, "label": f"{KINDS['dataset']}: {host}" if host and host != "-" else KINDS["dataset"],
                "kind": "dataset", "url": None}
    if s in ("mse.mn", "open.mse.mn"):
        return {"source": s, "label": s, "kind": "mse", "url": None}
    if s == "opendatalab.mn":
        return {"source": s, "label": KINDS["registry"], "kind": "registry", "url": None}
    return {"source": s, "label": s, "kind": "other", "url": None}


def describe_all(source: str | None, sources: list | None = None) -> list[dict]:
    """The owning source first, then every other one that reported the edge, once each."""
    out, seen = [], set()
    for s in [source, *(sources or [])]:
        d = describe(s)
        if d and d["source"] not in seen:
            seen.add(d["source"])
            out.append(d)
    return out


def annotate(row: dict) -> dict:
    """Add `provenance` to an entity row that has `source` / `sources` (in place; returns it)."""
    row["provenance"] = describe_all(row.get("source"), row.get("sources"))
    return row
