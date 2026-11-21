"""Hand-curated datasets kept in the repository under data/manual/ (e.g. Shunkhlai Group).

`load_manual` loads each dataset once, and again only when its files change.
`registry_tickers` names the companies the Open Data Lab registry import must
always cover, whatever the day's shard, so every run re-checks them.
`validate_manual` compares what a dataset says with what the registry (and
mse.mn) report for the same companies, as stored in the graph.

The registry is read through the normal import (`opendatalab.enrich_companies`),
so validation needs no requests of its own: it only reads the graph.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import csv_io

REGISTRY_HOST = "opendatalab.mn"
MSE_HOSTS = ("mse.mn",)


def _root(root: str | Path | None) -> Path:
    return Path(root or os.getenv("MANUAL_DATA_DIR") or csv_io.MANUAL_ROOT)


def load_manual(g, root: str | Path | None = None, force: bool = False) -> list[dict]:
    """Load the datasets under data/manual/ (or $MANUAL_DATA_DIR).

    Each one is loaded once, and again only when its files change: a researched
    stake dated earlier than mse.mn's would otherwise be re-applied on every run
    and flip-flop with the scrape. Its edges are a baseline, not changes to alert on.
    A dataset that refers to a listed company not in the graph yet (e.g. the
    scrape failed) is loaded but not marked, so the next run fills in the rest.
    """
    out = []
    for d in csv_io.manual_dirs(_root(root)):
        digest = csv_io.dir_digest(d)
        if not force and g.manual_digest(d.name) == digest:
            out.append({"dataset": d.name, "status": "unchanged"})
            continue
        ds = csv_io.read_dir(d)
        g.drop_demo_data()
        loaded = g.load(ds)
        g.mark_baseline(loaded["run"])
        missing = sorted(g.missing_tickers(_referenced(ds) - _own(ds)))
        if missing:
            out.append({"dataset": d.name, "status": "partial", "missing_companies": missing})
        else:
            g.mark_manual(d.name, digest)
            out.append({"dataset": d.name, "status": "loaded", **ds.summary()})
    return out


def _own(ds) -> set[str]:
    return {c.ticker.upper() for c in ds.companies}


def _referenced(ds) -> set[str]:
    return {x.company_ticker.upper() for x in ds.shareholdings + ds.positions + ds.beneficial_owners}


def registry_tickers(root: str | Path | None = None) -> set[str]:
    """Tickers of every company a manual dataset describes or refers to (e.g. APU)."""
    out: set[str] = set()
    for d in csv_io.manual_dirs(_root(root)):
        ds = csv_io.read_dir(d)
        out |= _own(ds) | _referenced(ds)
    return out


def with_registry_tickers(selected: list[tuple[str, str]], everything: list[tuple[str, str]],
                          root: str | Path | None = None) -> list[tuple[str, str]]:
    """`selected` (e.g. today's shard of `everything`) plus the manual datasets' companies."""
    extra = registry_tickers(root)
    have = {t for t, _ in selected}
    return selected + [c for c in everything if c[0] in extra and c[0] not in have]


# ---- validation ---------------------------------------------------------------

_EDGES = """
MATCH (c:Company {ticker: $ticker})
OPTIONAL MATCH (e:Entity)-[o:OWNS {current: true}]->(c)
WITH c, collect(CASE WHEN o IS NULL THEN null ELSE
       {key: e.key, name: e.name, percent: o.percent,
        sources: coalesce(o.sources, [x IN [o.source] WHERE x IS NOT NULL])} END) AS owners
OPTIONAL MATCH (p:Person)-[h:HOLDS_POSITION {current: true}]->(c)
WITH c, owners, collect(CASE WHEN h IS NULL THEN null ELSE
       {key: p.key, name: p.name, title: h.title,
        sources: coalesce(h.sources, [x IN [h.source] WHERE x IS NOT NULL])} END) AS officers
OPTIONAL MATCH (d:Company) WHERE d <> c AND (
       (coalesce(c.reg_no, '') <> '' AND d.reg_no = c.reg_no) OR
       any(a IN coalesce(c.aliases, []) WHERE a IN coalesce(d.aliases, [])))
RETURN c.ticker AS ticker, c.name AS name, c.reg_no AS reg_no, c.registry_url AS registry_url,
       c.registry_name AS registry_name, c.registry_updated AS registry_updated,
       owners, officers, collect(DISTINCT d.ticker) AS duplicates
"""


def _has(sources: list[str], hosts) -> bool:
    hosts = (hosts,) if isinstance(hosts, str) else hosts
    return any(h in (s or "") for s in sources for h in hosts)


def _compare(edges: list[dict], manual_sources: set[str], who) -> dict:
    """Split one company's current edges by who reports them: the dataset, the registry, mse.mn."""
    manual = [e for e in edges if manual_sources & set(e["sources"])]
    registry = [e for e in edges if _has(e["sources"], REGISTRY_HOST)]
    manual_keys = {e["key"] for e in manual}
    registry_keys = {e["key"] for e in registry}
    return {
        "confirmed_by_registry": sorted({who(e) for e in manual if e["key"] in registry_keys}),
        "not_in_registry": sorted({who(e) for e in manual if e["key"] not in registry_keys}),
        "registry_only": sorted({who(e) for e in registry if e["key"] not in manual_keys}),
        "confirmed_by_mse": sorted({who(e) for e in manual if _has(e["sources"], MSE_HOSTS)}),
    }


def validate_manual(g, root: str | Path | None = None) -> list[dict]:
    """For each dataset and company: which of its owners and officers the registry
    confirms, which it doesn't list, and what it lists that the dataset doesn't.

    A company counts as checked once the registry import has read its page
    (`registry_url` is set). Officers are compared per person, since the registry
    and the research name the same post differently. Only current records count.
    """
    report = []
    for d in csv_io.manual_dirs(_root(root)):
        ds = csv_io.read_dir(d)
        sources: dict[str, dict[str, set[str]]] = {}
        for h in ds.shareholdings:
            sources.setdefault(h.company_ticker.upper(), {}).setdefault("owners", set()).add(h.source)
        for p in ds.positions:
            if not p.until:
                sources.setdefault(p.company_ticker.upper(), {}).setdefault("officers", set()).add(p.source)
        companies = []
        for ticker in sorted(_own(ds) | _referenced(ds)):
            rows = g.run(_EDGES, ticker=ticker)
            if not rows:
                companies.append({"ticker": ticker, "status": "not in graph"})
                continue
            r = rows[0]
            owners = [e for e in r["owners"] if e]
            officers = [e for e in r["officers"] if e]
            src = sources.get(ticker, {})
            entry = {
                "ticker": ticker, "name": r["name"], "reg_no": r["reg_no"] or "",
                "status": ("checked" if r["registry_url"] else
                           "no registration number" if not r["reg_no"] else "registry not read yet"),
                "registry_url": r["registry_url"] or "",
                "owners": _compare(owners, src.get("owners", set()), lambda e: e["name"]),
                "officers": _compare(officers, src.get("officers", set()),
                                     lambda e: e["name"]),
            }
            if r["registry_name"]:
                entry["registry_name"] = r["registry_name"]
            if r["duplicates"]:
                entry["possible_duplicates"] = sorted(r["duplicates"])
            companies.append(entry)
        checked = [c for c in companies if c["status"] == "checked"]
        report.append({
            "dataset": d.name,
            "companies": len(companies),
            "checked": len(checked),
            "summary": {
                part: {k: sum(len(c[part][k]) for c in checked)
                       for k in ("confirmed_by_registry", "not_in_registry", "registry_only")}
                for part in ("owners", "officers")
            },
            "details": companies,
        })
    return report
