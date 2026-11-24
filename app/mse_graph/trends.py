"""Trends over time (issue #30): officer turnover, stake-accumulation alerts, market trends.

Every edge is a period (graph.py, "History": `first_seen`, `last_seen`, `until`, `closed_at`,
`current`). The change feed reads single changes from them; this module reads the periods
as a whole, in one pass (`Periods.read`):

    turnover        per listed company: officers who arrived and left, per calendar year;
                    departures per year and the turnover rate (departures per year / average
                    number of officers), only once the company's history spans a full year;
                    average tenure of the current officers; the current CEO and how long
                    they have held the post. A rate of TURNOVER_HIGH_RATE or more, with at
                    least TURNOVER_MIN_DEPARTURES departures, is flagged `high`.
    accumulations   a holder whose stake in a company rose over ACCUMULATION_MIN_STEPS or more
                    consecutive periods (one source, ownership stakes with a percent), with
                    the thresholds it crossed on the way (ACCUMULATION_THRESHOLDS) and the
                    next one it is within ACCUMULATION_APPROACH points of. history.changes()
                    adds them to the change list as `stake_accumulation`, so they reach
                    /api/changes, /feed.xml and the alerts.
    market          a time series rebuilt from the edge periods, one point per date on which
                    the graph changed (thinned to weeks or months for long histories): the
                    state's direct stakes in listed companies, interlocks, cross-holdings
                    between listed companies and the median HHI (as quality.concentration).

Precision is the scrape date. An officer who left did so between the period's `last_seen` and
its `closed_at`, and an arrival is dated by the scrape that first saw it. History starts when
the scraping started (`history_start`), so the officers and stakes of the first load are a
baseline, not arrivals, and early tenures are lower bounds (`tenure_from: 'first_seen'`)
unless the source gave an appointment date (`since`).

The thresholds are this project's settings for alerts, not legal disclosure or takeover rules:
they haven't been checked against Mongolian law.
"""
from __future__ import annotations

import os
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from .history import NOISE, source_label


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _thresholds() -> tuple[float, ...]:
    raw = os.getenv("ACCUMULATION_THRESHOLDS", "")
    try:
        vals = tuple(sorted(float(x) for x in raw.split(",") if x.strip()))
    except ValueError:
        vals = ()
    return vals or (5.0, 25.0, 33.33, 50.0)


def settings() -> dict:
    """The configurable thresholds (environment variables), as used now."""
    return {
        "turnover_high_rate": _env_float("TURNOVER_HIGH_RATE", 0.5),
        "turnover_min_departures": int(_env_float("TURNOVER_MIN_DEPARTURES", 3)),
        "accumulation_min_steps": max(1, int(_env_float("ACCUMULATION_MIN_STEPS", 2))),
        "accumulation_approach": _env_float("ACCUMULATION_APPROACH", 2.0),
        "accumulation_thresholds": list(_thresholds()),
    }


FULL_YEAR_DAYS = 365
MAX_POINTS = 120          # market points before the series is thinned to weeks, then months
PLAUSIBLE_MAX = 100.5     # as quality.SUM_TOLERANCE: a table may add up to a little over 100


# ---- reading the periods ------------------------------------------------------------

_PERIODS = """
MATCH (x:Entity)-[r:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER]->(c:Company)
// a period the scraper saw: open, or closed by a load (a past stint copied from a source,
// already over when first seen, is history but never alive in the observed window)
WHERE (r.current OR r.closed_at IS NOT NULL)
  AND ($ticker IS NULL OR c.ticker = $ticker) AND ($key IS NULL OR x.key = $key)
RETURN type(r) AS rel, x.key AS key, x.name AS name,
       [l IN labels(x) WHERE l IN ['Company', 'Person', 'Organization']][0] AS type,
       c.key AS company, c.ticker AS ticker, c.name AS company_name, coalesce(c.listed, true) AS listed,
       r.percent AS percent, r.as_of AS as_of, coalesce(r.holding_type, 'direct') AS holding_type, r.source AS source,
       r.first_seen AS first_seen, r.last_seen AS last_seen, r.closed_at AS closed_at,
       r.current AS current, r.since AS since, r.role_std AS role_std, r.post AS post, r.title AS title,
       coalesce(r.baseline, false) AS baseline, r.opened_run AS opened_run, r.closed_run AS closed_run
"""


def _day(v) -> str:
    return str(v or "")[:10]


