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
# A dataset row whose source starts with this was taken from a search-engine result that
# quotes the page, not from the page itself (e.g. "search:https://www.opendatalab.mn/search/<reg>").
# It never counts as the registry (or mse.mn) reporting the edge; only the import reading the page does.
UNVERIFIED_PREFIX = "search:"
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
            # idempotent, so it also runs for unchanged datasets: cleans edges an earlier load left
            drop_unverified_twins(g, csv_io.read_dir(d))
            out.append({"dataset": d.name, "status": "unchanged"})
            continue
        ds = csv_io.read_dir(d)
        g.drop_demo_data()
        loaded = g.load(ds)
        g.mark_baseline(loaded["run"])
        removed = drop_retracted(g, ds)
        drop_unverified_twins(g, ds)
        missing = sorted(g.missing_tickers(_referenced(ds) - _own(ds)))
        if missing:
            out.append({"dataset": d.name, "status": "partial", "missing_companies": missing})
        else:
            g.mark_manual(d.name, digest)
            out.append({"dataset": d.name, "status": "loaded", **ds.summary(),
                        **({"retracted": removed} if removed else {})})
    return out


def drop_retracted(g, ds) -> int:
    """Delete the records an earlier version of the dataset added and this one takes back.

    A record counts as the dataset's own when every source it cites is a source the
    dataset uses and none is mse.mn or the registry: those keep their own edges, and
    a scrape or a registry page closes them in the usual way. Retracted research is
    deleted rather than closed, because it was never true, so it is no history.
    Only current edges into the dataset's companies are looked at.
    """
    from .graph import company_key
    from .normalize import clean, entity_key, is_aggregate, post_key

    g.canonicalize(ds)
    alias = {r["a"]: r["key"] for r in g.run("MATCH (c:Company) UNWIND c.aliases AS a RETURN a, c.key AS key")}
    tkeys = g.company_keys()
    owns, posts = set(), set()
    for h in ds.shareholdings:
        name = clean(h.holder_name)
        if not name or is_aggregate(name):
            continue
        if h.holder_ticker:
            key = tkeys.get(h.holder_ticker.upper()) or company_key(h.holder_ticker)
        else:
            k = entity_key(name, h.holder_type)
            key = alias.get(k) or alias.get(entity_key(name)) or k
        owns.add((h.company_ticker.upper(), key))
    for p in ds.positions:
        if not p.until:
            posts.add((p.company_ticker.upper(), entity_key(p.person_name, "person"), post_key(p.title, p.role)))
    sources = {x.source for x in ds.shareholdings + ds.positions if x.source}
    rows = g.run(
        """
        MATCH (e:Entity)-[r:OWNS|HOLDS_POSITION {current: true}]->(c:Company)
        WHERE c.ticker IN $tickers
        RETURN elementId(r) AS id, type(r) AS rel, e.key AS key, c.ticker AS ticker, r.post AS post,
               coalesce(r.sources, [x IN [r.source] WHERE x IS NOT NULL]) AS sources
        """, tickers=sorted(_own(ds) | _referenced(ds)))
    stale = []
    for r in rows:
        srcs = [x for x in r["sources"] if x]
        if not srcs or not set(srcs) <= sources or _has(srcs, (REGISTRY_HOST,) + MSE_HOSTS):
            continue
        listed = ((r["ticker"], r["key"]) in owns if r["rel"] == "OWNS"
                  else (r["ticker"], r["key"], r["post"]) in posts)
        if not listed:
            stale.append(r["id"])
    if stale:
        g.run("MATCH ()-[r]->() WHERE elementId(r) IN $ids DELETE r", ids=stale)
    return len(stale)


def drop_unverified_twins(g, ds) -> int:
    """Remove the bare page URL that an earlier version of a `search:` row left on its edges.

    When a row's source gains the `search:` prefix, its edges still carry the old bare
    URL, which is the same string the registry import writes. That would make them look
    registry-confirmed. The bare URL is removed only from companies the import hasn't read
    yet (no `registry_url`), so no real registry evidence can be lost; once the page is read,
    the import adds the URL back to the edges it really reports.
    """
    g.canonicalize(ds)
    twins = sorted({x.source[len(UNVERIFIED_PREFIX):]
                    for x in ds.shareholdings + ds.positions + ds.beneficial_owners
                    if (x.source or "").startswith(UNVERIFIED_PREFIX)})
    if not twins:
        return 0
    rows = g.run(
        """
        MATCH (:Entity)-[r:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER]->(c:Company)
        WHERE c.ticker IN $tickers AND coalesce(c.registry_url, '') = ''
          AND (r.source IN $twins OR any(x IN coalesce(r.sources, []) WHERE x IN $twins))
        SET r.source = CASE WHEN r.source IN $twins THEN $prefix + r.source ELSE r.source END
        SET r.sources = [x IN coalesce(r.sources, []) WHERE NOT x IN $twins]
        SET r.sources = CASE WHEN r.source IS NULL OR r.source IN r.sources THEN r.sources
                             ELSE r.sources + r.source END
        RETURN count(r) AS n
        """, tickers=sorted(_own(ds) | _referenced(ds)), twins=twins, prefix=UNVERIFIED_PREFIX)
    return rows[0]["n"] if rows else 0


def _own(ds) -> set[str]:
    return {c.ticker.upper() for c in ds.companies}


def _referenced(ds) -> set[str]:
    return ({x.company_ticker.upper() for x in ds.shareholdings + ds.positions + ds.beneficial_owners}
            | {f.ticker.upper() for f in ds.facts})


