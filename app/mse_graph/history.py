"""Change detection, alerts and ownership timelines, read from the graph's history.

The graph keeps every period of every edge (see graph.py): a load opens edges
for new holders and officers, and closes the ones that disappeared or whose
stake changed. This module turns those opened and closed edges back into a
list of changes, so the change list, alerts, the Atom feed and the ownership
timeline all come from Neo4j. Nothing is kept on disk, which matters on hosts
whose disk is wiped on restart.

    run_changes(g, run)       what one load changed (for alerts)
    recent_changes(g, ...)    changes on or after a date, newest first (API, feed, UI)
    ownership_timeline(g, t)  shareholder percentages of one company over time
    diff(old, new)            compare two datasets directly, without a graph

Change kinds
------------
company_added                         a company entered the graph (its first holders and
                                      officers are part of this, not listed one by one)
holder_added / holder_removed         a shareholder appeared in / disappeared from a company's table
stake_changed                         same holder, different percentage
position_added / position_removed     a board member / executive appeared / disappeared
position_changed                      same person, same company, new title
data_missing                          a company had shareholder (or officer) rows before and has none
                                      now. This is usually a failed scrape, not a real change, so it
                                      is never sent as an alert. The loader keeps the old edges open.
company_removed                       diff() only: the graph never deletes companies.
company_renamed                       a source gives the company another name (a dated fact, issue #38;
                                      the old name stays an alias)
company_fact_changed                  another value for the English name, address, legal form, sector
                                      or activity codes (`field`); shown, never sent as an alert
stake_accumulation                    a holder's stake rose over several consecutive periods (`steps`,
                                      from `old` to `new`, `crossed` / `approaching` thresholds;
                                      trends.py, issue #30). Comes with that last step's stake_changed
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict

from .models import Dataset
from .normalize import clean, entity_key, is_aggregate, parse_date, post_key, role_std

log = logging.getLogger(__name__)

# Ownership levels that matter for disclosure / control (percent).
THRESHOLDS = (5.0, 10.0, 20.0, 25.0, 33.33, 50.0, 75.0)
# Stake moves smaller than this (percentage points) are treated as rounding noise.
NOISE = 0.01


def source_label(source: str | None) -> str:
    """Short name of the source that recorded an edge."""
    s = (source or "").lower()
    if s.startswith("dataset:"):        # a company row of a dataset (graph.company_row_source)
        return "dataset"
    if "opendatalab" in s:
        return "opendatalab"
    if "mse.mn" in s:
        return "mse"
    if s.startswith("demo"):
        return "demo"
    return source or ""


# ---- diff --------------------------------------------------------------------

def _holders(ds: Dataset) -> dict[str, dict[str, dict]]:
    """ticker -> holder key -> {name, percent, shares, as_of}. Repeated rows are summed."""
    out: dict[str, dict[str, dict]] = {}
    for h in ds.shareholdings:
        name = clean(h.holder_name)
        if not name or is_aggregate(name):
            continue
        t = h.company_ticker.upper()
        k = entity_key(name)
        cur = out.setdefault(t, {}).get(k)
        if cur is None:
            out[t][k] = {"name": name, "percent": h.percent, "shares": h.shares, "as_of": parse_date(h.as_of)}
        else:
            if h.percent is not None:
                cur["percent"] = round((cur["percent"] or 0) + h.percent, 6)
            if h.shares is not None:
                cur["shares"] = (cur["shares"] or 0) + h.shares
    return out


def _positions(ds: Dataset) -> dict[str, dict[tuple[str, str], dict]]:
    """ticker -> (person key, post) -> {name, title, role, role_std}.

    Keyed by the seat (normalize.post_key), like the loader, so a respelt title
    is not a change."""
    out: dict[str, dict[tuple[str, str], dict]] = {}
    for p in ds.positions:
        name, title, role = clean(p.person_name), clean(p.title), p.role or "executive"
        if not name:
            continue
        out.setdefault(p.company_ticker.upper(), {})[(entity_key(name), post_key(title, role))] = {
            "name": name, "title": title, "role": role, "role_std": role_std(title, role)}
    return out


def crossed(old: float | None, new: float | None) -> list[float]:
    """Thresholds passed going from `old` to `new` percent (either direction)."""
    a, b = old or 0.0, new or 0.0
    lo, hi = min(a, b), max(a, b)
    return [t for t in THRESHOLDS if lo < t <= hi]


def diff(old: Dataset, new: Dataset, *, old_partial: bool = False, new_partial: bool = False) -> list[dict]:
    """Differences between two datasets of the same source."""
    changes: list[dict] = []
    names = {c.ticker.upper(): c.name for c in old.companies}
    names.update({c.ticker.upper(): c.name for c in new.companies})
    old_t = {c.ticker.upper() for c in old.companies}
    new_t = {c.ticker.upper() for c in new.companies}

    def add(kind, ticker, **kw):
        changes.append({"kind": kind, "ticker": ticker, "company": names.get(ticker, ticker), **kw})

    if not old_partial:      # against a partial list, "new" may just mean "not scraped before"
        for t in sorted(new_t - old_t):
            add("company_added", t)
    # A full scrape that suddenly finds far fewer companies is almost certainly broken.
    shrunk = bool(old_t) and len(new_t) < len(old_t) / 2
    if not (old_partial or new_partial or shrunk):
        for t in sorted(old_t - new_t):
            add("company_removed", t)
    if shrunk and not new_partial:
        log.warning("history: company list shrank from %d to %d; not reporting removals", len(old_t), len(new_t))

    # Tickers in only one company list are covered by company_added/removed (or are
    # outside a partial scrape). Tickers in neither list (sources with only
    # shareholder rows) are compared normally.
    skip = old_t ^ new_t

    oh, nh = _holders(old), _holders(new)
    for t in sorted(set(oh) | set(nh)):
        if t in skip:
            continue
        a, b = oh.get(t, {}), nh.get(t, {})
        if a and not b:
            add("data_missing", t, section="shareholders", count=len(a))
            continue
        for k in sorted(set(a) | set(b), key=lambda k: -((b.get(k) or a.get(k))["percent"] or 0)):
            x, y = a.get(k), b.get(k)
            if x and y:
                if x["percent"] is None or y["percent"] is None:
                    continue
                delta = round(y["percent"] - x["percent"], 4)
                if abs(delta) < NOISE:
                    continue
                add("stake_changed", t, entity=y["name"], entity_key=k, old=x["percent"], new=y["percent"],
                    delta=delta, crossed=crossed(x["percent"], y["percent"]),
                    as_of_old=x["as_of"], as_of_new=y["as_of"])
            elif y:
                add("holder_added", t, entity=y["name"], entity_key=k, old=None, new=y["percent"],
                    delta=y["percent"], crossed=crossed(None, y["percent"]), as_of_new=y["as_of"])
            else:
                add("holder_removed", t, entity=x["name"], entity_key=k, old=x["percent"], new=None,
                    delta=-x["percent"] if x["percent"] is not None else None,
                    crossed=crossed(x["percent"], None), as_of_old=x["as_of"])

    op, np_ = _positions(old), _positions(new)
    for t in sorted(set(op) | set(np_)):
        if t in skip:
            continue
        a, b = op.get(t, {}), np_.get(t, {})
        if a and not b:
            add("data_missing", t, section="officers", count=len(a))
            continue
        gone = {k: v for k, v in a.items() if k not in b}
        came = {k: v for k, v in b.items() if k not in a}
        # Same person, one title out and one title in -> a title change.
        for pk in sorted({k[0] for k in gone} & {k[0] for k in came}):
            g = [k for k in gone if k[0] == pk]
            c = [k for k in came if k[0] == pk]
            if len(g) == 1 and len(c) == 1:
                x, y = gone.pop(g[0]), came.pop(c[0])
                add("position_changed", t, entity=y["name"], entity_key=pk,
                    old=x["title"], new=y["title"], role=y["role"], role_std=y["role_std"])
        for (pk, _), y in sorted(came.items()):
            add("position_added", t, entity=y["name"], entity_key=pk, new=y["title"], role=y["role"],
                role_std=y["role_std"])
        for (pk, _), x in sorted(gone.items()):
            add("position_removed", t, entity=x["name"], entity_key=pk, old=x["title"], role=x["role"],
                role_std=x["role_std"])
    return changes


def is_significant(c: dict, min_percent: float | None = None) -> bool:
    """Worth an alert? Positions and company list changes always are. Stakes count
    when they move by `min_percent` points or more, or cross a threshold."""
    if min_percent is None:
        min_percent = float(os.getenv("ALERT_MIN_PERCENT", "1.0"))
    kind = c["kind"]
    if kind in ("data_missing", "company_fact_changed"):
        return False
    if kind.startswith("holder_") or kind == "stake_changed":
        if c.get("crossed"):
            return True
        if c.get("delta") is None:
            return kind != "stake_changed"   # added/removed holder with no % given
        return abs(c["delta"]) >= min_percent
    return True


# ---- changes from the graph ----------------------------------------------------

_EVENTS = """
MATCH (x:Entity)-[r:OWNS|HOLDS_POSITION]->(c:Company)
WHERE (r.closed_at IS NOT NULL OR r.current)          // not a past stint copied from a source
  AND ($ticker IS NULL OR c.ticker = $ticker) AND ($key IS NULL OR x.key = $key)
