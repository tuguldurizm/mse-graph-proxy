"""Related parties that listed companies disclose for their shareholders (issue #10).

open.mse.mn's shareholders tab gives, in each holder's row, a table "Түүнтэй нэгдмэл сонирхолтой
этгээд" (persons acting in concert / related parties): a name, the relation in words ("эхнэр",
"охин компани", "толгой компани", "Хувьцаа эзэмшигч", "Захирал"), and the party's own shares in
the company. The company itself publishes this, so unlike the patronymic hypotheses of
relations.py (#32) these are sourced facts, shown to everyone.

In the graph
------------
    (holder:Entity)-[:AFFILIATED_WITH {company, relation, relation_std, family, party_shares,
                                      party_percent, source, sources, first_seen, last_seen,
                                      current, until, opened_run, closed_run}]->(party:Entity)

`company` is the ticker of the company whose page discloses it; one pair can be disclosed by
several companies (one edge each). The relation reads holder → party: "эхнэр" says the party is
the holder's wife, "охин компани" its subsidiary. `relation_std` (relation_std below) is one of
RELATION_STD; `family` is true for spouse / parent_child / sibling / relative.

The party is resolved like a shareholder (people.resolve, company aliases), so a related party
who is also a holder ("ДУГАРЖАВ ОЮУНСҮРЭН", HRM) is the same node. Names in initial form only
("Б.Болд") are not loaded: they can't tell people apart (the owner's rule, as for FRC's data).

History: a company's shareholders tab is a complete list (snapshot kind "affiliations"), so a
disclosed link missing from the next read of that tab is closed (`current` false, `until`).
Old archives have no such snapshot, so replaying them closes nothing.

Use: the details panel and the dossier list them; neighbourhood views and the shortest path
follow them; a current family link counts as a known relative in the related-party check
(analysis.family_related_parties), next to the confirmed RELATED_TO links.
"""
from __future__ import annotations

import logging
import re

from .normalize import clean, entity_key, is_organization

log = logging.getLogger(__name__)

RELATION_STD = ("spouse", "parent_child", "sibling", "relative", "parent_company", "subsidiary",
                "beneficial_owner", "officer", "shareholder", "affiliate", "other")
FAMILY = {"spouse", "parent_child", "sibling", "relative"}
LABEL_MN = {"spouse": "эхнэр / нөхөр", "parent_child": "эцэг эх / хүүхэд", "sibling": "ах эгч дүү",
            "relative": "төрөл садан", "parent_company": "толгой компани", "subsidiary": "охин компани",
            "beneficial_owner": "эцсийн өмчлөгч", "officer": "удирдлага", "shareholder": "хувьцаа эзэмшигч",
            "affiliate": "хамаарал бүхий", "other": "бусад"}

# (relation_std, words). A word matches a token of the relation text that equals it or, for words of
# four letters or more and inside phrases, starts with it (case endings: "эхнэрийн", "гэр бүлийн"). Earlier rules win, so a
# text naming a family tie and a post ("хүү, захирал") is family.
_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("subsidiary", ("охин компани", "охин байгууллага", "салбар компани", "хараат компани")),
    ("parent_company", ("толгой компани", "толгой байгууллага", "хяналтын хувьцаа эзэмшигч")),
    ("spouse", ("эхнэр", "нөхөр", "гэргий")),
    ("parent_child", ("аав", "ээж", "эцэг", "эх", "эхийн", "хүү", "хүүгийн", "охин", "охины", "хүүхэд", "төрүүлсэн")),
    ("sibling", ("ах", "ахын", "эгч", "дүү", "дүүгийн")),
    ("relative", ("гэр бүл", "хадам", "бэр", "хүргэн", "ач", "зээ", "авга", "нагац", "өвөө", "эмээ", "ураг", "төрөл",
                  "садан", "хамаатан")),
    ("beneficial_owner", ("эцсийн өмчлөгч", "эцсийн эзэмшигч", "бенефициар")),
    ("officer", ("захирал", "дарга", "гишүүн", "удирдлага", "менежер", "нягтлан", "туз")),
    ("shareholder", ("хувьцаа эзэмшигч", "хувьцаа эзэмшдэг", "хувь нийлүүлэгч", "үүсгэн байгуулагч",
                     "эзэмшигч", "өмчлөгч")),
    ("affiliate", ("хамаарал", "нэгдмэл", "холбоотой", "хамтрагч", "харьяа")),
]


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", (text or "").casefold())