@dataclass
class Periods:
    """Every observed edge period, read once (`Periods.read`)."""
    edges: list[dict] = field(default_factory=list)
    as_of: str = ""          # the latest observation in the graph

    @classmethod
    def read(cls, g, ticker: str | None = None, key: str | None = None) -> Periods:
        """All periods, or those of one company (`ticker`) or one holder / officer (`key`)."""
        edges = []
        for e in g.run(_PERIODS, ticker=ticker, key=key):
            e["start"] = _day(e["first_seen"])
            e["end"] = None if e["current"] else _day(e["closed_at"]) or None
            if not e["start"] or (e["end"] is not None and e["end"] <= e["start"]):
                continue
            e["source_label"] = source_label(e["source"])
            edges.append(e)
        dates = [d for e in edges for d in (_day(e["last_seen"]), e["end"] or "") if d]
        return cls(edges, max(dates) if dates else date.today().isoformat())

    @property
    def history_start(self) -> str | None:
        return min((e["start"] for e in self.edges), default=None)

    def alive(self, d: str):
        """The edges open on date `d`."""
        return (e for e in self.edges if e["start"] <= d and (e["end"] is None or e["end"] > d))


def _date(v: str | None) -> date | None:
    """An ISO date, or a year / year-month from a source (taken as its first day)."""
    v = _day(v)
    for fmt in (v, v + "-01", v + "-01-01"):
        try:
            return date.fromisoformat(fmt)
        except ValueError:
            continue
    return None


def _years(a: str, b: str) -> float:
    da, db = _date(a), _date(b)
    return round(((db - da).days if da and db else 0) / 365.25, 2)


# ---- turnover -----------------------------------------------------------------------

def _stints(edges: list[dict]) -> list[dict]:
    """One person's position periods at one company, merged into stints: a new title in the
    same company (closed and reopened on the same scrape) is the same stint."""
    out: list[dict] = []
    for e in sorted(edges, key=lambda e: e["start"]):
        cur = out[-1] if out else None
        if cur and (cur["end"] is None or e["start"] <= cur["end"]):
            cur["edges"].append(e)
            if cur["end"] is not None and (e["end"] is None or e["end"] > cur["end"]):
                cur["end"], cur["last_seen"] = e["end"], _day(e["last_seen"])
            continue
        out.append({"start": e["start"], "end": e["end"], "last_seen": _day(e["last_seen"]),
                    "baseline": e["baseline"], "edges": [e]})
    return out


def _tenure_start(stint: dict) -> tuple[str, str]:
    """When a stint began: the source's appointment date if it is earlier, else first seen."""
    since = min((d for d in (_date(e["since"]) for e in stint["edges"]) if d), default=None)
    if since and since.isoformat() < stint["start"]:
        return since.isoformat(), "since"
    return stint["start"], "first_seen"


