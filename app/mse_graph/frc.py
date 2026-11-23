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
PROBE_VERSION = 8      # 4: the import (step 2); 5: no FRC shareholders for listed companies, BOM fix; 6: decisions; 7: name matching;
#                        8: no people from FRC (initial-form names): Open Data Lab gives them

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
# `~<register no>` companies. Each keeps its licences (`frc_licenses`, `frc_kinds`).
#
# No people or shareholders come from FRC (owner, 2026-11-23): FRC writes people in initial form
# ("Я.Мягмарсүрэн": the first letter of the patronymic and the given name), which can't tell
# namesakes apart and merged different people. The register number brings each company into the
# Open Data Lab registry rotation instead (discovery reads a page never read, then a seventh of the
# unlisted companies a day), whose pages give officers, shareholders and beneficial owners with full
# names. `withdraw_people` removed what earlier imports had loaded.

SOURCE_BASE = "https://www.frc.mn/#/sct/org-detail/"    # the entity's page on frc.mn (its source)
LICENSE_API = os.getenv("FRC_LICENSE_API", "https://license.frc.mn/")      # the decisions' PDFs
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
            "name": _text(low.get("entity_name") or low.get("name")), "address": _text(low.get("address")), "licenses": [lic]}


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
                for k in ("state_reg_no", "name", "address"):
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


def withdraw_people(g) -> dict:
    """Remove every shareholding and position FRC gave (imports before 2026-11-23's version 8: CEOs,
    board members, stakeholders), current or past: an edge only FRC gives goes, one another source
    also gives loses FRC from its sources. Then FRC's source records, people and holders left with no
    link at all, FRC's spellings in `name_variants` and `frc_detail_at`. Idempotent."""
    b = SOURCE_BASE
    srcs = "coalesce(r.sources, [x IN [r.source] WHERE x IS NOT NULL])"
    keys = [x["k"] for x in g.run(
        f"MATCH (e:Entity)-[r:OWNS|HOLDS_POSITION]->(:Company) WHERE any(s IN {srcs} WHERE s STARTS WITH $b) "
        "RETURN DISTINCT e.key AS k", b=b)]
    out = {}
    if keys:
        out["edges"] = g.run(
            f"MATCH (:Entity)-[r:OWNS|HOLDS_POSITION]->(:Company) WHERE all(s IN {srcs} WHERE s STARTS WITH $b) "
            "DELETE r RETURN count(r) AS n", b=b)[0]["n"]
        out["sources_dropped"] = g.run(
            f"MATCH (:Entity)-[r:OWNS|HOLDS_POSITION]->(:Company) WHERE any(s IN {srcs} WHERE s STARTS WITH $b) "
            f"WITH r, [s IN {srcs} WHERE NOT s STARTS WITH $b] AS keep "
            "SET r.sources = keep, r.source = CASE WHEN r.source STARTS WITH $b THEN keep[0] ELSE r.source END "
            "RETURN count(r) AS n", b=b)[0]["n"]
    out["records"] = g.run("MATCH (r:Record) WHERE r.source STARTS WITH $b DETACH DELETE r RETURN count(r) AS n",
                           b=b)[0]["n"]
    if keys:
        out["entities"] = g.run("MATCH (e:Entity) WHERE e.key IN $ks AND NOT e:Company AND NOT (e)--() "
                                "DELETE e RETURN count(e) AS n", ks=keys)[0]["n"]
    g.run("MATCH (e:Entity) WHERE any(v IN coalesce(e.name_variants, []) WHERE v CONTAINS $b) "
          "SET e.name_variants = [v IN e.name_variants WHERE NOT v CONTAINS $b]", b=b)
    g.run("MATCH (c:Company) WHERE c.frc_detail_at IS NOT NULL REMOVE c.frc_detail_at")
    return {k: v for k, v in out.items() if v}


def build(g, entities: dict[str, dict], when: str):
    """The dataset for the graph: companies (new) or facts (known); no people, no shareholders
    (see above). Returns (dataset, tickers)."""
    from .models import Company, CompanyFacts, Dataset

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
    return ds, tickers


