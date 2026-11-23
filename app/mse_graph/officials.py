"""Public officials whose declarations name a company, plus licences and tenders (issue #9).

Every company page on Open Data Lab's registry mirror (opendatalab.mn/search/<register no>)
ends with three lists this module reads:

* "Хамаарал бүхий нийтийн албан тушаалтан": public officials whose asset and income
  declaration (ХОМ, filed with the IAAC) mentions the company. One row per declaration year;
  the official is one cell, "<agency> <title> <initial>.<given name>" ("Монгол банк Салбарын
  захирал Х.Алтангэрэл"), and the year links to the declaration. Rows starting with
  "АТГ-т мэдүүлгээ бүртгүүлдэг албан тушаалтнууд" are senior officials who file with the IAAC
  itself (members of parliament, heads of agencies): `iaac`.
* "Тусгай зөвшөөрөл": the company's special licences (code, name, valid from / to, issuer).
* "Оролцсон тендер": public tenders it bid in, with the bid's status (Шалгарсан = won).

A declaration that mentions a company is a **disclosed interest, not a finding**: it can be a
shareholding, a job, a loan or a relative's link, and the page doesn't say which. Credit Open
Data Lab as the source.

Graph
-----
    (:Official {key, name, position, iaac, match_key, given_name, initial, source, first_seen, last_seen})
    (:Official)-[:DECLARED_INTEREST {years, reports (the declaration of each year), first_year, last_year, source, current, first_seen,
                                     last_seen, until, closed_at, opened_run, closed_run, baseline}]->(:Company)
    (:Company {licenses, license_count, tenders, tender_count, tenders_won, registry_lists_at})

Officials are **not** :Entity :Person nodes. Their names are initials only ("Ц.Алтанцэцэг"), so
merging them into people would join namesakes even more often than MSE's initials do (#36). An
official is keyed by name and position (`official:<initial name>|<position>`): the same official
declaring several companies is one node, a new post is a new node. Graph views, search, metrics
and the person counts never see them. `possible_people()` lists the people at the same company
whose name fits (same initial and given name); they are hypotheses for a reviewer, served only
with the review password, never on the public site, until a confirmed match is modelled.

A registry page read is the complete list for its company: a declared interest the page no
longer shows is closed (`current: false`, `until`), the first read of a company is a baseline.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict

from .models import Dataset, DeclaredInterest, RegistryLists
from .normalize import clean, person_name

log = logging.getLogger(__name__)

SOURCE = "opendatalab.mn"
# Rows of senior officials who file their declarations with the IAAC itself.
IAAC_RE = re.compile(r"^\s*атг-т\s+мэдүүлгээ\s+бүртгүүлдэг\s+албан\s+тушаалтнууд\s*", re.IGNORECASE)
# The official's name ends the cell: one or more initials with a dot, then the given name
# ("Ц.Алтанцэцэг", "С. Батболд", "Я.Мөнх-Эрдэнэ", "Г.ГАНЗУЛ").
_NAME_RE = re.compile(r"((?:[^\W\d_]{1,3}\.\s*)+[^\W\d_]+(?:-[^\W\d_]+)*)\s*$")
TENDER_WON = "шалгарсан"


def parse_official(text: str) -> tuple[str, str, bool]:
    """Split a registry cell into (name, position, iaac). The name is "" when the cell
    doesn't end with an initial-form name (then the row can't be used)."""
    text = clean(text)
    iaac = bool(IAAC_RE.match(text))
    body = IAAC_RE.sub("", text) if iaac else text
    m = _NAME_RE.search(body)
    if not m:
        return "", clean(body), iaac
    name = re.sub(r"\.\s+", ".", m.group(1))
    return name, clean(body[:m.start()]).rstrip(" ,;/-"), iaac


def _fold(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s-]", " ", clean(text).casefold())).strip()


def official_key(name: str, position: str) -> str:
    """`official:<initial-form name key>|<folded position>`: "С. Батболд" and "С.Батболд" in
    the same post are one official."""
    return f"official:{person_name(name).key.removeprefix('person:')}|{_fold(position)}"


def _get(rec: dict, *names: str) -> str:
    for n in names:
        for k, v in rec.items():
            if not k.startswith("_") and (k == n or k.startswith(n)):
                return v
    return ""


def registry_lists(page: dict, ticker: str, url: str) -> RegistryLists:
    """The officials, licences and tenders of a parsed registry page (opendatalab.parse_registry_page)."""
    out = RegistryLists(ticker=ticker, source=url)
    skipped = 0
    for r in page.get("officials", []):
        if r.get("_removed"):
            continue
        name, position, iaac = parse_official(_get(r, "албан тушаал"))
        if not name:
            skipped += 1
            continue
        year = re.search(r"\d{4}", _get(r, "хом", "огноо"))
        reports = [u for u in r.get("_links", []) if "xreport" in u.lower()] or r.get("_links", [])
        out.declarations.append(DeclaredInterest(name, position, year.group(0) if year else "",
                                                 reports[0] if reports else "", iaac))
    if skipped:
        log.info("ODL %s: %d official rows without a name at the end were left out", ticker, skipped)
    for r in page.get("licenses", []):
        if r.get("_removed"):
            continue
        head = " ".join(x for x in (_get(r, "код"), _get(r, "нэр")) if x)
        parts = [head, _get(r, "олгосон"), _get(r, "хүчинтэй")]
        if head:
            out.licenses.append(" | ".join(p for p in parts if p))
    for r in page.get("tenders", []):       # every bid: "Хасагдсан" is a rejected bid here
        name = _get(r, "нэр")
        if name:
            status = _get(r, "төлөв")
            out.tenders.append(f"{status}: {name}" if status else name)
    return out


# ---- loading ----------------------------------------------------------------------------

def _rows(ds: Dataset, baseline: set[str]) -> list[dict]:
    """One row per (company, official): the declaration years and reports collected."""
    by: dict[tuple[str, str], dict] = {}
    for lists in ds.registry_lists:
        t = lists.ticker.upper()
        for d in lists.declarations:
            key = official_key(d.official_name, d.position)
            pn = person_name(d.official_name)
            row = by.setdefault((t, key), {
                "ticker": t, "key": key, "name": d.official_name, "position": d.position, "iaac": d.iaac,
                "match_key": pn.match_key, "given": pn.given, "initial": pn.initial, "source": lists.source,
                "by_year": {}, "baseline": t in baseline})
            if d.year or d.report_url:
                row["by_year"][d.year] = row["by_year"].get(d.year) or d.report_url
    out = []
    for row in by.values():
        by_year = row.pop("by_year")
        years = sorted(by_year)                 # `reports[i]` is the declaration of `years[i]`
        known = [y for y in years if y]
        out.append({**row, "years": years, "reports": [by_year[y] for y in years],
                    "first_year": known[0] if known else None, "last_year": known[-1] if known else None})
    return out


def load(g, ds: Dataset, when: str, run: str) -> dict:
    """Write the dataset's registry lists (Graph.load calls this). Returns the change counts."""
    if not ds.registry_lists:
        return {}
    pages = {}
    for lists in ds.registry_lists:      # the last read of a company wins
        pages[lists.ticker.upper()] = lists
    page_rows = [{"ticker": t, "licenses": p.licenses, "tenders": p.tenders,
                  "won": sum(1 for x in p.tenders if x.casefold().startswith(TENDER_WON)),
                  "keys": sorted({official_key(d.official_name, d.position) for d in p.declarations})}
                 for t, p in pages.items()]
    # a company's first read is its baseline: what it shows then is not a change
    baseline = {r["t"] for r in g.run(
        "UNWIND $t AS t MATCH (c:Company {ticker: t}) WHERE c.registry_lists_at IS NULL RETURN t",
        t=sorted(pages))}
    rows = _rows(Dataset(registry_lists=list(pages.values())), baseline)
    g.run(
        """
        UNWIND $pages AS p
        MATCH (c:Company {ticker: p.ticker})
        SET c.licenses = p.licenses, c.license_count = size(p.licenses),
            c.tenders = p.tenders, c.tender_count = size(p.tenders), c.tenders_won = p.won,
            c.registry_lists_at = $when
        """, pages=page_rows, when=when)
    opened = g.run(
        """
        UNWIND $rows AS r
        MATCH (c:Company {ticker: r.ticker})
        MERGE (o:Official {key: r.key})
        ON CREATE SET o.first_seen = $when, o.name = r.name, o.position = r.position, o.iaac = r.iaac,
                      o.match_key = r.match_key, o.given_name = r.given, o.initial = r.initial, o.source = $src
        SET o.last_seen = $when
        MERGE (o)-[d:DECLARED_INTEREST]->(c)
        ON CREATE SET d.first_seen = $when, d.baseline = r.baseline
        WITH d, r, NOT coalesce(d.current, false) AS opened      // new, or reported again after it ended
        SET d.years = r.years, d.reports = r.reports, d.first_year = r.first_year, d.last_year = r.last_year,
            d.source = r.source, d.current = true, d.last_seen = $when,
            d.until = null, d.closed_at = null, d.closed_run = null,
            d.opened_run = CASE WHEN opened THEN $run ELSE d.opened_run END
        RETURN count(CASE WHEN opened AND NOT r.baseline THEN 1 END) AS n
        """, rows=rows, when=when, run=run, src=SOURCE)[0]["n"]
    closed = g.run(
        """
        UNWIND $pages AS p
        MATCH (o:Official)-[d:DECLARED_INTEREST {current: true}]->(c:Company {ticker: p.ticker})
        WHERE NOT o.key IN p.keys
        SET d.current = false, d.until = $when, d.closed_at = $when, d.closed_run = $run
        RETURN count(d) AS n
        """, pages=page_rows, when=when, run=run)[0]["n"]
    return {"declared_interests_opened": opened, "declared_interests_closed": closed}