def _company_turnover(company: dict, edges: list[dict], as_of: str, cfg: dict) -> dict:
    by_person: dict[str, list[dict]] = defaultdict(list)
    for e in edges:
        by_person[e["key"]].append(e)
    start = min(e["start"] for e in edges)
    span = _years(start, as_of)
    full = (_date(as_of) - _date(start)).days >= FULL_YEAR_DAYS if _date(as_of) and _date(start) else False
    years: dict[str, dict[str, int]] = defaultdict(lambda: {"arrivals": 0, "departures": 0})
    arrivals, departures, current, ceo_arrivals = [], [], [], 0
    at_start = 0
    for key, es in by_person.items():
        for s in _stints(es):
            name = s["edges"][-1]["name"]
            title = s["edges"][-1]["title"]
            if s["start"] <= start:
                at_start += 1
            # the first load's officers (and a source's first report) are the baseline
            if s["start"] > start and not s["baseline"]:
                arrivals.append({"key": key, "name": name, "title": title, "date": s["start"]})
                years[s["start"][:4]]["arrivals"] += 1
                if any(e["role_std"] == "ceo" for e in s["edges"]):
                    ceo_arrivals += 1
            if s["end"] is not None:
                departures.append({"key": key, "name": name, "title": title,
                                   "between": [s["last_seen"], s["end"]], "date": s["end"]})
                years[s["end"][:4]]["departures"] += 1
            else:
                began, how = _tenure_start(s)
                ceo = [e for e in s["edges"] if e["end"] is None and e["role_std"] == "ceo"]
                cstart, chow = _tenure_start({**s, "edges": ceo, "start": min(e["start"] for e in ceo)}) \
                    if ceo else (None, None)
                current.append({"key": key, "name": name, "title": title, "since": began, "tenure_from": how,
                                "tenure_years": _years(began, as_of), "ceo": bool(ceo),
                                "ceo_since": cstart, "ceo_from": chow})
    heads = [h for h in (at_start, len(current)) if h]
    per_year = round(len(departures) / span, 2) if full and span else None
    rate = round(per_year / statistics.mean(heads), 3) if per_year is not None and heads else None
    ceos = sorted((c for c in current if c["ceo"]), key=lambda c: c["ceo_since"])
    ceo = ceos[0] if ceos else None
    arrivals.sort(key=lambda a: a["date"], reverse=True)
    departures.sort(key=lambda d: d["date"], reverse=True)
    current.sort(key=lambda c: (-c["tenure_years"], c["name"] or ""))
    tenures = [c["tenure_years"] for c in current]
    return {
        "key": company["company"], "ticker": company["ticker"], "name": company["company_name"],
        "history_start": start, "as_of": as_of, "history_years": span, "full_year": full,
        "officers": len(current), "officers_at_start": at_start,
        "arrivals": len(arrivals), "departures": len(departures),
        "departures_per_year": per_year, "turnover_rate": rate,
        "high": rate is not None and rate >= cfg["turnover_high_rate"]
                and len(departures) >= cfg["turnover_min_departures"],
        "avg_tenure_years": round(statistics.mean(tenures), 2) if tenures else None,
        # a tenure measured from first_seen is a lower bound ("at least")
        "tenure_lower_bound": any(c["tenure_from"] == "first_seen" for c in current),
        "ceo": {"key": ceo["key"], "name": ceo["name"], "title": ceo["title"], "since": ceo["ceo_since"],
                "tenure_from": ceo["ceo_from"], "tenure_years": _years(ceo["ceo_since"], as_of)} if ceo else None,
        "ceo_changes": ceo_arrivals,
        "by_year": [{"year": y, **v} for y, v in sorted(years.items())],
        "arrived": arrivals[:50], "left": departures[:50], "current": current,
    }


TURNOVER_SORTS = {
    "rate": lambda c: (c["turnover_rate"] is None, -(c["turnover_rate"] or 0), -c["departures"]),
    "departures": lambda c: (-c["departures"], -c["arrivals"]),
    "tenure": lambda c: (c["avg_tenure_years"] is None, c["avg_tenure_years"] or 0),
    "ceo_tenure": lambda c: (c["ceo"] is None, (c["ceo"] or {}).get("tenure_years") or 0),
    "ticker": lambda c: (False, c["ticker"] or ""),
}


def turnover(g=None, sort: str = "rate", periods: Periods | None = None, key: str | None = None) -> dict:
    """Officer turnover of every listed company with officers (or of one company, `key`)."""
    p = periods or Periods.read(g)
    cfg = settings()
    by_co: dict[str, list[dict]] = defaultdict(list)
    info: dict[str, dict] = {}
    for e in p.edges:
        if e["rel"] == "HOLDS_POSITION" and e["listed"] and (key is None or e["company"] == key):
            by_co[e["company"]].append(e)
            info[e["company"]] = e
    items = [_company_turnover(info[c], es, p.as_of, cfg) for c, es in by_co.items()]
    items.sort(key=lambda c: (*TURNOVER_SORTS.get(sort, TURNOVER_SORTS["rate"])(c), c["ticker"] or ""))
    rated = [c["turnover_rate"] for c in items if c["turnover_rate"] is not None]
    tenures = [c["avg_tenure_years"] for c in items if c["avg_tenure_years"] is not None]
    ceo = [c["ceo"]["tenure_years"] for c in items if c["ceo"]]
    summary = {
        "companies": len(items), "history_start": p.history_start, "as_of": p.as_of,
        "rated": len(rated), "high": sum(1 for c in items if c["high"]),
        "arrivals": sum(c["arrivals"] for c in items), "departures": sum(c["departures"] for c in items),
        "median_rate": round(statistics.median(rated), 3) if rated else None,
        "median_tenure_years": round(statistics.median(tenures), 2) if tenures else None,
        "median_ceo_tenure_years": round(statistics.median(ceo), 2) if ceo else None,
        "with_ceo": len(ceo),
        "note": "A rate needs a full year of history; departures are dated between last_seen and the "
                "scrape that missed them; tenures from first_seen are lower bounds.",
    }
    return {"items": items, "summary": summary, "settings": cfg}