WITH x, r, c,
     coalesce(CASE WHEN $run IS NULL THEN r.first_seen >= $since ELSE r.opened_run = $run END, false) AS opened,
     coalesce(CASE WHEN $run IS NULL THEN r.closed_at >= $since ELSE r.closed_run = $run END, false) AS closed,
     // a new company's first holders and officers are one company_added, and what a
     // source reports the first time it covers a company is that source's baseline
     coalesce(r.baseline, false) OR coalesce(r.opened_run = c.first_run, r.first_seen = c.first_seen, false)
       AS initial
WITH x, r, c, opened AND NOT initial AS opened, closed
WHERE opened OR closed
RETURN type(r) AS rel, x.key AS key, x.name AS name, c.ticker AS ticker, c.name AS company,
       r.title AS title, r.role AS role, r.role_std AS role_std, r.post AS post, r.percent AS percent, r.shares AS shares, r.as_of AS as_of,
       r.source AS source, r.first_seen AS first_seen, r.last_seen AS last_seen, r.closed_at AS closed_at,
       opened, closed
"""

_COMPANIES = """
MATCH (c:Company)
WHERE ($ticker IS NULL OR c.ticker = $ticker) AND $key IS NULL
  AND coalesce(CASE WHEN $run IS NULL THEN c.first_seen >= $since ELSE c.first_run = $run END, false)
  // the very first load adds every company: that is the baseline, not a change
  AND c.first_seen > $origin
  // nor is a company discovery found in the registry (discovery.py): the graph learnt of it
  AND c.discovered_at IS NULL
  // an unlisted company (a subsidiary, a registry shareholder) is news only if it
  // came with a change, not with a source's baseline (e.g. a first read of a
  // company's list of subsidiaries)
  AND (coalesce(c.listed, true) OR EXISTS {
         MATCH ()-[r:OWNS|HOLDS_POSITION]->(c) WHERE r.opened_run = c.first_run AND NOT coalesce(r.baseline, false) }
       OR NOT EXISTS { MATCH ()-[r:OWNS|HOLDS_POSITION]->(c) WHERE r.opened_run = c.first_run })