def move(g, old: str, new: str) -> None:
    """Companies `old` and `new` (element ids) are one: `new` takes `old`'s declared interests
    (Graph._absorb_node). An official already linked to `new` keeps that edge."""
    g.run("""MATCH (o) WHERE elementId(o) = $old MATCH (n) WHERE elementId(n) = $new
             MATCH (x:Official)-[d:DECLARED_INTEREST]->(o)
             OPTIONAL MATCH (x)-[k:DECLARED_INTEREST]->(n)
             WITH n, x, d, k
             FOREACH (_ IN CASE WHEN k IS NULL THEN [1] ELSE [] END |
                 CREATE (x)-[e:DECLARED_INTEREST]->(n) SET e = properties(d))
             DELETE d""", old=old, new=new)


# ---- reading -----------------------------------------------------------------------------

_INTERESTS = """
MATCH (o:Official)-[d:DECLARED_INTEREST]->(c:Company)
WHERE ($key IS NULL OR c.key = $key) AND ($all OR d.current)
RETURN o.key AS key, o.name AS name, o.position AS position, coalesce(o.iaac, false) AS iaac,
       o.match_key AS match_key, c.key AS company_key, c.ticker AS ticker, c.name AS company,
       coalesce(c.listed, true) AS listed, d.years AS years, d.reports AS reports,
       d.first_year AS first_year, d.last_year AS last_year, d.source AS source,
       d.current AS current, d.first_seen AS first_seen, d.until AS until
ORDER BY d.last_year DESC, o.name
"""


