"""Governance checks: ownership loops, cross-holdings, conflicts of interest, state ownership.

Each function takes a `Graph` and returns plain dicts. Only current edges
count: past holdings and posts (graph history) never raise a flag. These checks flag
structures worth a closer look. They are not findings of wrongdoing: a
director nominated by the parent company is normal, but transactions between
the two are where conflicts arise. The UI and the flag texts word them
accordingly.

    cycles            companies that own themselves through a chain of holdings
    cross_holdings    listed companies holding shares in other listed companies
    conflicts         insider_owner   officer who is also a shareholder of the company
                      competitor      person/entity with influence in two companies of one sector
                      related_party   officer of a company who also has influence in its
                                      shareholder or subsidiary
    state_ownership   direct and look-through stakes held by state / municipal bodies
    company_flags     all of the above for one company, plus a look-through controller
    position_roles    current positions per standard role, and the titles no rule recognises
"""
from __future__ import annotations

import re

from .graph import Graph
from .normalize import clean

# Name markers of state or municipal owners (Mongolian / English).
STATE_MARKERS = [
    "төрийн", "засгийн газар", "яам", "аймгийн", "нийслэлийн", "дүүргийн", "төк", "төхк",
    "эрдэнэс монгол", "монгол банк", "нийгмийн даатгалын сан",
    "government", "ministry", "state property", "state-owned", "state owned", "municipal",
    "erdenes mongol", "bank of mongolia",
]
_STATE_RE = re.compile(r"(?<![\w])(" + "|".join(re.escape(m) for m in STATE_MARKERS) + r")(?![\w])",
                       re.IGNORECASE)

# Holdings at or above this are treated as influence (besides board/executive posts).
DEFAULT_MIN_STAKE = 5.0

_NODE = "{key: n.key, name: n.name, ticker: n.ticker, type: [l IN labels(n) WHERE l <> 'Entity'][0]}"
_SIMPLE = "all(i IN range(0, size(nodes(p)) - 2) WHERE NOT nodes(p)[i] IN nodes(p)[i + 1..])"


def is_state(name: str) -> bool:
    return bool(_STATE_RE.search(clean(name).lower()))


def _co(n) -> str:
    """Display label for a node dict: ticker for listed companies, else name."""
    return n.get("ticker") or n.get("name") or n.get("key", "")


def _pct(v) -> str:
    return f"{v:g}%" if v is not None else "?%"


def _roles(items: list[dict]) -> list[str]:
    """[{rel, title, percent}] -> ["ТУЗ-ийн гишүүн", "51%"], posts first."""
    posts = sorted({i["title"] for i in items if i["rel"] == "HOLDS_POSITION" and i["title"]})
    stakes = [_pct(i["percent"]) for i in items if i["rel"] == "OWNS"]
    return posts + stakes


# ---- ownership structure -----------------------------------------------------

def cycles(g: Graph, max_len: int = 6) -> list[dict]:
    """Ownership loops: A owns B owns ... owns A.

    `loop_percent` is the product of the stakes around the loop, i.e. how much of
    each company in it ends up owning itself. Each loop is listed once.
    """
    max_len = max(2, min(int(max_len), 8))
    rows = g.run(
        f"""
        MATCH p = (c:Company)-[:OWNS*2..{max_len}]->(c)
        WITH c, p, nodes(p)[..-1] AS ns
        WHERE all(r IN relationships(p) WHERE r.current)
          AND all(n IN ns WHERE n.key >= c.key)                       // one rotation per loop
          AND all(i IN range(0, size(ns) - 2) WHERE NOT ns[i] IN ns[i + 1..])   // simple loops only
        RETURN [n IN ns | {_NODE}] AS companies,
               [r IN relationships(p) | r.percent] AS percents,
               CASE WHEN all(r IN relationships(p) WHERE r.percent IS NOT NULL)
                    THEN reduce(x = 100.0, r IN relationships(p) | x * r.percent / 100.0) END AS loop_percent
        ORDER BY size(ns), loop_percent DESC
        """
    )
    for r in rows:
        if r["loop_percent"] is not None:
            r["loop_percent"] = round(r["loop_percent"], 4)
        r["mutual"] = len(r["companies"]) == 2
    return rows