def company_turnover(g, key: str) -> dict | None:
    # every edge period of the market, shared by the clicks in the details panel (memo.py)
    from .memo import cached
    rows = turnover(g, key=g.resolve_key(key), periods=cached(g, "periods", Periods.read))["items"]
    return rows[0] if rows else None


# ---- stake accumulation -------------------------------------------------------------

def _accumulation_events(edges: list[dict], cfg: dict) -> list[dict]:
    """Every point at which one holder's stake in one company (one source) had risen over
    `accumulation_min_steps` or more consecutive periods; the last one per pair is `current`
    when its period is still open."""
    th = cfg["accumulation_thresholds"]
    chains: dict[tuple, list[dict]] = defaultdict(list)
    for e in edges:
        if (e["rel"] == "OWNS" and e["percent"] is not None and e["holding_type"] in ("direct", "unknown")
                and e["key"] != e["company"]):
            chains[(e["key"], e["company"], e["source_label"])].append(e)
    out = []
    for chain in chains.values():
        chain.sort(key=lambda e: e["start"])
        run_start = 0
        for i in range(1, len(chain)):
            if chain[i]["percent"] <= chain[i - 1]["percent"] + NOISE:
                run_start = i
                continue
            steps = i - run_start
            if steps < cfg["accumulation_min_steps"]:
                continue
            first, last = chain[run_start], chain[i]
            lo, hi = first["percent"], last["percent"]
            above = [t for t in th if t > hi]
            nxt = above[0] if above and above[0] - hi <= cfg["accumulation_approach"] else None
            crossed = [t for t in th if lo < t <= hi]
            out.append({
                "kind": "stake_accumulation", "ticker": last["ticker"], "company": last["company_name"],
                "company_key": last["company"], "entity": last["name"], "entity_key": last["key"],
                "entity_type": last["type"], "listed": last["listed"],
                "old": lo, "new": hi, "delta": round(hi - lo, 4), "steps": steps,
                "crossed": crossed, "approaching": nxt,
                "level": "crossed" if crossed else "approaching" if nxt is not None else "rising",
                "path": [{"date": e["start"], "percent": e["percent"], "as_of": e.get("as_of")}
                         for e in chain[run_start:i + 1]],
                "since": first["start"], "detected_at": last["start"], "opened_run": last["opened_run"],
                "current": last["end"] is None and i == len(chain) - 1,
                "source": last["source_label"],
            })
    return out


_LEVEL_ORDER = {"crossed": 0, "approaching": 1, "rising": 2}


def accumulations(g=None, *, periods: Periods | None = None, current_only: bool = True,
                  listed_only: bool = True, ticker: str | None = None, key: str | None = None,
                  run: str | None = None, since: str | None = None) -> list[dict]:
    """Stake-accumulation alerts: by default the holders still accumulating now, in listed
    companies, crossed thresholds first, then those approaching one, then by the size of
    the rise. `run` / `since` give the events one load (or the loads since a date) recorded."""
    p = periods or Periods.read(g, ticker=ticker, key=key)
    out = _accumulation_events(p.edges, settings())
    out = [a for a in out
           if (not current_only or a["current"]) and (not listed_only or a["listed"])
           and (ticker is None or a["ticker"] == ticker) and (key is None or a["entity_key"] == key)
           and (run is None or a["opened_run"] == run) and (since is None or a["detected_at"] >= since)]
    out.sort(key=lambda a: (_LEVEL_ORDER[a["level"]], -a["delta"], a["ticker"] or "", a["entity"] or ""))
    return out


def accumulation_report(g) -> dict:
    p = Periods.read(g)
    items = accumulations(periods=p)
    return {"items": items, "history_start": p.history_start, "as_of": p.as_of, "settings": settings(),
            "note": "Thresholds are this project's alert settings, not legal rules."}


# ---- market trends --------------------------------------------------------------------

METRICS = {
    "companies": "listed companies in the graph",
    "with_holdings": "listed companies with disclosed holdings (a percent)",
    "with_officers": "listed companies with officers",
    "state_companies": "listed companies with a direct state stake",
    "state_mean_percent": "mean direct state stake over the companies with holdings (%)",
    "interlocks": "people tied to two or more companies (posts, stakes, beneficial ownership)",
    "cross_holdings": "stakes of one listed company in another",
    "median_hhi": "median HHI of the companies whose holdings add up to at most 100% (as #7)",
    "mean_hhi": "mean HHI of the same companies",
    "officers": "positions at listed companies",
}