def write_licenses(g, entities: dict[str, dict], tickers: dict[str, str], when: str) -> int:
    rows = []
    for reg, e in entities.items():
        lic = [" · ".join(x for x in (l["activity"] or l["kind"], l["number"], l["date"]) if x) for l in e["licenses"]]
        rows.append({"t": tickers[reg], "lic": sorted(set(lic)), "kinds": sorted({l["kind"] for l in e["licenses"]}),
                     "url": SOURCE_BASE + reg})
    g.run("""UNWIND $rows AS r MATCH (c:Company {ticker: r.t})
             SET c.frc_licenses = r.lic, c.frc_kinds = r.kinds, c.frc_url = r.url, c.frc_seen = $when""",
          rows=rows, when=when)
    # licences that FRC no longer lists
    gone = g.run("MATCH (c:Company) WHERE c.frc_seen IS NOT NULL AND c.frc_seen < $when "
                 "SET c.frc_licenses = [], c.frc_kinds = [] RETURN count(c) AS n", when=when)[0]["n"]
    return gone


def step(g, get=None, delay: float | None = None,
         sleep: Callable[[float], None] = time.sleep, when: str | None = None) -> dict:
    """Read FRC's lists and decisions, load them, and return a summary."""
    from datetime import date
    get = get or _client_get()
    delay = _env_float("FRC_DELAY", 5.0) if delay is None else delay
    when = when or date.today().isoformat()
    report: dict = {"requests": 0, "errors": [], "lists": {}}

    def fetch(path: str, **params):
        if report["requests"]:
            sleep(delay)
        report["requests"] += 1
        u = url(path, **params)
        try:      # some answers start with a byte-order mark
            return json.loads(get(u).lstrip("\ufeff"))
        except Exception as exc:
            report["errors"].append(f"{path}: {type(exc).__name__}: {str(exc)[:160]}")
            log.warning("FRC %s failed: %s", u, exc)
            return None

    entities, report["lists"] = read_lists(fetch)
    report["entities"] = len(entities)
    if not entities:
        return report
    report["withdrawn"] = withdraw_people(g)
    ds, tickers = build(g, entities, when)
    loaded = g.load(ds, observed_at=when)
    gone = write_licenses(g, entities, tickers, when)
    # the commission's decisions, linked to the companies they name (FRC's names first)
    names: dict[str, str] = {}
    clash: set[str] = set()
    for reg, e in entities.items():
        c = core_name(e["name"])
        if c in names and names[c] != tickers[reg]:
            clash.add(c)
        names.setdefault(c, tickers[reg])
    for c in clash:                    # a core name two licensed entities share links neither
        names.pop(c, None)
    decisions, report["decision_lists"] = read_decisions(g, fetch, int(when[:4]))
    report["decisions"] = write_decisions(g, decisions, names, when)
    listed = [t for t in tickers.values() if not t.startswith("~")]
    report.update(listed_matched=len(set(listed)), registers_filled=len(ds.facts),
                  new_companies=len(ds.companies), licenses_gone=gone,
                  changes=loaded.get("changes", {}))
    return report


def mark_step(g, report: dict) -> None:
    g.run("MERGE (m:Meta {key: 'frc'}) SET m.at = $at, m.version = $v, m.requests = $r, m.entities = $n, "
          "m.errors = size($e)", at=datetime.now(timezone.utc).isoformat(), v=PROBE_VERSION, r=report["requests"],
          n=report.get("entities", 0), e=report["errors"])


# ---- the commission's decisions (FRC step 3) -----------------------------------------------------
# `frc/decision?sctId=<sector>&decId=<year>` lists a sector's decisions (тогтоол) of one year: name,
# number, date, type and a PDF on license.frc.mn. Each becomes a (:FrcDecision) node (not the review
# queue's (:Decision)) that CONCERNS the companies its name gives in brackets ("... түдгэлзүүлэх тухай
# ("МТ финанс ББСБ" ХХК)"). Adverse ones (suspension, revocation, penalty, liquidation, temporary
# administration, restriction, warning) are company flags for FRC_FLAG_YEARS (3). This year's lists
# are read every run; earlier years back to FRC_DECISION_SINCE (2021), FRC_DECISION_BACKFILL (10) a run.