def company_interests(g, key: str) -> dict:
    """A company's declared interests: {"current": [...], "ended": [...]} (Graph.entity)."""
    rows = g.run(_INTERESTS, key=key, all=True)
    for r in rows:
        r.pop("match_key", None)
    return {"current": [r for r in rows if r["current"]], "ended": [r for r in rows if not r["current"]]}


def report(g, min_companies: int = 1, listed_only: bool = False, limit: int | None = None) -> dict:
    """Officials with their current declared interests, most companies first:
    {"items": [{key, name, position, iaac, companies: [...], listed_companies, last_year}], "summary"}."""
    by: dict[str, dict] = {}
    for r in g.run(_INTERESTS, key=None, all=False):
        o = by.setdefault(r["key"], {"key": r["key"], "name": r["name"], "position": r["position"],
                                     "iaac": r["iaac"], "companies": []})
        o["companies"].append({k: r[k] for k in ("company_key", "ticker", "company", "listed", "years",
                                                 "reports", "first_year", "last_year", "source")})
    items = []
    for o in by.values():
        o["listed_companies"] = sum(1 for c in o["companies"] if c["listed"])
        o["last_year"] = max((c["last_year"] or "" for c in o["companies"]), default="") or None
        if len(o["companies"]) >= min_companies and (o["listed_companies"] or not listed_only):
            items.append(o)
    items.sort(key=lambda o: (-o["listed_companies"], -len(o["companies"]), not o["iaac"],
                              -(int(o["last_year"]) if o["last_year"] else 0), o["name"]))
    every = list(by.values())
    companies = {c["company_key"] for o in every for c in o["companies"]}
    summary = {
        "officials": len(every),
        "iaac_officials": sum(1 for o in every if o["iaac"]),
        "interests": sum(len(o["companies"]) for o in every),
        "companies": len(companies),
        "listed_companies": len({c["company_key"] for o in every for c in o["companies"] if c["listed"]}),
        "in_several_companies": sum(1 for o in every if len(o["companies"]) > 1),
        "pages_read": g.run("MATCH (c:Company) WHERE c.registry_lists_at IS NOT NULL RETURN count(c) AS n")[0]["n"],
        "note": "A declaration that mentions a company is a disclosed interest, not a finding. "
                "Source: Open Data Lab (opendatalab.mn).",
    }
    return {"items": items[:limit] if limit else items, "summary": summary}


