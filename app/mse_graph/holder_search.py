"""Holder search: which other companies does each person and organisation in the graph own?

Registry pages (/search/<reg no>) list a company's owners, but nothing links an owner to
the other companies it owns. Open Data Lab's name search does: /search?query=<name>
returns, besides companies of that name, the registry's shareholder records
("Эзэмшигч") of everyone with that name, each naming the company. A record's page,
/search/<id>?model=CFounder, links to that company's registry page, which gives its
register number. So, for each holder:

1. one search for its name (a person's full registry name, "Patronymic Given"; an
   organisation's name). Only records whose holder has exactly that name count: for a
   person the same merge key as in the graph (normalize.person_name), so "Батын Батболд"
   and "Баярын Батболд" stay apart. The registry has no personal ID, so two people with
   the same patronymic and given name can't be told apart: their holdings come together,
   as they already do in the graph. The search answers with its first 30 results only
   (`page=` is ignored), so a holder with more holdings than that shows its first ones;
2. for each company the graph doesn't have yet (by name), one request for a record page
   to get its register number (cached in (:HoldingLead) nodes, never asked twice);
3. the numbers become discovery leads (kind "holding", discovery.frontier) one step
   deeper than the holder, so the regular discovery run reads those registry pages,
   adds the companies with their owners and officers, and those people are searched in
   their turn. That is how the graph grows by itself.

An organisation's search also returns the organisation's own registry entry: when exactly
one entity has its name and the graph has no register number for it, that number becomes
a lead too ("self"). That is how listed companies, for which mse.mn gives only the 10-digit
state number, get their registry pages (discovery attaches a page to a listed company only
when it is a joint-stock company), and how an owner known only by name gets its page.

Every holder is searched again every `every_days` (7) days, a share of them each day:
`quota="auto"` takes ceil(holders / every_days) a day, so the daily share grows with the
graph, never-searched holders first, the closest to the listed companies first. A run
stops at its time budget; whoever is left is first in line the next day.

A block or challenge page served with HTTP 200 is no answer: a search counts only when a real
search page comes back (see `search`), so a block never parks holders as having no holdings.

Like every opendatalab.mn request: 11 s apart, never in parallel (opendatalab.RegistryLease).
robots.txt disallows /search?query=; the owner has an agreement with Open Data Lab that
this project may ignore it (CLAUDE.md), so the load is kept gentle instead.

    python -m mse_graph.cli odl-holders [--minutes M] [--quota N|auto|all] [--key KEY ...] [--dry-run]
"""
from __future__ import annotations

import logging
import math
import os
import re
import time
from urllib.parse import quote

from . import opendatalab
from .config import settings
from .normalize import clean, entity_key, person_name

log = logging.getLogger(__name__)

EVERY_DAYS = 7               # each holder is searched again after this many days
DEFAULT_MINUTES = 60.0       # time budget of one run
MAX_EMPTY_IN_A_ROW = 10      # name searches with no result at all, in a row, that stop a run
FOLLOW_ROLES = ("эзэмшигч",)  # record types followed: shareholders (founders)

# legal forms as the search results spell them out, after the company's name
_FORMS = [
    "хязгаарлагдмал хариуцлагатай компани", "хувьцаат компани", "төрийн өмчит үйлдвэрийн газар",
    "орон нутгийн өмчит үйлдвэрийн газар", "төрийн өмчит хувьцаат компани", "боловсролын сургалтын байгууллага",
    "гадаадын хөрөнгө оруулалттай компани", "хоршоо", "нөхөрлөл", "сан", "холбоо", "төрийн бус байгууллага",
    "ххк", "хк", "төхк", "ххн", "тбб", "гүтбб", "хмб", "ххк /гхо/",
]
_FORM_RE = re.compile(r"\s+(?:" + "|".join(re.escape(f) for f in sorted(_FORMS, key=len, reverse=True))
                      + r")(?:\s*/[^/]*/)?\s*$", re.I)