RETURN c.ticker AS ticker, c.name AS company, c.first_seen AS date
"""


# a fact opened by a load, with the one of the same source it replaced (issue #38)
_FACTS = """
MATCH (c:Company)-[:HAS_FACT]->(f:Fact)
WHERE ($ticker IS NULL OR c.ticker = $ticker) AND $key IS NULL AND NOT coalesce(f.baseline, false)
  AND coalesce(CASE WHEN $run IS NULL THEN f.first_seen >= $since ELSE f.opened_run = $run END, false)
OPTIONAL MATCH (c)-[:HAS_FACT]->(o:Fact {name: f.name, source: f.source})
WHERE o.closed_run = f.opened_run
WITH c, f, head(collect(o.value)) AS old
RETURN c.ticker AS ticker, c.name AS company, f.name AS field, old, f.value AS new, f.source AS source,
       f.first_seen AS date
"""


def _origin(g) -> str:
    rows = g.run("MATCH (c:Company) RETURN min(c.first_seen) AS d")
    return (rows[0]["d"] if rows else None) or ""


def changes(g, *, run: str | None = None, since: str | None = None, ticker: str | None = None,
            key: str | None = None) -> list[dict]:
    """Changes recorded by one load (`run`) or on/after a date (`since`, YYYY-MM-DD).

    Records have the same shape as diff()'s, plus `detected_at` (observation
    date), `source` (short name) and, for removals, `last_seen`: the holder or
    officer left somewhere between `last_seen` and `detected_at`.
    """
    params = {"run": run, "since": since or "", "ticker": ticker.upper() if ticker else None, "key": key}
    events = g.run(_EVENTS, **params)
    added = g.run(_COMPANIES, origin=_origin(g), **params)

    out: list[dict] = []
    for c in added:
        out.append({"kind": "company_added", "ticker": c["ticker"], "company": c["company"],
                    "detected_at": c["date"], "source": ""})
    for f in g.run(_FACTS, **{k: v for k, v in params.items()}):
        out.append({"kind": "company_renamed" if f["field"] == "name" else "company_fact_changed",
                    "ticker": f["ticker"], "company": f["company"], "field": f["field"], "old": f["old"],
                    "new": f["new"], "detected_at": f["date"], "source": source_label(f["source"])})

    # (rel, ticker, key, date) -> opened / closed edges on that date
    groups: dict[tuple, dict[str, list[dict]]] = defaultdict(lambda: {"opened": [], "closed": []})
    for e in events:
        if e["opened"]:
            groups[(e["rel"], e["ticker"], e["key"], e["first_seen"])]["opened"].append(e)
        if e["closed"]:
            groups[(e["rel"], e["ticker"], e["key"], e["closed_at"])]["closed"].append(e)

    for (rel, ticker_, key_, when), g_ in groups.items():
        opened, closed = g_["opened"], g_["closed"]
        any_ = (opened or closed)[0]
        base = {"ticker": ticker_, "company": any_["company"], "detected_at": when}

        def add(kind, e, **kw):
            out.append({"kind": kind, **base, "entity": e["name"], "entity_key": key_,
                        "source": source_label(e["source"]), **kw})

        if rel == "OWNS":
            # one holder has at most one open stake per company: closed + opened = a new
            # size, if the same source says so (mse.mn dropping a holder that the registry
            # still lists is a removal and an addition)
            if opened and closed and source_label(opened[0]["source"]) == source_label(closed[0]["source"]):
                x, y = closed[0], opened[0]
                if x["percent"] is None or y["percent"] is None:
                    continue
                delta = round(y["percent"] - x["percent"], 4)
                if abs(delta) >= NOISE:
                    add("stake_changed", y, old=x["percent"], new=y["percent"], delta=delta,
                        crossed=crossed(x["percent"], y["percent"]), as_of_old=x["as_of"] or "",
                        as_of_new=y["as_of"] or "")
                continue
            for y in opened:
                add("holder_added", y, old=None, new=y["percent"], delta=y["percent"],
                    crossed=crossed(None, y["percent"]), as_of_new=y["as_of"] or "")
            for x in closed:
                add("holder_removed", x, old=x["percent"], new=None,
                    delta=-x["percent"] if x["percent"] is not None else None,
                    crossed=crossed(x["percent"], None), as_of_old=x["as_of"] or "", last_seen=x["last_seen"])
            continue

        # positions: compare seats (post), like diff()
        def seat(e):
            return e.get("post") or post_key(e["title"], e["role"])
        gone = {seat(e): e for e in closed}
        came = {seat(e): e for e in opened}
        for t in set(gone) & set(came):      # closed and reopened in the same seat
            gone.pop(t), came.pop(t)
        if len(gone) == 1 and len(came) == 1:
            (x,), (y,) = gone.values(), came.values()
            add("position_changed", y, old=x["title"], new=y["title"], role=y["role"], role_std=y.get("role_std"))
            continue
        for y in came.values():
            add("position_added", y, new=y["title"], role=y["role"], role_std=y.get("role_std"))
        for x in gone.values():
            add("position_removed", x, old=x["title"], role=x["role"], role_std=x.get("role_std"),
                last_seen=x["last_seen"])

    # stakes that kept rising over several scrapes (issue #30), dated by their latest step
    from . import trends
    for a in trends.accumulations(g, current_only=False, listed_only=False, ticker=params["ticker"], key=key,
                                  run=run, since=None if run else params["since"]):
        out.append({k: v for k, v in a.items() if k not in ("opened_run", "current", "listed")})

    # newest first; within a day by company, the company itself first, then the biggest moves
    out.sort(key=lambda c: (c["ticker"], c["kind"] != "company_added", -abs(c.get("delta") or 0)))
    out.sort(key=lambda c: c["detected_at"] or "", reverse=True)
    return out


def _under(source: str | None, prefix: str) -> bool:
    """`source` is the page `prefix` or one of its sub-pages (same rule as graph._under)."""
    source = source or ""
    return source == prefix or source.startswith(prefix.rstrip("/") + "/")


def missing_data(g, ds: Dataset) -> list[dict]:
    """`data_missing` records for one load: companies whose complete page came back
    without shareholder (or officer) rows while the graph still holds open ones."""
    out = []
    for section, rows, rel in (("shareholders", ds.shareholdings, "OWNS"),
                               ("officers", ds.positions, "HOLDS_POSITION")):
        snaps = {t.upper(): p for t, p in ds.snapshots.get("shareholdings" if rel == "OWNS" else "positions",
                                                            {}).items()}
        # only rows and edges of the company's own page count (not, e.g., a parent
        # company reporting its stake in it)
        have = {r.company_ticker.upper() for r in rows
                if r.company_ticker.upper() in snaps and _under(r.source, snaps[r.company_ticker.upper()])}
        empty = [{"t": t, "prefix": p, "under": p.rstrip("/") + "/"} for t, p in snaps.items() if t not in have]
        if not empty:
            continue
        for r in g.run(f"""
                UNWIND $scopes AS s
                MATCH (c:Company {{ticker: s.t}})<-[r:{rel} {{current: true}}]-()
                WHERE r.source = s.prefix OR r.source STARTS WITH s.under
                RETURN c.ticker AS ticker, c.name AS company, count(r) AS n""", scopes=empty):
            out.append({"kind": "data_missing", "ticker": r["ticker"], "company": r["company"],
                        "section": section, "count": r["n"]})
    # a company's list of its own stakes in other companies came back empty
    snaps = ds.snapshots.get("holdings", {})
    have = {r.holder_ticker.upper() for r in ds.shareholdings
            if r.holder_ticker and _under(r.source, snaps.get(r.holder_ticker.upper(), "\0"))}
    scopes = [{"t": t.upper(), "under": p.rstrip("/") + "/", "prefix": p}
              for t, p in snaps.items() if t.upper() not in have]
    if scopes:
        for r in g.run("""
                UNWIND $scopes AS s
                MATCH (c:Company {ticker: s.t})-[r:OWNS {current: true}]->(:Company)
                WHERE r.source = s.prefix OR r.source STARTS WITH s.under
                RETURN c.ticker AS ticker, c.name AS company, count(r) AS n""", scopes=scopes):
            out.append({"kind": "data_missing", "ticker": r["ticker"], "company": r["company"],
                        "section": "holdings", "count": r["n"]})
    return out


def run_changes(g, run: str, ds: Dataset | None = None) -> list[dict]:
    """What one load changed, plus data gaps it left alone. Empty after the first load."""
    out = changes(g, run=run)
    if ds is not None:
        when = ds.observed_at or ""
        out += [{**c, "detected_at": when, "source": ""} for c in missing_data(g, ds)]
    return out


def recent_changes(g, *, ticker: str | None = None, key: str | None = None, since: str | None = None,
                   source: str | None = None, significant_only: bool = False, limit: int = 200) -> list[dict]:
    """Changes on or after `since` (default: all), newest first."""
    out = changes(g, since=(since or "")[:10], ticker=ticker, key=key)
    if source:
        out = [c for c in out if c["source"] == source]
    if significant_only:
        out = [c for c in out if is_significant(c)]
    return out[:limit]


def ownership_timeline(g, ticker: str) -> dict:
    """Shareholder percentages of one company at each date its table changed.

    Returns {"ticker", "points": [{taken_at, as_of}], "holders": [{key, name, values}]},
    where `values[i]` is the holder's percent at `points[i]` (None = not listed) and
    `taken_at` is the first observation of that state. `as_of` is the newest
    source date among the stakes held then; a stake that never changed keeps
    only its latest `as_of`, so early points can show a later date.
    """
    ticker = ticker.upper()
    edges = g.run(
        """
        MATCH (h:Entity)-[r:OWNS]->(:Company {ticker: $t})
        WHERE r.percent IS NOT NULL          // registry rows without a % don't change the table
        RETURN h.key AS key, h.name AS name, r.percent AS percent, r.as_of AS as_of,
               r.first_seen AS start, CASE WHEN r.current THEN null ELSE coalesce(r.closed_at, r.until) END AS end
        """,
        t=ticker,
    )
    dates = sorted({e["start"] for e in edges if e["start"]} | {e["end"] for e in edges if e["end"]})
    points, columns, names = [], [], {}
    for d in dates:
        live = [e for e in edges if (e["start"] or "") <= d and (e["end"] is None or e["end"] > d)]
        col = {e["key"]: e["percent"] for e in live}
        if not col or (columns and columns[-1] == col):
            continue
        as_of = sorted({e["as_of"] for e in live if e["as_of"]})
        points.append({"taken_at": d, "as_of": as_of[-1] if as_of else ""})
        columns.append(col)
        for e in live:
            names[e["key"]] = e["name"]
    keys = {k for col in columns for k in col}
    holders = [{"key": k, "name": names[k], "values": [col.get(k) for col in columns]} for k in keys]
    holders.sort(key=lambda h: (-(h["values"][-1] or -1), -max(v or 0 for v in h["values"]), h["name"]))
    return {"ticker": ticker, "points": points, "holders": holders}