def _thin(dates: list[str], every: str) -> list[str]:
    """The last date of each week ('week') or month ('month'); all for 'day'."""
    if every == "day":
        return dates
    out: dict[str, str] = {}
    for d in dates:
        dd = _date(d)
        if not dd:
            continue
        b = d[:7] if every == "month" else "%d-W%02d" % dd.isocalendar()[:2]
        out[b] = max(out.get(b, d), d)
    return sorted(out.values())


def _point(p: Periods, d: str, state: set[str], first_seen: dict[str, str]) -> dict:
    stakes: dict[str, list[dict]] = defaultdict(list)
    officers: dict[str, int] = defaultdict(int)
    ties: dict[str, set[str]] = defaultdict(set)
    cross = 0
    for e in p.alive(d):
        real = e["rel"] != "OWNS" or e["holding_type"] in ("direct", "unknown")
        if e["type"] == "Person" and real:
            ties[e["key"]].add(e["company"])
        if not e["listed"]:
            continue
        if e["rel"] == "HOLDS_POSITION":
            officers[e["company"]] += 1
        elif e["rel"] == "OWNS" and e["percent"] is not None:
            stakes[e["company"]].append(e)
            if real and e["type"] == "Company" and e["key"] != e["company"] and e["key"] in first_seen:
                cross += 1
    hhis, state_pcts, state_cos = [], [], 0
    for c, ss in stakes.items():
        own = [s for s in ss if s["holding_type"] in ("direct", "unknown") and s["key"] != c]
        st = sum(s["percent"] for s in own if s["key"] in state)
        state_pcts.append(st)
        state_cos += st > 0
        total = sum(s["percent"] for s in ss)
        if 0 < total <= PLAUSIBLE_MAX:
            hhis.append(sum(s["percent"] ** 2 for s in own))
    return {
        "date": d,
        "companies": sum(1 for f in first_seen.values() if f and f <= d),
        "with_holdings": len(stakes), "with_officers": len(officers),
        "state_companies": state_cos,
        "state_mean_percent": round(statistics.mean(state_pcts), 3) if state_pcts else None,
        "interlocks": sum(1 for cos in ties.values() if len(cos) >= 2),
        "cross_holdings": cross,
        "median_hhi": round(statistics.median(hhis), 1) if hhis else None,
        "mean_hhi": round(statistics.mean(hhis), 1) if hhis else None,
        "officers": sum(officers.values()),
    }


def market(g, every: str = "auto", periods: Periods | None = None) -> dict:
    """The market-wide time series: one point per date the graph changed (`every`: day,
    week, month, or auto: days up to MAX_POINTS points, then weeks, then months), each
    computed from the edges open on that date. The last point is the graph now."""
    from .analysis import state_entities

    p = periods or Periods.read(g)
    state = {s["key"] for s in state_entities(g)}
    listed = g.run("MATCH (c:Company) WHERE coalesce(c.listed, true) RETURN c.key AS key, c.first_seen AS f")
    first_seen = {r["key"]: _day(r["f"]) for r in listed}
    start = p.history_start
    dates = sorted({d for e in p.edges for d in (e["start"], e["end"]) if d} | ({p.as_of} if start else set()))
    dates = [d for d in dates if d <= p.as_of]
    if every == "auto":
        every = "day"
        for step in ("week", "month"):
            if len(_thin(dates, every)) <= MAX_POINTS:
                break
            every = step
    pts = [_point(p, d, state, first_seen) for d in _thin(dates, every)]
    if pts and pts[-1]["date"] != p.as_of and start:
        pts.append(_point(p, p.as_of, state, first_seen))
    return {"points": pts, "every": every, "history_start": start, "as_of": p.as_of,
            "metrics": METRICS,
            "note": "Rebuilt from the edge periods: each point is the graph as the scrapes saw it on "
                    "that date. History starts when the scraping started; companies that joined "
                    "later raise the counts without a change in the market."}


def summary_line(g) -> dict:
    """For the pipeline log and the daily job: the latest market point, the number of
    accumulation alerts and high-turnover companies."""
    p = Periods.read(g)
    m = market(g, every="month", periods=p)
    last = m["points"][-1] if m["points"] else {}
    t = turnover(periods=p)["summary"]
    acc = accumulations(periods=p)
    return {"history_start": p.history_start, "as_of": p.as_of, "points": len(m["points"]),
            "now": {k: last.get(k) for k in ("state_companies", "interlocks", "cross_holdings", "median_hhi")},
            "accumulating": len(acc), "accumulating_crossed": sum(1 for a in acc if a["level"] == "crossed"),
            "turnover_rated": t["rated"], "turnover_high": t["high"]}