DECISION_SECTORS = {11: "securities", 12: "nbfi", 13: "insurance", 14: "savings_coop", 1202: "money_lender"}
# (pattern, kind, adverse): the first that matches the decision's name
DECISION_KINDS = (
    (r"түдгэлзүүл", "suspension", True),
    (r"зөвшөөрл\w*\s+хүчингүй|хүчингүй\s+болго\w*.*зөвшөөр", "revocation", True),
    (r"торго|шийтгэ|төлбөр\s+ногдуул", "penalty", True),
    (r"татан\s+буулга|дампуур", "liquidation", True),
    (r"түр\s+удирд|эрх\s+хязгаарла|хяналтын\s+дэглэм|онцгой\s+дэглэм", "administration", True),
    (r"хязгаарла", "restriction", True),
    (r"сануулга|анхааруул", "warning", True),
    (r"сэргээ", "restoration", False),
    (r"зөвшөөрөл\s+олго|олгох\s+тухай|бүртгэ", "grant", False),
)


def decision_kind(name: str) -> tuple[str, bool]:
    low = (name or "").lower()
    return next(((k, adv) for rx, k, adv in DECISION_KINDS if re.search(rx, low)), ("other", False))


_MARK = r"ХХК|ХК|ТӨХК|ТӨҮГ|ББСБ|ХЗХ|ҮЦК|ТЗК|ХААБ|LLC|JSC|ХХН"
# a name with a legal form at the start of the decision: "Өндөрхаан инвест ҮЦК ХХК-ийн ... тухай"
_LEADING = re.compile(rf"^\s*(.{{2,100}}?(?:{_MARK}))\s*-?\s*(?:ийн|ын|ий|ны|ний|н|д|т|ийг|ыг)\b")


def named_companies(name: str) -> list[str]:
    """The companies a decision names: in brackets ('("МТ финанс ББСБ" ХХК)'; several split on commas
    and 'болон'), only parts that look like a name (quoted, or with a legal form or licence tag, so not
    "(брокер)"); else a name with a legal form at the start ("Масдак ҮЦК ХХК-ийн ..."). Quotes dropped."""
    import html as html_mod
    raw = html_mod.unescape(str(name or ""))
    out = []
    for inner in re.findall(r"\(([^()]{3,200})\)?", raw):
        for part in re.split(r",|;|\sболон\s", inner):
            if re.search(rf"[\"“”«»]|(?<!\w)(?:{_MARK})(?!\w)", part):
                part = _text(part)
                if len(part) >= 3 and part not in out:
                    out.append(part)
    if not out:
        m = _LEADING.match(_text(raw))
        if m:
            out.append(_text(m.group(1)))
    return out


_FORMS_RE = re.compile(rf"(?<!\w)(?:{_MARK.lower()}|ltd|inc)(?!\w)")


def core_name(name: str) -> str:
    """A name without legal forms, licence tags, quotes, punctuation or spaces, for matching a decision's
    "Гэрэлфинанс ББСБ ХХК" to FRC's "Гэрэл финанс": 'гэрэлфинанс'."""
    n = _text(name).casefold()
    n = _FORMS_RE.sub(" ", n)
    return re.sub(r"[\W_]+", "", n)


def decision_row(rec: dict, sector: int) -> dict | None:
    low = {k.lower(): v for k, v in rec.items()}
    name = _text(low.get("decision_name"))
    if not name:
        return None
    date, number = _date(low.get("decision_date")), _text(low.get("decision_number"))
    kind, adverse = decision_kind(name)
    f = str(low.get("file_url") or "").strip()
    return {"key": f"frc:{sector}:{number or name[:40]}:{date}", "sector": DECISION_SECTORS.get(sector, str(sector)),
            "name": name, "number": number, "date": date, "type": _text(low.get("decision_type_name")),
            "kind": kind, "adverse": adverse, "companies": named_companies(low.get("decision_name")),   # raw: quotes count
            "url": (LICENSE_API.rstrip("/") + "/" + f.lstrip("/")) if f and not f.startswith("http") else (f or None)}


def read_decisions(g, fetch, year: int) -> tuple[list[dict], dict]:
    """This year's decisions of every sector, and up to FRC_DECISION_BACKFILL earlier sector-years not
    read yet. Returns the decisions and the count per sector-year read."""
    since = int(_env_float("FRC_DECISION_SINCE", 2021))
    backfill = int(_env_float("FRC_DECISION_BACKFILL", 10))
    done = set((g.run("MATCH (m:Meta {key: 'frc_decisions'}) RETURN m.done AS d") or [{"d": []}])[0]["d"] or [])
    todo = [(s, year) for s in DECISION_SECTORS]
    todo += [(s, y) for y in range(year - 1, since - 1, -1) for s in DECISION_SECTORS if f"{s}:{y}" not in done][:backfill]
    out, counts = [], {}
    for sector, y in todo:
        ans = fetch("frc/decision", sctId=sector, decId=y)
        if ans is None:
            continue
        rows = [r for r in (decision_row(x, sector) for x in records(ans)) if r]
        counts[f"{sector}:{y}"] = len(rows)
        out += rows
        if y < year:
            done.add(f"{sector}:{y}")
    g.run("MERGE (m:Meta {key: 'frc_decisions'}) SET m.done = $d", d=sorted(done))
    return out, counts