def cross_holdings(g: Graph) -> list[dict]:
    """Listed companies holding shares in other listed companies, with the reverse stake if any."""
    return g.run(
        """
        MATCH (a:Company)-[o:OWNS {current: true}]->(b:Company)
        WHERE coalesce(a.listed, true) AND coalesce(b.listed, true)
        OPTIONAL MATCH (b)-[back:OWNS {current: true}]->(a)
        RETURN a.key AS holder_key, a.ticker AS holder, a.name AS holder_name,
               b.key AS company_key, b.ticker AS company, b.name AS company_name,
               o.percent AS percent, back.percent AS reverse_percent
        ORDER BY coalesce(o.percent, 0) DESC, holder, company
        """
    )


# ---- conflicts of interest -------------------------------------------------------

def insider_owners(g: Graph) -> list[dict]:
    """Board members / executives who also hold shares in the same company."""
    return g.run(
        """
        MATCH (p:Person)-[h:HOLDS_POSITION {current: true}]->(c:Company)<-[o:OWNS {current: true}]-(p)
        WITH p, c, o, collect(h.title) AS titles
        RETURN 'insider_owner' AS kind, p.key AS key, p.name AS name,
               [{key: c.key, ticker: c.ticker, name: c.name, titles: titles, percent: o.percent}] AS companies
        ORDER BY coalesce(o.percent, 0) DESC, name
        """
    )


def competitor_links(g: Graph, min_stake: float = DEFAULT_MIN_STAKE) -> list[dict]:
    """People or entities with a post, or a stake >= min_stake, in two+ companies of the same sector."""
    rows = g.run(
        """
        MATCH (x:Entity)-[r:OWNS|HOLDS_POSITION {current: true}]->(c:Company)
        WHERE coalesce(c.sector, '') <> '' AND x <> c
          AND (type(r) = 'HOLDS_POSITION' OR coalesce(r.percent, 0) >= $min)
        WITH x, toLower(c.sector) AS sector_key, c, collect({rel: type(r), title: r.title, percent: r.percent}) AS roles
        WITH x, sector_key, collect({key: c.key, ticker: c.ticker, name: c.name, sector: c.sector,
                                     roles: roles}) AS companies
        WHERE size(companies) >= 2
        RETURN 'competitor' AS kind, x.key AS key, x.name AS name,
               [l IN labels(x) WHERE l <> 'Entity'][0] AS type,
               companies[0].sector AS sector, companies
        ORDER BY size(companies) DESC, name
        """,
        min=min_stake,
    )
    for row in rows:
        for co in row["companies"]:
            co["roles"] = _roles(co["roles"])
    return rows


def related_parties(g: Graph, min_stake: float = DEFAULT_MIN_STAKE) -> list[dict]:
    """Officers of a company who also have a post or a stake >= min_stake in a company
    that owns it or that it owns: they sit on both sides of intra-group dealings."""
    rows = g.run(
        """
        MATCH (p:Person)-[h:HOLDS_POSITION {current: true}]->(c:Company)
        MATCH (c)-[link:OWNS {current: true}]-(s:Company)
        MATCH (p)-[r:OWNS|HOLDS_POSITION {current: true}]->(s)
        WHERE s <> c AND (type(r) = 'HOLDS_POSITION' OR coalesce(r.percent, 0) >= $min)
        WITH p, c, s, link, collect(DISTINCT h.title) AS titles_c,
             collect(DISTINCT {rel: type(r), title: r.title, percent: r.percent}) AS roles_s
        RETURN 'related_party' AS kind, p.key AS key, p.name AS name,
               {key: c.key, ticker: c.ticker, name: c.name, roles: titles_c} AS company,
               {key: s.key, ticker: s.ticker, name: s.name, roles: roles_s} AS counterparty,
               CASE WHEN startNode(link) = s THEN 'shareholder' ELSE 'subsidiary' END AS relation,
               link.percent AS percent
        ORDER BY name, company.ticker, counterparty.ticker
        """,
        min=min_stake,
    )
    # A person on both boards appears twice (once from each side): keep the side
    # where `company` is the one being owned, which reads naturally ("officer of
    # XNB, whose shareholder is XAG").
    for r in rows:
        r["company"]["roles"] = sorted(r["company"]["roles"])
        r["counterparty"]["roles"] = _roles(r["counterparty"]["roles"])
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: r["relation"] != "shareholder"):
        pair = (r["key"], frozenset((r["company"]["key"], r["counterparty"]["key"])))
        if pair not in seen:
            seen.add(pair)
            out.append(r)
    return sorted(out, key=lambda r: (r["name"], _co(r["company"]), _co(r["counterparty"])))