_RECORD_RE = re.compile(r"^/search/(\d+)\?model=(\w+)$")
_ENTITY_RE = re.compile(r"^(?:https?://[^/]+)?/search/(\d{6,8})$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def settings_from_env() -> dict:
    """HOLDER_SEARCH_MINUTES (0 turns the search off), HOLDER_SEARCH_QUOTA ("auto", "all" or
    a number), HOLDER_SEARCH_EVERY_DAYS."""
    return {
        "minutes": float(os.getenv("HOLDER_SEARCH_MINUTES") or DEFAULT_MINUTES),
        "quota": os.getenv("HOLDER_SEARCH_QUOTA") or "auto",
        "every_days": int(os.getenv("HOLDER_SEARCH_EVERY_DAYS") or EVERY_DAYS),
    }


def search_url(query: str) -> str:
    return settings.odl_base_url.rstrip("/") + "/search?query=" + quote(clean(query))


def record_url(record: str, model: str = "CFounder") -> str:
    return settings.odl_base_url.rstrip("/") + f"/search/{record}?model={model}"


def split_company(text: str) -> tuple[str, str]:
    """"Номинхолдинг Хязгаарлагдмал хариуцлагатай компани" -> ("Номинхолдинг", "Хязгаарлагдмал ...")."""
    text = clean(text).strip(" ,")
    m = _FORM_RE.search(text)
    if not m or m.start() == 0:
        return text, ""
    return text[:m.start()].strip(" ,"), m.group(0).strip()


def parse_search(html: str) -> list[dict]:
    """The records on a search page: {"record", "model", "holder", "role", "holder_type",
    "country", "company", "legal_form", "since"}. Results that are entities themselves
    (a company named like the query) are left out."""
    from bs4 import BeautifulSoup

    out = []
    for a in BeautifulSoup(html or "", "lxml").select("div.entity a[href]"):
        m = _RECORD_RE.match(a["href"].strip())
        if not m:
            continue
        h6, typ, body = a.find("h6"), a.find("p", class_="type"), a.find("p", class_="body")
        holder = " ".join(s.get_text(" ", strip=True) for s in h6.find_all("span")) if h6 else ""
        holder = clean(holder or (h6.get_text(" ", strip=True) if h6 else ""))
        parts = [clean(p) for p in (body.get_text(" ", strip=True) if body else "").split(",")]
        since = parts.pop() if parts and _DATE_RE.match(parts[-1]) else ""
        parts = [p for p in parts if p]
        country = parts[0] if parts else ""
        htype = parts[1] if len(parts) > 1 else ""
        company, form = split_company(", ".join(parts[2:]))
        out.append({"record": m.group(1), "model": m.group(2), "holder": holder,
                    "role": clean(typ.get_text(" ", strip=True)) if typ else "", "holder_type": htype,
                    "country": country, "company": company, "legal_form": form, "since": since})
    return out


def parse_entities(html: str) -> list[dict]:
    """The legal entities on a search page (results that are companies themselves, not
    records): {"reg", "name", "legal_form"}."""
    from bs4 import BeautifulSoup

    out = []
    for a in BeautifulSoup(html or "", "lxml").select("div.entity a[href]"):
        m = _ENTITY_RE.match(a["href"].strip())
        h6 = a.find("h6")
        if not m or not h6:
            continue
        hl = h6.find("hl")
        title = clean(h6.get_text(" ", strip=True))
        name = clean(hl.get_text(" ", strip=True)) if hl else ""
        form = clean(title[len(name):]) if name and title.startswith(name) else ""
        if not name:
            name, form = split_company(title)
        out.append({"reg": m.group(1), "name": name, "legal_form": form})
    return out


_SHORT_FORM_RE = re.compile(r"\s+(?:ххк|хк|төхк|төк|өхк|ххн|llc|jsc)\.?$", re.I)


def query_for(target: dict) -> str:
    """What to search for: a person's full name; an organisation's name without a trailing
    short legal form ("Улсын их дэлгүүр ХК" -> "Улсын их дэлгүүр")."""
    q = clean(target["query"])
    return q if target["kind"] == "person" else (_SHORT_FORM_RE.sub("", q) or q)


def parse_record(html: str) -> str:
    """The register number a record page (/search/<id>?model=CFounder) links to, or ""."""
    from bs4 import BeautifulSoup

    for a in BeautifulSoup(html or "", "lxml").find_all("a", href=True):
        m = _ENTITY_RE.match(a["href"].strip())
        if m:
            return m.group(1)
    return ""


def holder_key(row: dict) -> str:
    """The graph key of a record's holder: a person for "Иргэн", else an organisation."""
    if "иргэн" in row.get("holder_type", "").lower():
        return entity_key(row["holder"])
    return entity_key(row["holder"], "organization")


def company_key(name: str) -> str:
    return entity_key(name, "organization")


def matching(rows: list[dict], target: dict, roles=FOLLOW_ROLES) -> list[dict]:
    """The records whose holder is `target` (same key) in one of `roles`."""
    out = []
    for r in rows:
        if not r["company"] or not any(x in r["role"].lower() for x in roles):
            continue
        person = "иргэн" in r["holder_type"].lower()
        if (target["kind"] == "person") != person:
            continue
        keys = set(target.get("keys") or [target["key"]])
        if holder_key(r) in keys or (person and person_name(r["holder"]).key in keys):
            out.append(r)
    return out


def quota_for(total: int, quota, every_days: int = EVERY_DAYS) -> int | None:
    """Holders a run may search: "auto" = ceil(total / every_days), "all" = no limit, or a number."""
    if quota in (None, "", "all"):
        return None
    if str(quota) == "auto":
        return max(1, math.ceil(total / max(1, every_days)))
    return max(0, int(quota))


def search(g, fetcher=None, minutes: float = DEFAULT_MINUTES, quota="auto", every_days: int = EVERY_DAYS,
           keys: list[str] | None = None, progress=None,
           max_failures: int = opendatalab.MAX_CONSECUTIVE_FAILURES, max_empty: int = MAX_EMPTY_IN_A_ROW,
           clock=time.monotonic) -> dict:
    """Search the registry for the holdings of the holders due today (or `keys`) and keep
    what it finds as (:HoldingLead) nodes; the next discovery run reads the new companies.

    Only a real search page counts as an answer. A page served with HTTP 200 that has no
    result at all and shows a sign of a block or challenge page (opendatalab.challenge_marker)
    is a failed download: it leaves the cache, counts toward `max_failures` and the holder
    stays due. A page with no result and no such sign may be either (the search for a
    holder's own name normally finds at least that holder's record), so it is held back:
    the holder counts as searched with no holdings ("empty") only once a later search of
    the run returns a real result page; `max_empty` such pages in a row stop the run and
    leave those holders due. Until 2026-11-28 any page counted as "no holdings" and parked
    the holder for `every_days`.

    Returns a report: `searched` holders, `holdings` matched, `in_graph` (companies the
    graph has), `new` (register numbers it doesn't), `unresolved` records, `empty` (searched,
    nothing found), `unconfirmed` (empty answers the run ended on, still due), `requests`,
    `failed`, `left` (holders still due), `stopped`, and `found` per holder."""
    fetcher = fetcher or opendatalab._fetcher()
    total = g.holder_target_count()
    limit = quota_for(total, quota, every_days) if not keys else None
    targets = g.holder_targets(every_days, limit=limit, keys=keys)
    lookup = g.company_lookup()
    regs = {v["reg"]: t for t, v in g.company_registers().items() if v["reg"]}
    known = g.holding_records()
    deadline = clock() + minutes * 60 if minutes else None
    report = {"total": total, "quota": limit, "due": len(targets), "searched": 0, "holdings": 0, "in_graph": 0,
              "new": 0, "unresolved": 0, "self": 0, "empty": 0, "unconfirmed": 0, "requests": 0, "failed": [],
              "found": [], "stopped": ""}
    failures = 0
    empty: list[tuple[dict, str]] = []     # (holder, url) of empty answers waiting for a real page

    def failed() -> None:
        nonlocal failures
        failures += 1
        if failures >= max_failures and not report["stopped"]:
            report["stopped"] = (f"{failures} opendatalab.mn requests in a row failed; "
                                 "check the route: python -m mse_graph.cli egress-check")

    def forget(url) -> None:
        if hasattr(fetcher, "forget"):
            fetcher.forget(url)

    def get(url):
        html = fetcher.get(url)
        report["requests"] += 1
        if progress:
            progress()
        if html is None:
            failed()
        return html

    def blocked(url, html) -> str:
        """Why `html` (in which nothing was found) is no real answer, or ""."""
        marker = opendatalab.challenge_marker(html)
        if marker:
            forget(url)
            failed()
            log.warning("HOLDERS %s: answered HTTP 200 with a block or challenge page (shows %r)", url, marker)
        return marker

    def searched_empty(pending) -> None:
        g.note_holder_searched([{"key": t["key"], "found": 0, "new": 0} for t, _ in pending])
        for t, _ in pending:
            report["searched"] += 1
            report["empty"] += 1
            report["found"].append({"key": t["key"], "name": t["name"], "holdings": 0, "new": 0, "companies": []})
            log.info("HOLDERS %s: no results", t["name"])

    for t in targets:
        if report["stopped"]:
            break
        if deadline is not None and clock() >= deadline:
            report["stopped"] = f"time budget of {minutes:g} minutes used"
            break
        url = search_url(query_for(t))
        html = get(url)
        if html is None:
            report["failed"].append({"key": t["key"], "error": getattr(fetcher, "last_error", "")})
            continue
        records, entities = parse_search(html), parse_entities(html)
        if not records and not entities:
            marker = blocked(url, html)
            if marker:
                report["failed"].append({"key": t["key"], "error": f"a block or challenge page (shows {marker!r})"})
                continue
            empty.append((t, url))
            if len(empty) >= max_empty:
                report["stopped"] = (f"{len(empty)} opendatalab.mn name searches in a row came back with no "
                                     "result at all (a block or challenge page?); those holders stay due")
                for e, u in empty:
                    forget(u)
                    report["failed"].append({"key": e["key"], "error": "no result at all (unconfirmed)"})
                empty.clear()
            continue
        failures = 0
        if empty:                         # a real page: the empty answers before it were real too
            searched_empty(empty)
            empty.clear()
        rows = matching(records, t)
        self_leads = []
        if t["kind"] == "organization" and not t.get("reg"):
            # the organisation's own registry entry: its register number, when one entity has its name
            own = set(t.get("keys") or [t["key"]])
            same = {e["reg"]: e for e in entities if company_key(e["name"]) in own}
            if len(same) == 1:
                e = next(iter(same.values()))
                ticker = regs.get(e["reg"])
                self_leads.append({"record": "entity:" + e["reg"], "model": "entity", "company": e["name"],
                                   "company_key": t["key"], "reg": e["reg"], "ticker": ticker,
                                   "holder_key": t["key"], "holder_name": t["name"], "role": "self",
                                   "since": None, "depth": int(t["depth"] or 0),
                                   "status": "in graph" if ticker else "new"})
                report["self"] += not ticker
        # one record per company (the newest), companies the graph has need no record page
        by_company: dict[str, dict] = {}
        for r in sorted(rows, key=lambda r: r["since"], reverse=True):
            by_company.setdefault(company_key(r["company"]), r)
        leads, new = [], 0
        for ck, r in by_company.items():
            ticker = lookup.get(ck) or lookup.get(entity_key(r["company"]))
            reg = known.get(r["record"])
            status = "in graph" if ticker else ""
            if not ticker and not reg:
                if report["stopped"] or (deadline is not None and clock() >= deadline):
                    status = "unresolved"          # next search of this holder resolves it
                else:
                    rurl = record_url(r["record"], r["model"])
                    page = get(rurl)
                    reg = parse_record(page or "")
                    if reg:
                        failures = 0
                    elif page is not None:
                        # no link to a company: a block page (a failure) or an odd record; either
                        # way not replayed from the cache when the holder is searched again
                        if not blocked(rurl, page):
                            forget(rurl)
                    status = "" if reg else "unresolved"
            if reg and not ticker:
                ticker = regs.get(reg)
                status = "in graph" if ticker else "new"
            if reg:
                known[r["record"]] = reg
            report["holdings"] += 1
            report["in_graph" if status == "in graph" else "new" if status == "new" else "unresolved"] += 1
            new += status == "new"
            leads.append({"record": r["record"], "model": r["model"], "company": r["company"], "company_key": ck,
                          "reg": reg or None, "ticker": ticker or None, "holder_key": t["key"],
                          "holder_name": t["name"], "role": r["role"], "since": r["since"] or None,
                          "depth": int(t["depth"] or 0) + 1, "status": status})
        g.save_holding_leads(leads + self_leads)
        g.note_holder_searched([{"key": t["key"], "found": len(by_company), "new": new}])
        report["searched"] += 1
        report["found"].append({"key": t["key"], "name": t["name"], "holdings": len(by_company), "new": new,
                                "companies": [f"{x['company']} ({x['reg'] or '?'}, {x['status']})" for x in leads]})
        log.info("HOLDERS [%d/%d] %s: %d holdings, %d new", report["searched"], len(targets), t["name"],
                 len(by_company), new)
    if empty:
        # the run ended on empty answers that no real page followed: they stay due (first in line
        # next time) rather than being parked for `every_days` on an answer that may be a block page
        report["unconfirmed"] = len(empty)
        for _, u in empty:
            forget(u)
    report["left"] = len(g.holder_targets(every_days)) if not keys else 0
    return report


def summary(report: dict, names: int = 15) -> dict:
    out = {k: report[k] for k in ("total", "quota", "searched", "holdings", "in_graph", "new", "unresolved",
                                  "self", "empty", "unconfirmed", "requests", "left") if k in report}
    if report.get("failed"):
        out["failed"] = len(report["failed"])
    top = sorted(report.get("found", []), key=lambda f: -f["new"])[:names]
    out["most_new"] = [f"{f['name']}: {f['new']} new of {f['holdings']}" for f in top if f["new"]]
    if report.get("stopped"):
        out["stopped"] = report["stopped"]
    return out
