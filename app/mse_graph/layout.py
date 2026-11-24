"""Real open.mse.mn pages as regression fixtures, and a live layout check (issue #10).

open.mse.mn's layout can change without notice. Before this module, the parsers were tested
only on synthetic pages, so a change showed up, if at all, as tables that quietly came back
empty. Now:

* ``tests/fixtures/live/`` holds real pages of a few listed companies, saved by
  ``cli layout-fixtures --refresh``: the company list (``companies.json``) and, per ticker,
  the profile page and its four tabs (``<TICKER>_<page>.html``). Scripts, styles and images
  are dropped and long price-history tables cut to their newest rows (no parser reads the
  rest). ``snapshot.json`` is what the parsers make of them; ``tests/test_live_fixtures.py``
  fails when a parser change alters it. After a deliberate change, ``cli layout-fixtures
  --rebuild`` rewrites it, and the diff shows what changed.
* ``cli layout-check`` (the 04:12 daily job) fetches the same pages live, parses them and
  compares them with the fixtures: a table header that is gone, or a tab that parses to
  nothing where the fixture had rows, is a problem (exit code 1). That is the early
  warning for a layout change, a day after it happens instead of whenever someone notices.

The fixtures were how the nested-table bug of 2026-11-24 was found: open.mse.mn puts each
shareholder's related parties ("Түүнтэй нэгдмэл сонирхолтой этгээд": wife, parent company)
and its other holdings in tables inside the row's cells, and the parser read those rows as
shareholders of the company. ``repair_nested`` corrects what that put in the graph.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from .models import Company

log = logging.getLogger(__name__)

LIVE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "live"
# APU: large, full board; KHAN and TDB: related parties and other holdings nested in cells;
# HRM: a related party (spouse) who is also a holder, spelt in another case; AAR: small, no board.
DEFAULT_TICKERS = ("APU", "KHAN", "TDB", "HRM", "AAR")
PAGES = ("info", "shareholders", "boardmembers", "officials", "tradeinfo")
TAB_KIND = {"shareholders": "shareholders", "boardmembers": "board", "officials": "executive"}
MAX_ROWS = 30            # rows kept of a long table (price history)
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def page_url(company: Company, page: str) -> str:
    return company.url if page == "info" else f"{company.url}/tab/{page}"


# ---- saving pages --------------------------------------------------------------------------

def slim(html: str, max_rows: int = MAX_ROWS) -> str:
    """The page without what no parser reads: scripts, styles, images, forms' hidden inputs,
    and the rows of a long table after its header and first `max_rows` rows."""
    soup = BeautifulSoup(html, "lxml")
    for el in soup.find_all(["script", "style", "noscript", "svg", "img", "link", "meta", "iframe"]):
        el.decompose()
    for table in soup.find_all("table"):
        own = [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]
        for tr in own[max_rows + 1:]:
            tr.decompose()
    # the tradeinfo tab's chart data: the whole price history as JSON in an attribute
    # (data-trading-histories, megabytes); keep its newest entries
    for el in soup.find_all(True):
        for k, v in list(el.attrs.items()):
            if isinstance(v, str) and len(v) > 2000:
                try:
                    items = json.loads(v)
                except ValueError:
                    el[k] = v[:2000]
                    continue
                if isinstance(items, list):
                    el[k] = json.dumps(items[-max_rows:], ensure_ascii=False, separators=(",", ":"))
    text = str(soup)
    return re.sub(r"\n\s*\n+", "\n", text)


def fetch_pages(fetcher, company: Company, use_cache: bool = False) -> dict[str, str | None]:
    return {p: fetcher.get(page_url(company, p), use_cache=use_cache) for p in PAGES}


def refresh(fetcher, tickers=DEFAULT_TICKERS, out: Path | str = LIVE_DIR) -> dict:
    """Download the company list and each ticker's pages into `out`, then write the snapshot."""
    from . import scraper

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    raw = fetcher.get(scraper.COMPANY_LIST_API, use_cache=False)
    if not raw:
        raise RuntimeError(f"no company list from {scraper.COMPANY_LIST_API}")
    listing = json.loads(raw)
    (out / "companies.json").write_text(json.dumps(listing, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    companies = {c.ticker: c for c in scraper.list_companies_api(_Static(raw))}
    manifest = {"fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
                "company_list": scraper.COMPANY_LIST_API, "companies": {}}
    for t in tickers:
        c = companies.get(t)
        if c is None:
            raise RuntimeError(f"{t} is not in the company list")
        pages = fetch_pages(fetcher, c)
        missing = [p for p, h in pages.items() if not h]
        if missing:
            raise RuntimeError(f"{t}: could not fetch {', '.join(missing)}")
        for p, h in pages.items():
            (out / f"{t}_{p}.html").write_text(slim(h), encoding="utf-8")
        manifest["companies"][t] = {"name": c.name, "mse_id": c.mse_id, "url": c.url}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    snap = rebuild(out)
    return {"dir": str(out), "tickers": list(snap["companies"]), "fetched_at": manifest["fetched_at"]}


class _Static:
    """A fetcher that answers every URL with the same text (to reuse list_companies_api)."""
    def __init__(self, text: str):
        self.text = text

    def get(self, url, use_cache=True):
        return self.text


# ---- parsing a company's pages ------------------------------------------------------------

def parse_pages(pages: dict[str, str | None], company: Company) -> dict:
    """What the parsers make of one company's pages, as plain data (the snapshot's shape)."""
    from . import market, scraper

    c = Company(ticker=company.ticker, name=company.name, mse_id=company.mse_id, url=company.url)
    out: dict = {}
    if pages.get("info"):
        ceo = scraper.parse_open_info(pages["info"], c)
        out["info"] = {"reg_no": c.reg_no, "sector": c.sector, "website": c.website, "address": c.address,
                       "ceo": ceo[0].person_name if ceo else ""}
    for page, kind in TAB_KIND.items():
        if pages.get(page) is None:
            continue
        ds = scraper.parse_open_tab(pages[page], c.ticker, kind, page_url(c, page))
        if kind == "shareholders":
            out[page] = [{"name": s.holder_name, "percent": s.percent, "shares": s.shares, "as_of": s.as_of}
                         for s in ds.shareholdings]
            out["affiliations"] = [{"holder": a.holder_name, "party": a.party_name, "relation": a.relation,
                                    "party_shares": a.party_shares}
                                   for a in scraper.parse_affiliations(pages[page], c.ticker, page_url(c, page))]
        else:
            out[page] = [{"name": p.person_name, "title": p.title, "role": p.role, "since": p.since}
                         for p in ds.positions]
    if pages.get("tradeinfo"):
        q = market.parse_html(pages["tradeinfo"], c.ticker, page_url(c, "tradeinfo"))
        out["tradeinfo"] = {"close": q.close, "trade_date": q.trade_date, "open": q.open,
                            "shares_outstanding": q.shares_outstanding, "market_cap": q.market_cap,
                            "volume": q.volume, "turnover": q.turnover,
                            "series_points": len(q.series), "series_last": list(q.series[-1]) if q.series else None}
    out["headers"] = {p: headers(h) for p, h in pages.items() if h}
    return out


def headers(html: str) -> list[list[str]]:
    """The header row of each of the page's own tables (not tables nested in cells)."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for table in soup.find_all("table"):
        if table.find_parent("table") is not None:
            continue
        tr = next((tr for tr in table.find_all("tr") if tr.find_parent("table") is table), None)
        if tr is not None and tr.find("th") and not tr.find("td", recursive=False):
            out.append([re.sub(r"\s+", " ", th.get_text(" ")).strip().lower()
                        for th in tr.find_all("th", recursive=False)])
    return out


def load_saved(d: Path | str = LIVE_DIR) -> tuple[list[Company], dict[str, dict[str, str]]]:
    """The saved company list and pages: ([Company], {ticker: {page: html}})."""
    from . import scraper

    d = Path(d)
    raw = (d / "companies.json").read_text(encoding="utf-8")
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    companies = scraper.list_companies_api(_Static(raw))
    pages = {t: {p: (d / f"{t}_{p}.html").read_text(encoding="utf-8") for p in PAGES}
             for t in manifest["companies"]}
    return companies, pages


def snapshot(d: Path | str = LIVE_DIR) -> dict:
    companies, pages = load_saved(d)
    by_ticker = {c.ticker: c for c in companies}
    return {"companies_listed": len(companies),
            "companies": {t: parse_pages(p, by_ticker[t]) for t, p in pages.items()}}


def rebuild(d: Path | str = LIVE_DIR) -> dict:
    """Re-parse the saved pages and rewrite snapshot.json (after a deliberate parser change)."""
    snap = snapshot(d)
    (Path(d) / "snapshot.json").write_text(json.dumps(snap, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                           encoding="utf-8")
    return snap


# ---- checks -----------------------------------------------------------------------------

def problems(parsed: dict, expected: dict | None = None) -> list[str]:
    """What looks wrong in one company's parse. With `expected` (the fixture's parse of the
    same company), a table header the fixture had and the live page lacks, or a tab that
    parses to nothing where the fixture had rows, is a problem too."""
    out: list[str] = []
    info = parsed.get("info")
    if info is None:
        out.append("info: page missing")
    elif not (info["sector"] or info["reg_no"]):
        out.append("info: neither a sector nor a register number read")
    holders = parsed.get("shareholders")
    if holders is not None:
        for h in holders:
            if h["percent"] is None:
                out.append(f"shareholders: no percent for {h['name']!r}")
            elif not 0 <= h["percent"] <= 100:
                out.append(f"shareholders: {h['percent']}% for {h['name']!r}")
            if h["as_of"] and not ISO.match(h["as_of"]):
                out.append(f"shareholders: as_of {h['as_of']!r} for {h['name']!r}")
        total = sum(h["percent"] or 0 for h in holders)
        if total > 100.5:
            out.append(f"shareholders: percentages add up to {total:.2f}")
        names = [h["name"].casefold() for h in holders]
        if len(set(names)) < len(names):
            out.append("shareholders: a holder listed twice")
    for page in ("shareholders", "boardmembers", "officials"):
        for row in parsed.get(page) or []:
            if re.search(r"\d", row["name"]) or len(row["name"]) < 3:
                out.append(f"{page}: odd name {row['name']!r}")
    q = parsed.get("tradeinfo")
    if q is not None and q["close"] is None:
        out.append("tradeinfo: no closing price")
    if expected:
        for page in ("shareholders", "boardmembers", "officials"):
            if expected.get(page) and parsed.get(page) is not None and not parsed[page]:
                out.append(f"{page}: no rows parsed (the fixture has {len(expected[page])})")
        if expected.get("info", {}).get("ceo") and info is not None and not info["ceo"]:
            out.append("info: no CEO read (the fixture has one)")
        exp_q = expected.get("tradeinfo") or {}
        if q is not None:
            for k in ("shares_outstanding", "market_cap"):
                if exp_q.get(k) is not None and q.get(k) is None:
                    out.append(f"tradeinfo: no {k} (the fixture has one)")
            if exp_q.get("series_points") and not q.get("series_points"):
                out.append("tradeinfo: no price history (the fixture has one)")
        for page, hdrs in (expected.get("headers") or {}).items():
            live = parsed.get("headers", {}).get(page)
            if live is None:
                continue
            for h in hdrs:
                if h not in live:
                    out.append(f"{page}: table header changed or gone: {' | '.join(h)}")
    return out


def check_saved(d: Path | str = LIVE_DIR) -> dict[str, list[str]]:
    return {t: problems(p) for t, p in snapshot(d)["companies"].items()}


def live_check(fetcher, d: Path | str = LIVE_DIR, tickers=None) -> dict:
    """Fetch the fixtures' pages live and compare (see the module doc). Returns a report with
    `problems` {ticker: [..]} (empty lists dropped) and `ok`."""
    from . import scraper

    d = Path(d)
    expected = json.loads((d / "snapshot.json").read_text(encoding="utf-8"))
    tickers = list(tickers or expected["companies"])
    report: dict = {"checked": [], "problems": {}, "requests": 0}
    companies = {c.ticker: c for c in scraper.list_companies_api(fetcher)}
    report["requests"] += 1
    report["companies_listed"] = len(companies)
    if len(companies) < 0.8 * expected["companies_listed"]:
        report["problems"]["list"] = [f"the company list has {len(companies)} companies "
                                      f"(the fixture {expected['companies_listed']})"]
    for t in tickers:
        c = companies.get(t)
        if c is None:
            report["problems"][t] = ["not in the company list (delisted? then refresh the fixtures)"]
            continue
        pages = fetch_pages(fetcher, c)
        report["requests"] += len(pages)
        failed = [p for p, h in pages.items() if h is None]
        parsed = parse_pages(pages, c)
        found = problems(parsed, expected["companies"].get(t))
        found += [f"{p}: fetch failed" for p in failed]
        report["checked"].append(t)
        if found:
            report["problems"][t] = found
    report["ok"] = not report["problems"]
    return report


# ---- repairing what the nested-table bug loaded (2026-11-24) ------------------------------

REPAIR_KEY = "mse_nested_repair"
REPAIR_VERSION = 1


def _row_key(name: str) -> str:
    from .records import name_key
    return name_key(name)


def nested_findings(html: str, ticker: str, source: str) -> dict:
    """Compare the parse of a shareholders tab with the pre-2026-11-24 parse of it.
    Returns {"rows": {key: row}, "bad": [(name, percent, shares, as_of)], "phantoms": {keys}}:
    the right rows, the rows only the old parse made, and the names those rows gave that are
    no shareholder of the company at all (related parties, other holdings' owners)."""
    from . import scraper

    new = scraper.parse_open_tab(html, ticker, "shareholders", source).shareholdings
    old = scraper.parse_open_tab(html, ticker, "shareholders", source, nested=True).shareholdings
    tup = lambda s: (s.holder_name, s.percent, s.shares, s.as_of or None)   # noqa: E731
    good = {tup(s) for s in new}
    rows: dict[str, dict] = {}
    for s in new:
        k = _row_key(s.holder_name)
        if k not in rows or (rows[k]["percent"] is None and s.percent is not None):
            rows[k] = {"name": s.holder_name, "percent": s.percent, "shares": s.shares, "as_of": s.as_of or None}
    bad = [tup(s) for s in old if tup(s) not in good]
    phantoms = {_row_key(b[0]) for b in bad} - set(rows)
    return {"rows": rows, "bad": bad, "phantoms": phantoms}


def repair_candidates(g) -> list[dict]:
    """Companies whose shareholders-tab stakes or records look like the bug's: no percent, or
    an `as_of` that is a header word ("№", "Компанийн нэр")."""
    return g.run(
        """
        MATCH (c:Company) WHERE c.url STARTS WITH 'https://open.mse.mn/securities/'
        WITH c, c.url + '/tab/shareholders' AS u
        WHERE EXISTS { MATCH ()-[o:OWNS]->(c)
                       WHERE u IN coalesce(o.sources, [o.source])
                         AND (o.percent IS NULL OR (o.as_of IS NOT NULL AND NOT o.as_of =~ $iso AND o.as_of <> '')) }
           OR EXISTS { MATCH (r:Record {kind: 'shareholder', source_url: u})
                       WHERE r.raw_percent IS NULL OR (r.raw_as_of IS NOT NULL AND NOT r.raw_as_of =~ $iso) }
        RETURN c.ticker AS ticker, c.url AS url, u AS tab ORDER BY ticker
        """, iso=ISO.pattern)


def repair_company(g, ticker: str, tab: str, html: str, when: str, dry_run: bool = False) -> dict:
    """Correct one company's graph from its shareholders tab (see repair_nested)."""
    f = nested_findings(html, ticker, tab)
    rows, phantoms = f["rows"], f["phantoms"]
    bad = [{"name": b[0], "percent": b[1], "shares": b[2], "as_of": b[3]} for b in f["bad"]]
    # every holder with an OWNS edge from this tab, and the name keys it is known by
    edges = g.run(
        """
        MATCH (e:Entity)-[o:OWNS]->(c:Company {ticker: $t})
        WHERE $u IN coalesce(o.sources, [o.source])
        OPTIONAL MATCH (r:Record {kind: 'shareholder', source_url: $u})-[:REFERS_TO]->(e)
        RETURN elementId(o) AS id, e.key AS key, e.name AS name, o.current AS current, o.percent AS percent,
               o.shares AS shares, o.as_of AS as_of, coalesce(o.sources, [o.source]) AS sources,
               collect(DISTINCT r.name_key) AS rkeys
        """, t=ticker, u=tab)
    fix, drop, unsource = [], [], []
    for e in edges:
        keys = {_row_key(e["name"] or ""), *[k for k in e["rkeys"] if k]}
        hit = next((rows[k] for k in keys if k in rows), None)
        polluted = e["percent"] is None or bool(e["as_of"] and not ISO.match(e["as_of"]))
        if hit is not None:
            if e["current"] and polluted and (e["percent"], e["shares"], e["as_of"]) != (
                    hit["percent"], hit["shares"], hit["as_of"]):
                fix.append({"id": e["id"], "name": e["name"], "percent": hit["percent"], "shares": hit["shares"],
                            "as_of": hit["as_of"], "was": [e["percent"], e["shares"], e["as_of"]]})
        elif keys & phantoms and e["percent"] is None:
            (drop if list(e["sources"]) == [tab] else unsource).append({"id": e["id"], "name": e["name"],
                                                                        "key": e["key"]})
    report = {"ticker": ticker, "fixed": [f"{x['name']}: {x['was']} -> {x['percent']}% {x['as_of'] or ''}".strip()
                                          for x in fix],
              "removed": [x["name"] for x in drop], "unsourced": [x["name"] for x in unsource]}
    if dry_run:
        report["bad_records"] = len(bad)
        return report
    g.run("""UNWIND $rows AS x MATCH ()-[o:OWNS]->() WHERE elementId(o) = x.id
             SET o.percent = x.percent, o.shares = x.shares, o.as_of = x.as_of, o.repaired = $when""",
          rows=fix, when=when)
    g.run("UNWIND $ids AS id MATCH ()-[o:OWNS]->() WHERE elementId(o) = id DELETE o",
          ids=[x["id"] for x in drop])
    g.run("""UNWIND $ids AS id MATCH ()-[o:OWNS]->() WHERE elementId(o) = id
             WITH o, [s IN coalesce(o.sources, [o.source]) WHERE s <> $u] AS keep
             SET o.sources = keep, o.source = CASE WHEN o.source = $u THEN keep[0] ELSE o.source END""",
          ids=[x["id"] for x in unsource], u=tab)
    # the records the old parse made: gone, and out of every edge's list
    ids = [r["id"] for r in g.run(
        """
        UNWIND $bad AS b
        MATCH (r:Record {kind: 'shareholder', source_url: $u})
        WHERE r.raw_name = b.name AND coalesce(r.raw_percent, -1.0) = coalesce(b.percent, -1.0)
          AND coalesce(r.raw_shares, -1) = coalesce(b.shares, -1) AND coalesce(r.raw_as_of, '') = coalesce(b.as_of, '')
        RETURN DISTINCT r.record_id AS id
        """, bad=bad, u=tab)]
    if ids:
        g.run("""MATCH (:Entity)-[o:OWNS]->(:Company {ticker: $t}) WHERE any(x IN coalesce(o.records, []) WHERE x IN $ids)
                 SET o.records = [x IN o.records WHERE NOT x IN $ids]""", t=ticker, ids=ids)
        g.run("MATCH (r:Record) WHERE r.record_id IN $ids DETACH DELETE r", ids=ids)
    gone = [x["key"] for x in drop]
    removed_entities = g.run("MATCH (e:Entity) WHERE e.key IN $ks AND NOT e:Company AND NOT (e)--() "
                             "DELETE e RETURN count(e) AS n", ks=gone)[0]["n"] if gone else 0
    report.update(records_deleted=len(ids), entities_deleted=removed_entities)
    return report


def repair_nested(g, fetcher=None, dry_run: bool = False, force: bool = False, when: str | None = None) -> dict:
    """Undo what the nested-table bug loaded, once (`(:Meta {key: 'mse_nested_repair'})`).

    For each company with a suspicious shareholders-tab stake (repair_candidates), its tab is
    read again (one request) and compared with what the old parser made of it:
    * a real holder whose stake lost its percent or got a header word as `as_of` (the related
      party's row overwrote it) gets the right values in place: a correction, not a change,
      so no "stake changed" event;
    * a stake of someone the tab names only as a related party is deleted (or loses the tab
      from its sources, when another source also gives it), and so is that holder when it
      has no other link: they never owned the shares, so it is not recorded as a sale;
    * the source records of the old parse's rows are deleted.
    """
    from . import scraper

    when = when or date.today().isoformat()
    if not force and not dry_run:
        done = g.run("MATCH (m:Meta {key: $k}) RETURN m.version AS v", k=REPAIR_KEY)
        if done and (done[0]["v"] or 0) >= REPAIR_VERSION:
            return {"skipped": "done before"}
    fetcher = fetcher or scraper.Fetcher()
    report: dict = {"companies": [], "failed": []}
    for c in repair_candidates(g):
        html = fetcher.get(c["tab"], use_cache=False)
        if html is None:
            report["failed"].append(c["ticker"])
            continue
        r = repair_company(g, c["ticker"], c["tab"], html, when, dry_run=dry_run)
        if any(r.get(k) for k in ("fixed", "removed", "unsourced", "records_deleted", "bad_records")):
            report["companies"].append(r)
    report["fixed"] = sum(len(r["fixed"]) for r in report["companies"])
    report["removed"] = sum(len(r["removed"]) for r in report["companies"])
    if not dry_run and not report["failed"]:
        g.run("MERGE (m:Meta {key: $k}) SET m.version = $v, m.at = $when", k=REPAIR_KEY, v=REPAIR_VERSION,
              when=when)
    return report