def conflicts(g: Graph, kind: str | None = None, min_stake: float = DEFAULT_MIN_STAKE) -> list[dict]:
    out = []
    if kind in (None, "insider_owner"):
        out += insider_owners(g)
    if kind in (None, "competitor"):
        out += competitor_links(g, min_stake)
    if kind in (None, "related_party"):
        out += related_parties(g, min_stake)
    return out


# ---- state ownership -------------------------------------------------------------

def state_entities(g: Graph) -> list[dict]:
    rows = g.run("MATCH (e:Entity) WHERE e:Organization OR e:Company RETURN e.key AS key, e.name AS name")
    return [r for r in rows if is_state(r["name"] or "")]


def state_ownership(g: Graph, max_depth: int = 5) -> list[dict]:
    """Companies with state or municipal owners.

    `direct_percent` sums stakes held directly by state bodies. `lookthrough_percent`
    follows chains from the top state owners (those not themselves owned by another
    state body), so Government -> State holding -> X is counted once.
    """
    keys = [r["key"] for r in state_entities(g)]
    if not keys:
        return []
    max_depth = max(1, min(int(max_depth), 8))
    rows = g.run(
        f"""
        MATCH (s:Entity) WHERE s.key IN $keys
          AND NOT EXISTS {{ MATCH (t:Entity)-[:OWNS {{current: true}}]->(s) WHERE t.key IN $keys }}
        MATCH p = (s)-[:OWNS*1..{max_depth}]->(c:Company)
        WHERE all(r IN relationships(p) WHERE r.current AND r.percent IS NOT NULL) AND NOT c.key IN $keys
          AND {_SIMPLE}
        WITH c, s, sum(reduce(x = 100.0, r IN relationships(p) | x * r.percent / 100.0)) AS eff
        WITH c, sum(eff) AS lookthrough, collect({{key: s.key, name: s.name, percent: round(eff, 3)}}) AS via
        OPTIONAL MATCH (d:Entity)-[o:OWNS {{current: true}}]->(c) WHERE d.key IN $keys
        WITH c, lookthrough, via, sum(coalesce(o.percent, 0)) AS direct,
             collect(CASE WHEN d IS NULL THEN NULL ELSE {{key: d.key, name: d.name, percent: o.percent}} END) AS holders
        RETURN c.key AS key, c.ticker AS ticker, c.name AS name, coalesce(c.listed, true) AS listed,
               round(direct, 3) AS direct_percent, round(lookthrough, 3) AS lookthrough_percent,
               holders AS direct_holders, via AS top_owners
        ORDER BY lookthrough DESC, ticker
        """,
        keys=keys,
    )
    return [r for r in rows if r["lookthrough_percent"] > 0 or r["direct_percent"] > 0]


# ---- positions by standard role (issue #34) --------------------------------------

def position_roles(g: Graph, limit: int = 200) -> dict:
    """Current positions counted per `role_std`, plus the titles mapped to "unknown".

    The unknown titles are the ones normalize.role_std needs a pattern for; the
    data-quality report (#8) lists them.
    """
    counts = {r["role_std"]: r["n"] for r in g.run(
        """MATCH ()-[h:HOLDS_POSITION {current: true}]->()
           RETURN coalesce(h.role_std, 'unknown') AS role_std, count(h) AS n""")}
    unknown = g.run(
        """
        MATCH (p:Person)-[h:HOLDS_POSITION {current: true}]->(c:Company)
        WHERE coalesce(h.role_std, 'unknown') = 'unknown'
        WITH coalesce(h.title, '') AS title, count(h) AS n, collect(DISTINCT c.ticker)[..10] AS companies
        RETURN title, n, companies ORDER BY n DESC, title LIMIT $limit
        """, limit=limit)
    return {"counts": counts, "unknown_titles": unknown}