def write_decisions(g, decisions: list[dict], names: dict[str, str], when: str) -> dict:
    """Store the decisions and link each to the companies it names, by core name (`core_name`): FRC's
    licensed entities of this run first (`names`: core -> ticker), then the graph's companies. A core
    name that fits several companies links none."""
    graph_cores: dict[str, set] = {}
    for r in g.run("MATCH (c:Company) RETURN c.ticker AS t, [c.name, c.registry_name] AS ns"):
        for n in r["ns"]:
            if n:
                graph_cores.setdefault(core_name(n), set()).add(r["t"])
    rows = []
    for d in decisions:
        tickers = []
        for n in d["companies"]:
            c = core_name(n)
            t = names.get(c)
            if t is None and len(graph_cores.get(c, ())) == 1:
                t = next(iter(graph_cores[c]))
            if t and t not in tickers:
                tickers.append(t)
        rows.append({**{k: v for k, v in d.items() if k != "companies"}, "company_names": d["companies"],
                     "tickers": tickers})
    g.run("""UNWIND $rows AS r
             MERGE (d:FrcDecision {key: r.key})
             ON CREATE SET d.first_seen = $when
             SET d.sector = r.sector, d.name = r.name, d.number = r.number, d.date = r.date, d.type = r.type,
                 d.kind = r.kind, d.adverse = r.adverse, d.url = r.url, d.company_names = r.company_names,
                 d.last_seen = $when
             WITH d, r
             OPTIONAL MATCH (d)-[old:CONCERNS]->(x:Company) WHERE NOT x.ticker IN r.tickers
             DELETE old
             WITH DISTINCT d, r
             UNWIND r.tickers AS t
             MATCH (c:Company {ticker: t})
             MERGE (d)-[:CONCERNS]->(c)""", rows=rows, when=when)
    linked = sum(1 for r in rows if r["tickers"])
    return {"decisions": len(rows), "linked": linked, "adverse": sum(1 for r in rows if r["adverse"]),
            "adverse_linked": sum(1 for r in rows if r["adverse"] and r["tickers"]),
            "named": sum(1 for r in rows if r["company_names"])}


def company_decisions(g, key: str, limit: int = 50) -> list[dict]:
    """The FRC decisions that name a company, newest first."""
    return g.run("""MATCH (d:FrcDecision)-[:CONCERNS]->(c:Company {key: $key})
                    RETURN d.date AS date, d.number AS number, d.name AS name, d.kind AS kind, d.adverse AS adverse,
                           d.sector AS sector, d.url AS url
                    ORDER BY d.date DESC LIMIT $limit""", key=key, limit=limit)


def decisions_report(g, adverse_only: bool = True, since: str = "", limit: int = 500) -> dict:
    """FRC decisions across the market, newest first, with the companies they name."""
    rows = g.run("""MATCH (d:FrcDecision) WHERE (NOT $adv OR d.adverse) AND d.date >= $since
                    OPTIONAL MATCH (d)-[:CONCERNS]->(c:Company)
                    WITH d, collect({key: c.key, ticker: c.ticker, name: c.name, listed: coalesce(c.listed, true)}) AS cs
                    RETURN d.date AS date, d.number AS number, d.name AS name, d.kind AS kind, d.adverse AS adverse,
                           d.sector AS sector, d.url AS url, [x IN cs WHERE x.key IS NOT NULL] AS companies,
                           d.company_names AS named
                    ORDER BY d.date DESC, d.number DESC LIMIT $limit""", adv=adverse_only, since=since, limit=limit)
    kinds: dict[str, int] = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    return {"items": rows, "summary": {"decisions": len(rows), "kinds": kinds,
                                       "linked": sum(1 for r in rows if r["companies"]),
                                       "listed": sum(1 for r in rows if any(c["listed"] for c in r["companies"]))}}