def possible_people(g, limit: int = 500) -> list[dict]:
    """Officials and the people at the same company whose name fits theirs: the same initial
    and given name (normalize.PersonName.match_key), linked to that company now (owner, officer
    or beneficial owner). Hypotheses for a reviewer only: never shown without the review password."""
    from .normalize import match_key
    rows = g.run(
        """
        MATCH (o:Official)-[d:DECLARED_INTEREST {current: true}]->(c:Company)
        MATCH (p:Person)-[r:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER]->(c) WHERE r.current
        WITH o, c, p, collect(DISTINCT type(r)) AS links
        RETURN o.key AS official, o.name AS official_name, o.position AS position, o.match_key AS mk,
               c.key AS company_key, c.ticker AS ticker, c.name AS company,
               p.key AS person, p.name AS person_name, p.full_name AS full_name, links
        """)
    out: dict[tuple, dict] = {}
    for r in rows:
        if not r["mk"] or match_key(r["full_name"] or r["person_name"]) != r["mk"]:
            continue
        x = out.setdefault((r["official"], r["person"]), {
            "official": r["official"], "official_name": r["official_name"], "position": r["position"],
            "person": r["person"], "person_name": r["person_name"], "companies": []})
        x["companies"].append({"key": r["company_key"], "ticker": r["ticker"], "name": r["company"],
                               "links": sorted(r["links"])})
    items = sorted(out.values(), key=lambda x: (-len(x["companies"]), x["official_name"]))
    return items[:limit]


def counts(g) -> dict:
    """Counts for the daily job and the pipeline log."""
    return g.run(
        """
        CALL () { MATCH (o:Official) RETURN count(o) AS officials }
        CALL () { MATCH (:Official)-[d:DECLARED_INTEREST {current: true}]->() RETURN count(d) AS interests }
        CALL () { MATCH (:Official)-[:DECLARED_INTEREST {current: true}]->(c:Company)
                  RETURN count(DISTINCT c) AS companies }
        CALL () { MATCH (c:Company) WHERE c.registry_lists_at IS NOT NULL
                  RETURN count(c) AS pages_read, sum(coalesce(c.license_count, 0)) AS licenses,
                         sum(coalesce(c.tender_count, 0)) AS tenders, sum(coalesce(c.tenders_won, 0)) AS tenders_won }
        RETURN officials, interests, companies, pages_read, licenses, tenders, tenders_won
        """)[0]