# ---- per-company summary ----------------------------------------------------------

def company_flags(g: Graph, ticker: str, min_stake: float = DEFAULT_MIN_STAKE) -> list[dict]:
    """Everything above that involves one company, as {kind, level, text, keys}.

    level is "warn" for structures that usually need a closer look (loops, mutual
    holdings, competitor ties) and "info" for common, disclosure-worthy ones.
    """
    ticker = ticker.upper()
    me = "company:" + ticker
    flags: list[dict] = []

    def add(kind, level, text, keys=()):
        flags.append({"kind": kind, "level": level, "text": text, "keys": list(keys)})

    for cyc in cycles(g):
        tickers = [_co(n) for n in cyc["companies"]]
        if ticker in tickers:
            i = tickers.index(ticker)
            ring = tickers[i:] + tickers[:i] + [ticker]
            pct = f" (давталтаар өөрийгөө {cyc['loop_percent']:g}% эзэмшинэ)" if cyc["loop_percent"] else ""
            kind = "mutual_holding" if cyc["mutual"] else "ownership_cycle"
            label = "Харилцан эзэмшил" if cyc["mutual"] else "Тойрог эзэмшил"
            add(kind, "warn", f"{label}: {' → '.join(ring)}{pct}", [n["key"] for n in cyc["companies"]])

    for x in cross_holdings(g):
        if x["company"] == ticker and x["reverse_percent"] is None:
            add("listed_holder", "info", f"Бүртгэлтэй компани {x['holder']} {x['percent']:g}% эзэмшдэг"
                if x["percent"] is not None else f"Бүртгэлтэй компани {x['holder']} эзэмшдэг", [x["holder_key"]])

    uo = g.ultimate_owners(ticker)
    if uo and uo[0]["effective_percent"] >= 50:
        add("controller", "info", f"Хяналтын эзэмшигч: {uo[0]['name']} ({uo[0]['effective_percent']:g}% look-through)",
            [uo[0]["key"]])

    for s in state_ownership(g):
        if s["key"] == me:
            direct = f"шууд {s['direct_percent']:g}%, " if s["direct_percent"] else ""
            add("state", "info", f"Төрийн оролцоо: {direct}look-through {s['lookthrough_percent']:g}%",
                [o["key"] for o in s["top_owners"]])

    for c in insider_owners(g):
        co = c["companies"][0]
        if co["ticker"] == ticker:
            pct = f" {co['percent']:g}%-ийг" if co["percent"] is not None else ""
            add("insider_owner", "info", f"{c['name']} ({', '.join(co['titles'])}) компанийн хувьцааны{pct} эзэмшдэг",
                [c["key"]])

    for c in competitor_links(g, min_stake):
        tickers = [co["ticker"] for co in c["companies"]]
        if ticker in tickers:
            mine = next(co for co in c["companies"] if co["ticker"] == ticker)
            others = "; ".join(f"{_co(co)}: {', '.join(co['roles'])}"
                               for co in c["companies"] if co["ticker"] != ticker)
            add("competitor", "warn",
                f"{c['name']} ({', '.join(mine['roles'])}) ижил салбарын ({c['sector']}) өөр компанид бас оролцоотой — {others}",
                [c["key"]] + [co["key"] for co in c["companies"]])

    for r in related_parties(g, min_stake):
        sides = {r["company"]["key"], r["counterparty"]["key"]}
        if me in sides:
            rel = "хувьцаа эзэмшигч" if r["relation"] == "shareholder" else "охин компани"
            add("related_party", "info",
                f"{r['name']}: {_co(r['company'])} ({', '.join(r['company']['roles'])}) болон түүний {rel} "
                f"{_co(r['counterparty'])} ({', '.join(r['counterparty']['roles'])})",
                [r["key"], r["counterparty"]["key"], r["company"]["key"]])

    order = {"warn": 0, "info": 1}
    flags.sort(key=lambda f: order[f["level"]])
    return flags