def registry_tickers(root: str | Path | None = None) -> set[str]:
    """Tickers of every company a manual dataset describes or refers to (e.g. APU)."""
    out: set[str] = set()
    for d in csv_io.manual_dirs(_root(root)):
        ds = csv_io.read_dir(d)
        out |= _own(ds) | _referenced(ds)
    return out


def with_registry_tickers(selected: list[tuple[str, str]], everything: list[tuple[str, str]],
                          root: str | Path | None = None) -> list[tuple[str, str]]:
    """`selected` (e.g. today's shard of `everything`) plus the manual datasets' companies.
    A dataset's `~<register no>` also picks the company that number was merged into."""
    extra = registry_tickers(root)
    have = {t for t, _ in selected}
    return selected + [c for c in everything
                       if (c[0] in extra or (c[1] and "~" + c[1] in extra)) and c[0] not in have]


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
       (coalesce(c.register_no, '') <> '' AND d.register_no = c.register_no) OR
       any(a IN coalesce(c.aliases, []) WHERE a IN coalesce(d.aliases, [])))
RETURN c.ticker AS ticker, c.name AS name, c.register_no AS reg_no, c.registry_url AS registry_url,
       c.registry_name AS registry_name, c.registry_updated AS registry_updated,
       owners, officers, collect(DISTINCT d.ticker) AS duplicates
"""


def _has(sources: list[str], hosts) -> bool:
    hosts = (hosts,) if isinstance(hosts, str) else hosts
    return any(h in (s or "") for s in sources if not (s or "").startswith(UNVERIFIED_PREFIX) for h in hosts)


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
        g.canonicalize(ds)       # e.g. a `~<register no>` merged into the listed company it is
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


# ---- group people → their other companies ----------------------------------------

_GROUP = """
MATCH (seed:Company) WHERE seed.ticker IN $tickers
OPTIONAL MATCH (seed)-[:OWNS*1..3 {current: true}]->(sub:Company)
WITH collect(DISTINCT seed) + collect(DISTINCT sub) AS cs
UNWIND cs AS c
RETURN collect(DISTINCT c.ticker) AS tickers
"""

_PEOPLE = """
MATCH (p:Person)-[r:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER {current: true}]->(c:Company)
WHERE c.ticker IN $group
WITH p, collect(DISTINCT {ticker: c.ticker, name: c.name, rel: type(r), title: r.title,
                          percent: r.percent}) AS inside
OPTIONAL MATCH (p)-[o:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER]->(x:Company)
WHERE NOT x.ticker IN $group AND ($past OR o.current)
WITH p, inside, collect(DISTINCT CASE WHEN x IS NULL THEN null ELSE
       {ticker: x.ticker, name: x.name, reg_no: x.register_no, listed: coalesce(x.listed, true),
        rel: type(o), title: o.title, percent: o.percent, current: o.current,
        sources: coalesce(o.sources, [s IN [o.source] WHERE s IS NOT NULL])} END) AS elsewhere
RETURN p.key AS key, p.name AS name, inside, elsewhere
ORDER BY size(elsewhere) DESC, p.name
"""

# owners named with a registration number in the group's beneficial-owner chains whose
# page the graph doesn't have yet: what odl-discover reads next for this group
_LEADS = """
MATCH (e:Entity)-[b:BENEFICIAL_OWNER {current: true}]->(c:Company)
WHERE c.ticker IN $group AND b.owner_reg IS NOT NULL
  AND NOT EXISTS { MATCH (k:Company) WHERE k.register_no = b.owner_reg }
RETURN DISTINCT b.owner_reg AS reg_no, e.name AS owner, c.ticker AS via
ORDER BY reg_no
"""


def group_people(g, root: str | Path | None = None, dataset: str | None = None,
                 past: bool = False) -> list[dict]:
    """For each hand-curated group (data/manual/<name>): the people in its companies and the
    *other* companies the graph links them to, plus the registry numbers still to read.

    The group is the dataset's companies plus what they own (current stakes, up to three
    steps), so the subsidiaries discovery added from the registry count as the group's.
    People are matched by their merge key (`normalize.entity_key`: "Т.Гантөмөр" ==
    "Тардай Гантөмөр"), so a namesake can show up as the same person: check the sources.

    Reads only the graph. Open Data Lab can't be searched by name (robots.txt), so a
    person's other companies appear here once their registry pages are in the graph:
    through discovery (beneficial-owner chains, data/discovery/seeds.csv) or the import.
    """
    out = []
    for d in csv_io.manual_dirs(_root(root)):
        if dataset and d.name != dataset:
            continue
        ds = csv_io.read_dir(d)
        g.canonicalize(ds)
        seeds = sorted(_own(ds) | _referenced(ds))
        rows = g.run(_GROUP, tickers=seeds)
        group = sorted(set(rows[0]["tickers"] if rows else []) | set(seeds))
        people = [{**r, "elsewhere": sorted(r["elsewhere"], key=lambda x: (not x["current"], x["ticker"]))}
                  for r in g.run(_PEOPLE, group=group, past=past)]
        others = {x["ticker"] for p in people for x in p["elsewhere"]}
        out.append({
            "dataset": d.name,
            "group_companies": len(group),
            "people": len(people),
            "people_elsewhere": sum(1 for p in people if p["elsewhere"]),
            "other_companies": sorted(others),
            "registry_leads": g.run(_LEADS, group=group),
            "details": people,
        })
    return out