def _has(tokens: list[str], phrase: str) -> bool:
    words = phrase.split()
    for i in range(len(tokens) - len(words) + 1):
        if all(t == w or ((len(w) >= 4 or len(words) > 1) and t.startswith(w)) for t, w in zip(tokens[i:], words)):
            return True
    return False


def relation_std(text: str) -> str:
    """The relation's kind (RELATION_STD) from the page's words; "other" when nothing matches."""
    tokens = _tokens(text)
    if not tokens:
        return "other"
    for std, words in _RULES:
        if any(_has(tokens, w) for w in words):
            return std
    return "other"


# ---- loading ------------------------------------------------------------------------------

def _resolve(name: str, kind: str, ticker: str, source: str, people, alias_map: dict) -> tuple[str, str]:
    """(key, label) for a holder or party, the way the shareholder loader resolves holders."""
    key = entity_key(name, kind)
    ov = people.match_override(name, ticker, source, "shareholder", kind)
    if ov is not None:
        return ov.target, ov.label
    if key in alias_map or entity_key(name) in alias_map:
        return alias_map.get(key) or alias_map[entity_key(name)], "Company"
    is_org = kind.startswith("org") if kind else is_organization(name)
    if is_org:
        return key, "Organization"
    return people.resolve(name, ticker, source, "shareholder")[0], "Person"


def load(g, ds, when: str, run: str, people) -> dict:
    """Write the dataset's affiliations and close those its snapshots no longer list."""
    snaps = {t.upper(): p for t, p in ds.snapshots.get("affiliations", {}).items()}
    if not ds.affiliations and not snaps:
        return {}
    alias_map = {r["a"]: r["key"] for r in g.run("MATCH (c:Company) UNWIND c.aliases AS a RETURN a, c.key AS key")}
    rows: dict[tuple, dict] = {}
    entities: dict[str, dict] = {}
    for a in ds.affiliations:
        holder, party = clean(a.holder_name), clean(a.party_name)
        if not holder or not party:
            continue
        t = a.company_ticker.upper()
        hk, hl = _resolve(holder, a.holder_type, t, a.source, people, alias_map)
        pk, pl = _resolve(party, a.party_type, t, a.source, people, alias_map)
        if hk == pk:
            continue
        for k, label, name in ((hk, hl, holder), (pk, pl, party)):
            if label != "Company":
                entities.setdefault(k, {"key": k, "name": name, "label": label})
        std = relation_std(a.relation)
        rows[(t, hk, pk)] = {"t": t, "h": hk, "p": pk, "relation": clean(a.relation) or None, "std": std,
                             "family": std in FAMILY, "shares": a.party_shares, "percent": a.party_percent,
                             "source": a.source or None}
    for label in ("Person", "Organization"):
        g.run(f"""UNWIND $rows AS r MERGE (e:Entity {{key: r.key}}) ON CREATE SET e.name = r.name SET e:{label}""",
              rows=[e for e in entities.values() if e["label"] == label])
    res = g.run(
        """
        UNWIND $rows AS r
        MATCH (c:Company {ticker: r.t}) MATCH (h:Entity {key: r.h}) MATCH (p:Entity {key: r.p})
        OPTIONAL MATCH (h)-[o:AFFILIATED_WITH {company: r.t, current: true}]->(p)
        CALL (r, h, p, o) {
            WITH r, h, p, o WHERE o IS NULL
            CREATE (h)-[x:AFFILIATED_WITH {company: r.t, current: true}]->(p)
            SET x.first_seen = $when, x.opened_run = $run, x.sources = [r.source]
        }
        WITH r, h, p
        MATCH (h)-[x:AFFILIATED_WITH {company: r.t, current: true}]->(p)
        SET x.relation = r.relation, x.relation_std = r.std, x.family = r.family, x.party_shares = r.shares,
            x.party_percent = r.percent, x.source = r.source, x.last_seen = $when,
            x.sources = CASE WHEN r.source IN coalesce(x.sources, []) THEN x.sources
                             ELSE coalesce(x.sources, []) + r.source END
        RETURN count(CASE WHEN x.opened_run = $run THEN 1 END) AS opened, count(x) AS seen
        """, rows=list(rows.values()), when=when, run=run)
    seen: dict[str, list[str]] = {t: [] for t in snaps}
    for (t, hk, pk) in rows:
        if t in seen:
            seen[t].append(f"{hk}|{pk}")
    scopes = [{"t": t, "prefix": p, "under": p.rstrip("/") + "/", "seen": seen.get(t, [])} for t, p in snaps.items()]
    closed = g.run(
        """
        UNWIND $scopes AS s
        MATCH (h:Entity)-[x:AFFILIATED_WITH {company: s.t, current: true}]->(p:Entity)
        WHERE (x.source = s.prefix OR x.source STARTS WITH s.under) AND NOT (h.key + '|' + p.key) IN s.seen
        SET x.current = false, x.until = $when, x.closed_run = $run
        RETURN count(x) AS n
        """, scopes=scopes, when=when, run=run)[0]["n"] if scopes else 0
    out = {"affiliations_opened": res[0]["opened"] if res else 0, "affiliations_closed": closed}
    return {k: v for k, v in out.items() if v}


