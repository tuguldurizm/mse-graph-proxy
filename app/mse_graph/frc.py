"""FRC (Financial Regulatory Commission, frc.mn): a first look at its data (issue #10, "More sources").

frc.mn is an Angular site. Its pages load everything from a JSON API on a separate port,
`https://www.frc.mn:5001/api/` (the site's `environment.apiUrl`), with the calls

    menu?site=main&lang=mn&status=1         the site's menu (every section)
    document?menuid=<id>&site=main&lang=mn  the documents filed under one menu (files)
    news?menuid=<id>&site=main&lang=mn&page=<n>   the posts under one menu
    securitie/custodian, securitie/secbroker, frc/nbfi, insurance/insurance, ...   licensed entities
    frc/orgdetails?id=<id>                  one licensed entity
    tabinfo/stakeholder?register=<reg no>   its stakeholders (POST, empty body), tabinfo/branch too
    frc/decision?sctId=<sector>&decId=<menu id>   the commission's decisions

Claude Code cloud sessions can't reach that port (their proxy only carries HTTPS on 443), so this
module runs where the data is reachable: the hosted pipeline on Render (`frc_step` in
pipeline.py), or `python -m mse_graph.cli frc-probe` on a machine with open egress. For now it
only *describes* what the API returns, as `FRC layout` log lines, so the parsers can be written
against real data. What the probes found (2026-11-23): the menu has 569 entries and no
large-holding or insider section; the licensed entities are typed lists (`LISTS`: custodians,
brokers, nominee accounts, NBFIs, insurers, ...), each entity has details, stakeholders and
branches by register number, and the commission's decisions are per sector and year. Nothing is
stored except the time and version of the last probe (`(:Meta {key: 'frc'})`).

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

# bump to probe again at the next start, whatever FRC_MIN_INTERVAL_HOURS says
PROBE_VERSION = 5      # 4: the import (step 2); 5: no FRC shareholders for listed companies, BOM fix

# The typed lists behind the "org" / "sct" / "decision" pages (the site's lazy chunk 454, read
# 2026-11-23): GET, no parameters. Custodians and brokers are the nominee list's sources (#39).
LISTS = ("securitie/custodian", "securitie/secbroker", "securitie/nominal", "securitie/underwriter",
         "securitie/dealer", "frc/nbfi", "insurance/insurance")
# per licensed entity: details (GET frc/orgdetails?id=), stakeholders and branches (POST
# tabinfo/stakeholder?register=, tabinfo/branch?register=); decisions: GET frc/decision?sctId=&decId=
_REG = re.compile(r"regist|regno|reg_no|rd$|^reg", re.I)



def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _client_get(timeout: float = 60.0) -> Callable[..., str]:
    """A plain GET (direct, not through MSE_PROXY_URL: the fetch proxy only allows mse.mn and
    opendatalab.mn); `post=True` sends the empty POST the site uses for its read-only `tabinfo`
    calls. Returns the body; raises on an HTTP error."""
    import httpx

    client = httpx.Client(timeout=httpx.Timeout(timeout, connect=20), follow_redirects=True,
                          headers={"User-Agent": "mse-graph/0.1 (research crawler)",
                                   "Content-Type": "application/json"})

    def get(url: str, post: bool = False) -> str:
        r = client.post(url) if post else client.get(url)
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

    def fetch(path: str, post: bool = False, **params):
        if report["requests"] >= max_requests:
            return None
        if report["requests"]:
            sleep(delay)
        report["requests"] += 1
        u = url(path, **params)
        try:
            return json.loads((get(u, post=True) if post else get(u)).lstrip("\ufeff"))
        except Exception as exc:          # an HTTP error, a timeout, not JSON: note it and go on
            report["errors"].append(f"{path}: {type(exc).__name__}: {str(exc)[:200]}")
            log.warning("FRC layout %s failed: %s", u, exc)
            return None

    menu = fetch("menu", **SITE, status=1)
    if menu is None:
        return report
    items = menu_items(menu)
    report["menus"] = len(items)
    # the typed lists of licensed entities
    first: dict | None = None
    for path in LISTS:
        ans = fetch(path)
        if ans is None:
            continue
        recs = records(ans)
        report["listings"][path] = len(recs)
        describe(ans, path)
        if recs and first is None and path in ("securitie/custodian", "securitie/secbroker"):
            first = recs[0]
    # one entity's details, stakeholders and branches, from the first custodian or broker
    if first:
        eid = _first(first, _ID)
        reg = next((v for k, v in first.items() if _REG.search(k) and v), None)
        report["sample"] = {"id": eid, "register": reg}
        if eid is not None:
            ans = fetch("frc/orgdetails", id=eid)
            if ans is not None:
                describe(ans, f"frc/orgdetails id={eid}")
        if reg:
            for path in ("tabinfo/stakeholder", "tabinfo/branch"):
                ans = fetch(path, post=True, register=reg)
                if ans is not None:
                    report["listings"][path] = len(records(ans))
                    describe(ans, f"{path} register={reg}")
    # one decisions page: the securities sector (11) and its newest year's menu
    dec = sorted((m for m in items if m["kind"] == "decision" and m["parent"] == 181), key=lambda m: -(m["id"] or 0))
    if dec:
        ans = fetch("frc/decision", sctId=11, decId=dec[0]["id"])
        if ans is not None:
            report["listings"][f"frc/decision:{dec[0]['id']}"] = len(records(ans))
            describe(ans, f"frc/decision sctId=11 decId={dec[0]['id']}")
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


# ---- the import (FRC step 2) -------------------------------------------------------------------
# Every licensed entity FRC lists becomes (or enriches) a company: listed companies are matched by
# their register number or mse.mn's 10-digit state number, which also fills the register number of
# the listed ones FRC licenses (banks, insurers, brokers, NBFIs); the others come in as unlisted
# `~<register no>` companies. Each keeps its licences (`frc_licenses`, `frc_kinds`). Their CEO comes
# with the list; their shareholders (`tabinfo/stakeholder`) and board (license.frc.mn `token/tuz`)
# are read for FRC_DETAIL_BUDGET entities a run, listed companies first, then those never read,
# then the oldest (every FRC_DETAIL_DAYS). FRC is a supplementary source like the registry: its
# rows add to edges other sources own and never take them over; a detail read closes only FRC's
# own edges of that company.

SOURCE_BASE = "https://www.frc.mn/#/sct/org-detail/"    # the entity's page on frc.mn (its source)
LICENSE_API = os.getenv("FRC_LICENSE_API", "https://license.frc.mn/")
# list call -> kind
LICENSED = {
    "securitie/custodian": "custodian", "securitie/secbroker": "broker", "securitie/nominal": "nominal_account",
    "securitie/underwriter": "underwriter", "securitie/dealer": "dealer", "securitie/homk": "asset_manager",
    "securitie/hoz": "investment_adviser", "securitie/hiu": "trust_manager", "securitie/zereglel": "rating_agency",
    "insurance/insurance": "insurer", "insurance/longinsurance": "life_insurer", "insurance/reinsurance": "reinsurer",
    "insurance/insurancebroker": "insurance_broker",
}
# sector lists, read as frc/entity?id=<sector>&otid=<list menu> (the site's "org" pages)
SECTOR_LISTS = {"nbfi": (12, 275), "savings_coop": (14, 353), "money_lender": (1202, 1286)}
# kinds whose licence makes a holder a nominee candidate (data/reference/nominees.csv, #39)
NOMINEE_KINDS = ("custodian", "nominal_account", "broker", "trust_manager")
CEO_TITLE = "Гүйцэтгэх захирал"
BOARD_TITLE = "ТУЗ-ийн гишүүн"


def _text(v) -> str:
    import html as html_mod
    s = html_mod.unescape(str(v or "")).replace('"', " ").replace("“", " ").replace("”", " ")
    return re.sub(r"\s+", " ", s).strip()


def _date(v) -> str:
    """'6/24/2015 12:00:00 AM' or '2025-05-29T00:00:00' -> '2015-06-24'."""
    s = str(v or "").strip()
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else ""


def entity_row(rec: dict, kind: str) -> dict | None:
    """One licensed entity from a list record (None without a 7-digit register number)."""
    low = {k.lower(): v for k, v in rec.items()}
    reg = re.sub(r"\D", "", str(low.get("registration_number") or low.get("register") or low.get("regno") or ""))
    if not re.fullmatch(r"\d{7}", reg):
        return None
    state = re.sub(r"\D", "", str(low.get("state_number") or ""))
    lic = {"kind": kind, "activity": _text(low.get("industry_names") or low.get("financial_activities_name")),
           "number": _text(low.get("license_number")), "date": _date(low.get("license_date")),
           "decision": _text(low.get("decision_number"))}
    return {"register_no": reg, "state_reg_no": state if re.fullmatch(r"\d{10}", state) else "",
            "name": _text(low.get("entity_name") or low.get("name")), "ceo": _text(low.get("ceo")),
            "address": _text(low.get("address")), "licenses": [lic]}


def read_lists(fetch) -> tuple[dict[str, dict], dict]:
    """Every licensed entity, by register number, with all its licences. `fetch(path, **params)`
    returns parsed JSON or None. Returns the entities and the count per list."""
    found: dict[str, dict] = {}
    counts: dict[str, int] = {}

    def add(recs, kind):
        n = 0
        for rec in recs:
            row = entity_row(rec, kind)
            if not row:
                continue
            n += 1
            have = found.get(row["register_no"])
            if have is None:
                found[row["register_no"]] = row
            else:
                have["licenses"] += row["licenses"]
                for k in ("state_reg_no", "name", "ceo", "address"):
                    have[k] = have[k] or row[k]
        return n

    for path, kind in LICENSED.items():
        ans = fetch(path)
        if ans is not None:
            counts[path] = add(records(ans), kind)
    for kind, (sector, menu) in SECTOR_LISTS.items():
        ans = fetch("frc/entity", id=sector, otid=menu)
        recs = records(ans) if ans is not None else []
        if ans is not None and not recs:         # the page may carry the list menu as its own id
            ans = fetch("frc/entity", id=menu, otid=menu)
            recs = records(ans) if ans is not None else []
        if recs and kind not in _described:
            describe(ans, f"frc/entity {kind}")
            _described.add(kind)
        counts[kind] = add(recs, kind)
    return found, counts


_described: set[str] = set()


def _person(rec: dict) -> str:
    low = {k.lower(): v for k, v in rec.items()}
    last = _text(low.get("last_name") or low.get("lastname") or low.get("ovog") or "")
    first = _text(low.get("name") or low.get("first_name") or low.get("firstname") or low.get("ner") or "")
    return f"{last} {first}".strip() if last and first else (first or last)


def stakeholders(ans) -> list[dict]:
    """[{name, percent}] of the active stakeholders in a `tabinfo/stakeholder` answer."""
    out = []
    for rec in records(ans):
        low = {k.lower(): v for k, v in rec.items()}
        if str(low.get("is_active", "1")).strip() in ("0", "false", "False"):
            continue
        name = _person(rec)
        try:
            pct = float(str(low.get("stock_percentage") or low.get("percent") or "").replace(",", "."))
        except ValueError:
            pct = None
        if name:
            out.append({"name": name, "percent": pct})
    return out


def board(ans) -> list[dict]:
    """[{name, title}] from a license.frc.mn `token/tuz` answer (its fields aren't known yet: the
    common ones are tried, and an answer without a name is described in the log)."""
    out = []
    for rec in records(ans):
        low = {k.lower(): v for k, v in rec.items()}
        name = _person(rec)
        title = _text(low.get("position") or low.get("title") or low.get("albantushaal") or low.get("role")
                      or low.get("type_name") or low.get("typename") or "")
        if name:
            out.append({"name": name, "title": title or BOARD_TITLE})
    if records(ans) and not out:
        describe(ans, "license token/tuz (no names found)")
    return out


# the order details are read in: securities firms and insurers, then NBFIs, then the rest
KIND_ORDER = tuple(LICENSED.values()) + ("nbfi", "savings_coop", "money_lender")


def _detail_queue(g, entities: dict[str, dict], tickers: dict[str, str], budget: int, days: float) -> list[str]:
    """Register numbers whose shareholders and board to read this run: unlisted entities only (a listed
    company's come from mse.mn: FRC's are older filings), securities firms and insurers first, then
    NBFIs, then the rest; never-read before the oldest read; none read within `days`."""
    listed = {r["t"] for r in g.run("MATCH (c:Company) WHERE c.ticker IN $ts AND coalesce(c.listed, true) "
                                    "RETURN c.ticker AS t", ts=list(set(tickers.values())))}
    at = {r["t"]: r["at"] for r in g.run("MATCH (c:Company) WHERE c.ticker IN $ts RETURN c.ticker AS t, "
                                         "c.frc_detail_at AS at", ts=list(set(tickers.values())))}
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def rank(reg):
        kinds = [l["kind"] for l in entities[reg]["licenses"]]
        return min((KIND_ORDER.index(k) for k in kinds if k in KIND_ORDER), default=len(KIND_ORDER))
    due = [reg for reg in entities if tickers[reg] not in listed and (not at.get(tickers[reg]) or at[tickers[reg]] < cutoff)]
    due.sort(key=lambda reg: (bool(at.get(tickers[reg])), rank(reg), at.get(tickers[reg]) or "", reg))
    return due[:budget]


def withdraw_listed(g) -> dict:
    """Remove FRC shareholdings of listed companies (loaded by the first import on 2026-11-23, before the
    rule that mse.mn owns a listed company's shareholders): the edges, their source records and any
    holder left with no link at all. Idempotent."""
    holders = [r["k"] for r in g.run(
        "MATCH (h:Entity)-[o:OWNS]->(c:Company) WHERE coalesce(c.listed, true) AND o.source STARTS WITH $b "
        "RETURN DISTINCT h.key AS k", b=SOURCE_BASE)]
    if not holders:
        return {}
    edges = g.run("MATCH (:Entity)-[o:OWNS]->(c:Company) WHERE coalesce(c.listed, true) AND o.source STARTS WITH $b "
                  "DELETE o RETURN count(o) AS n", b=SOURCE_BASE)[0]["n"]
    recs = g.run("MATCH (r:Record {kind: 'shareholder'})-[:REPORTED_FOR]->(c:Company) "
                 "WHERE coalesce(c.listed, true) AND r.source STARTS WITH $b DETACH DELETE r RETURN count(r) AS n",
                 b=SOURCE_BASE)[0]["n"]
    gone = g.run("MATCH (h:Entity) WHERE h.key IN $ks AND NOT h:Company AND NOT (h)--() DELETE h RETURN count(h) AS n",
                 ks=holders)[0]["n"]
    return {"edges": edges, "records": recs, "holders": gone}


def build(g, entities: dict[str, dict], details: dict[str, dict], when: str):
    """The dataset for the graph: companies (new) or facts (known), CEOs, and for the entities in
    `details` ({reg: {"stakeholders": [...] | None, "board": [...] | None}}) their shareholders and
    board, each a complete picture (snapshot) when it was read. Returns (dataset, tickers)."""
    from .models import Company, CompanyFacts, Dataset, Position, Shareholding

    lookup = g.company_lookup()
    have_reg = {r["t"]: r["reg"] or "" for r in g.run("MATCH (c:Company) RETURN c.ticker AS t, c.register_no AS reg")}
    ds = Dataset(observed_at=when, supplementary=True)
    tickers: dict[str, str] = {}
    for reg, e in entities.items():
        src = SOURCE_BASE + reg
        t = lookup.get("reg:" + reg) or (lookup.get("reg:" + e["state_reg_no"]) if e["state_reg_no"] else None)
        if t is None:
            t = "~" + reg
            ds.companies.append(Company(ticker=t, name=e["name"] or reg, listed=False, register_no=reg,
                                        state_reg_no=e["state_reg_no"], address=e["address"], url=src))
        elif not have_reg.get(t):
            # a listed company known by its state number gets its register number (it opens its registry page)
            ds.facts.append(CompanyFacts(ticker=t, register_no=reg, reg_no=e["state_reg_no"]))
        tickers[reg] = t
        if e["ceo"]:
            ds.positions.append(Position(t, e["ceo"], CEO_TITLE, "executive", source=src))
        d = details.get(reg) or {}
        if d.get("stakeholders") is not None:
            for s in d["stakeholders"]:
                ds.shareholdings.append(Shareholding(t, s["name"], s["percent"], source=src))   # FRC gives no date
            ds.mark_snapshot("shareholdings", t, src)
        if d.get("board") is not None:
            for b in d["board"]:
                ds.positions.append(Position(t, b["name"], b["title"], "board", source=src))
            if e["ceo"] or d["board"]:
                ds.mark_snapshot("positions", t, src)
    return ds, tickers


def write_licenses(g, entities: dict[str, dict], tickers: dict[str, str], details: dict, when: str) -> int:
    rows = []
    for reg, e in entities.items():
        lic = [" · ".join(x for x in (l["activity"] or l["kind"], l["number"], l["date"]) if x) for l in e["licenses"]]
        rows.append({"t": tickers[reg], "lic": sorted(set(lic)), "kinds": sorted({l["kind"] for l in e["licenses"]}),
                     "url": SOURCE_BASE + reg, "detail": reg in details})
    g.run("""UNWIND $rows AS r MATCH (c:Company {ticker: r.t})
             SET c.frc_licenses = r.lic, c.frc_kinds = r.kinds, c.frc_url = r.url, c.frc_seen = $when,
                 c.frc_detail_at = CASE WHEN r.detail THEN $now ELSE c.frc_detail_at END""",
          rows=rows, when=when, now=datetime.now(timezone.utc).isoformat())
    # licences that FRC no longer lists
    gone = g.run("MATCH (c:Company) WHERE c.frc_seen IS NOT NULL AND c.frc_seen < $when "
                 "SET c.frc_licenses = [], c.frc_kinds = [] RETURN count(c) AS n", when=when)[0]["n"]
    return gone


def step(g, get=None, budget: int | None = None, delay: float | None = None,
         sleep: Callable[[float], None] = time.sleep, when: str | None = None) -> dict:
    """Read FRC's lists and this run's details, load them, and return a summary."""
    from datetime import date
    get = get or _client_get()
    delay = _env_float("FRC_DELAY", 5.0) if delay is None else delay
    budget = int(budget if budget is not None else _env_float("FRC_DETAIL_BUDGET", 20))
    days = _env_float("FRC_DETAIL_DAYS", 30)
    when = when or date.today().isoformat()
    report: dict = {"requests": 0, "errors": [], "lists": {}}

    def fetch(path: str, post: bool = False, base: str | None = None, **params):
        if report["requests"]:
            sleep(delay)
        report["requests"] += 1
        u = (base.rstrip("/") + "/" + path + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")
             if base else url(path, **params))
        try:      # license.frc.mn answers start with a byte-order mark
            return json.loads((get(u, post=True) if post else get(u)).lstrip("\ufeff"))
        except Exception as exc:
            report["errors"].append(f"{path}: {type(exc).__name__}: {str(exc)[:160]}")
            log.warning("FRC %s failed: %s", u, exc)
            return None

    entities, report["lists"] = read_lists(fetch)
    report["entities"] = len(entities)
    # not parsed yet: one decisions page (NBFI sector, this year) and the sanctions list, described
    # in the log (`FRC layout`) so their parsers can be written from real answers
    for what, path, params in (("decisions", "frc/decision", {"sctId": 12, "decId": when[:4]}),
                               ("sanctions", "news", {"menuid": 451, **SITE, "page": 0})):
        ans = fetch(path, **params)
        report["lists"][what] = len(records(ans)) if ans is not None else None
        if ans is not None and records(ans):
            describe(ans, f"{path} ({what})")
    if not entities:
        return report
    lookup = g.company_lookup()
    known = {reg: lookup.get("reg:" + reg) or lookup.get("reg:" + e["state_reg_no"]) or "~" + reg
             for reg, e in entities.items()}
    details: dict[str, dict] = {}
    for reg in _detail_queue(g, entities, known, budget, days):
        sh = fetch("tabinfo/stakeholder", post=True, register=reg)
        tz = fetch("token/tuz", post=True, base=LICENSE_API, register=reg)
        if sh is None and tz is None:
            continue
        if tz is not None and "tuz" not in _described:
            describe(tz, f"license token/tuz register={reg}")
            _described.add("tuz")
        details[reg] = {"stakeholders": stakeholders(sh) if sh is not None else None,
                        "board": board(tz) if tz is not None else None}
    report["withdrawn"] = withdraw_listed(g)
    ds, tickers = build(g, entities, details, when)
    loaded = g.load(ds, observed_at=when)
    gone = write_licenses(g, entities, tickers, details, when)
    listed = [t for t in tickers.values() if not t.startswith("~")]
    report.update(details=len(details), listed_matched=len(set(listed)),
                  registers_filled=len(ds.facts), new_companies=len(ds.companies),
                  shareholdings=len(ds.shareholdings), positions=len(ds.positions), licenses_gone=gone,
                  changes=loaded.get("changes", {}))
    return report


def mark_step(g, report: dict) -> None:
    g.run("MERGE (m:Meta {key: 'frc'}) SET m.at = $at, m.version = $v, m.requests = $r, m.entities = $n, "
          "m.errors = size($e)", at=datetime.now(timezone.utc).isoformat(), v=PROBE_VERSION, r=report["requests"],
          n=report.get("entities", 0), e=report["errors"])