# ---- reading ------------------------------------------------------------------------------

_ROW = """{key: q.key, name: q.name, name_en: q.name_en, type: CASE WHEN q:Company THEN 'Company'
           WHEN q:Person THEN 'Person' ELSE 'Organization' END, ticker: q.ticker,
           company: x.company, company_name: c.name, relation: x.relation, relation_std: x.relation_std,
           family: x.family, party_shares: x.party_shares, current: x.current, first_seen: x.first_seen,
           last_seen: x.last_seen, until: x.until, source: x.source, sources: x.sources, direction: DIR}"""


def for_entity(g, key: str) -> list[dict]:
    """An entity's disclosed related parties, both ways: `direction` "out" when the entity is the
    holder whose row names the other, "in" when another holder's row names it."""
    rows = g.run(
        f"""
        MATCH (e:Entity {{key: $k}})
        CALL (e) {{
            MATCH (e)-[x:AFFILIATED_WITH]->(q:Entity) OPTIONAL MATCH (c:Company {{ticker: x.company}})
            RETURN {_ROW.replace("DIR", "'out'")} AS row
            UNION ALL
            MATCH (q:Entity)-[x:AFFILIATED_WITH]->(e) OPTIONAL MATCH (c:Company {{ticker: x.company}})
            RETURN {_ROW.replace("DIR", "'in'")} AS row
        }}
        RETURN row
        """, k=key)
    return sorted((r["row"] for r in rows),
                  key=lambda r: (not r["current"], not r["family"], r["name"] or "", r["company"] or ""))


def company_affiliations(g, ticker: str, current: bool = True) -> list[dict]:
    """The related parties a company's page discloses, per shareholder (the dossier's table)."""
    return g.run(
        """
        MATCH (h:Entity)-[x:AFFILIATED_WITH {company: $t}]->(q:Entity)
        WHERE NOT $current OR x.current
        RETURN h.key AS holder_key, h.name AS holder, q.key AS key, q.name AS name, x.relation AS relation,
               x.relation_std AS relation_std, x.family AS family, x.party_shares AS party_shares,
               x.current AS current, x.first_seen AS first_seen, x.until AS until, x.source AS source
        ORDER BY holder, name
        """, t=ticker, current=current)


def summary(g) -> dict:
    """Counts of the current links, by relation kind."""
    rows = g.run("""MATCH ()-[x:AFFILIATED_WITH {current: true}]->()
                    RETURN x.relation_std AS std, count(*) AS n, collect(DISTINCT x.company) AS cos""")
    by = {r["std"]: r["n"] for r in rows}
    return {"links": sum(by.values()), "family": sum(n for k, n in by.items() if k in FAMILY),
            "companies": len({c for r in rows for c in r["cos"]}),
            "by_relation": dict(sorted(by.items(), key=lambda kv: -kv[1]))}
