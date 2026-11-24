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
                                      shareholder or subsidiary, or whose confirmed relative
                                      (RELATED_TO, issue #32) has influence in the company itself
                                      or in its shareholder or subsidiary
    state_ownership   direct and look-through stakes held by state / municipal bodies
    company_flags     all of the above for one company, plus a look-through controller
    position_roles    current positions per standard role, and the titles no rule recognises
    nominees          holders that keep shares for others, stakes by holding type, and
                      holders that may be nominees but aren't marked (issue #39)

Family links count when a reviewer has confirmed them (RELATED_TO edges, relations.py) or a company
disclosed them for a shareholder on mse.mn (AFFILIATED_WITH with `family`, affiliations.py): the
possible links from shared patronymics are hypotheses and never reach these checks.

Only stakes that are ownership count (holdings.REAL: `direct` or `unknown`): a custodian
holding shares for its clients is not an owner, a controller or a related party, and a
company's treasury shares are not a holding (issue #39).
"""
from __future__ import annotations

from .graph import Graph
from .holdings import real
from .normalize import is_state

# a stake that is ownership (not nominee or treasury), for the relationship variable r
_REAL_R = real("r")

# Holdings at or above this are treated as influence (besides board/executive posts).
DEFAULT_MIN_STAKE = 5.0

_NODE = "{key: n.key, name: n.name, ticker: n.ticker, type: [l IN labels(n) WHERE l IN ['Company', 'Person', 'Organization']][0]}"
_SIMPLE = "all(i IN range(0, size(nodes(p)) - 2) WHERE NOT nodes(p)[i] IN nodes(p)[i + 1..])"


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
        WHERE all(r IN relationships(p) WHERE r.current AND {_REAL_R})
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
        f"""
        MATCH (a:Company)-[o:OWNS {{current: true}}]->(b:Company)
        WHERE coalesce(a.listed, true) AND coalesce(b.listed, true) AND a <> b AND {real("o")}
        OPTIONAL MATCH (b)-[back:OWNS {{current: true}}]->(a) WHERE {real("back")}
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
        f"""
        MATCH (p:Person)-[h:HOLDS_POSITION {{current: true}}]->(c:Company)<-[o:OWNS {{current: true}}]-(p)
        WHERE {real("o")}
        WITH p, c, o, collect(h.title) AS titles
        RETURN 'insider_owner' AS kind, p.key AS key, p.name AS name,
               [{{key: c.key, ticker: c.ticker, name: c.name, titles: titles, percent: o.percent}}] AS companies
        ORDER BY coalesce(o.percent, 0) DESC, name
        """
    )


def competitor_links(g: Graph, min_stake: float = DEFAULT_MIN_STAKE) -> list[dict]:
    """People or entities with a post, or a stake >= min_stake, in two+ companies of the same sector.

    The sector is the standard one (`sector_std`, the ISIC section of the registry's main
    activity code, issue #38). A company without registry activity codes falls back to the
    company list's sector text (`sector_mse`), so it is compared only with companies that
    have the same text and no standard sector. `sector_std` says which kind a row is."""
    rows = g.run(
        """
        MATCH (x:Entity)-[r:OWNS|HOLDS_POSITION {current: true}]->(c:Company)
        WHERE x <> c AND (type(r) = 'HOLDS_POSITION' OR (coalesce(r.percent, 0) >= $min AND REAL_R))
        WITH x, r, c, CASE WHEN c.sector_std IS NOT NULL THEN 'std:' + c.sector_std
                           WHEN coalesce(c.sector_mse, c.sector, '') <> ''
                           THEN 'text:' + toLower(coalesce(c.sector_mse, c.sector)) END AS sector_key
        WHERE sector_key IS NOT NULL
        WITH x, sector_key, c, collect({rel: type(r), title: r.title, percent: r.percent}) AS roles
        WITH x, sector_key, collect({key: c.key, ticker: c.ticker, name: c.name, sector: c.sector,
                                     sector_std: c.sector_std, roles: roles}) AS companies,
             head(collect(coalesce(c.sector_std_name, c.sector_mse, c.sector))) AS sector
        WHERE size(companies) >= 2
        RETURN 'competitor' AS kind, x.key AS key, x.name AS name,
               [l IN labels(x) WHERE l IN ['Company', 'Person', 'Organization']][0] AS type,
               sector, CASE WHEN sector_key STARTS WITH 'std:' THEN substring(sector_key, 4) END AS sector_std,
               companies
        ORDER BY size(companies) DESC, name
        """.replace("REAL_R", _REAL_R),
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
        MATCH (c)-[link:OWNS {current: true}]-(s:Company) WHERE REAL_LINK
        MATCH (p)-[r:OWNS|HOLDS_POSITION {current: true}]->(s)
        WHERE s <> c AND (type(r) = 'HOLDS_POSITION' OR (coalesce(r.percent, 0) >= $min AND REAL_R))
        WITH p, c, s, link, collect(DISTINCT h.title) AS titles_c,
             collect(DISTINCT {rel: type(r), title: r.title, percent: r.percent}) AS roles_s
        RETURN 'related_party' AS kind, p.key AS key, p.name AS name,
               {key: c.key, ticker: c.ticker, name: c.name, roles: titles_c} AS company,
               {key: s.key, ticker: s.ticker, name: s.name, roles: roles_s} AS counterparty,
               CASE WHEN startNode(link) = s THEN 'shareholder' ELSE 'subsidiary' END AS relation,
               link.percent AS percent
        ORDER BY name, company.ticker, counterparty.ticker
        """.replace("REAL_R", _REAL_R).replace("REAL_LINK", real("link")),
        min=min_stake,
    )
    # A person on both boards appears twice (once from each side): keep the side
    # where `company` is the one being owned, which reads naturally ("officer of
    # XNB, whose shareholder is XAG").
    for r in rows:
        r["company"]["roles"] = sorted(r["company"]["roles"])
        r["counterparty"]["roles"] = _roles(r["counterparty"]["roles"])
        r["relative"] = None
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: r["relation"] != "shareholder"):
        pair = (r["key"], frozenset((r["company"]["key"], r["counterparty"]["key"])))
        if pair not in seen:
            seen.add(pair)
            out.append(r)
    out += family_related_parties(g, min_stake)
    return sorted(out, key=lambda r: (r["name"], _co(r["company"]), _co(r["counterparty"]),
                                      (r["relative"] or {}).get("name") or ""))


def family_related_parties(g: Graph, min_stake: float = DEFAULT_MIN_STAKE) -> list[dict]:
    """Officers of a company whose relative has a post or a stake >= min_stake in the same company
    (`same_company`) or in a company that owns it or that it owns. A relative is a confirmed link
    (RELATED_TO, issue #32) or a family tie a company disclosed for a shareholder (AFFILIATED_WITH
    with `family`, affiliations.py). Hypotheses are never edges, so they never show here."""
    rows = g.run(
        """
        MATCH (p:Person)-[h:HOLDS_POSITION {current: true}]->(c:Company)
        MATCH (p)-[fam:RELATED_TO|AFFILIATED_WITH]-(q:Person)
        WHERE q <> p AND (type(fam) = 'RELATED_TO' OR (fam.current AND fam.family))
        MATCH (q)-[r:OWNS|HOLDS_POSITION {current: true}]->(s:Company)
        WHERE type(r) = 'HOLDS_POSITION' OR (coalesce(r.percent, 0) >= $min AND REAL_R)
        WITH p, c, q, fam, s, collect(DISTINCT h.title) AS titles_c,
             collect(DISTINCT {rel: type(r), title: r.title, percent: r.percent}) AS roles_s
        OPTIONAL MATCH (c)-[link:OWNS {current: true}]-(s) WHERE s <> c AND REAL_LINK
        WITH p, c, q, fam, s, titles_c, roles_s, head(collect(link)) AS link
        WHERE s = c OR link IS NOT NULL
        RETURN 'related_party' AS kind, p.key AS key, p.name AS name,
               {key: c.key, ticker: c.ticker, name: c.name, roles: titles_c} AS company,
               {key: s.key, ticker: s.ticker, name: s.name, roles: roles_s} AS counterparty,
               CASE WHEN s = c THEN 'same_company' WHEN startNode(link) = s THEN 'shareholder'
                    ELSE 'subsidiary' END AS relation,
               link.percent AS percent,
               {key: q.key, name: q.name, basis: coalesce(fam.basis, fam.relation_std), source: fam.source,
                relation: fam.relation, disclosed_by: fam.company} AS relative
        """.replace("REAL_R", _REAL_R).replace("REAL_LINK", real("link")),
        min=min_stake,
    )
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: ({"same_company": 0, "shareholder": 1}.get(r["relation"], 2), r["name"])):
        r["company"]["roles"] = sorted(r["company"]["roles"])
        r["counterparty"]["roles"] = _roles(r["counterparty"]["roles"])
        # one row per family pair and pair of companies (both relatives may be officers)
        k = (frozenset((r["key"], r["relative"]["key"])), frozenset((r["company"]["key"], r["counterparty"]["key"])))
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


BASIS_MN = {"sibling": "ах дүү", "parent_child": "эцэг эх / хүүхэд", "spouse": "эхнэр нөхөр", "relative": "төрөл"}


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
    """State and municipal bodies and state-owned companies.

    An organisation's `org_type` decides (set from its source, else from its name, see
    normalize.org_type_from_name). Companies have no `org_type`, so their name does."""
    rows = g.run("""MATCH (e:Entity) WHERE e:Organization OR e:Company
                    RETURN e.key AS key, e.name AS name, CASE WHEN e:Company THEN null ELSE e.org_type END AS t""")
    return [{"key": r["key"], "name": r["name"]} for r in rows
            if (r["t"] == "state" if r["t"] else is_state(r["name"] or ""))]


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
          AND NOT EXISTS {{ MATCH (t:Entity)-[x:OWNS {{current: true}}]->(s) WHERE t.key IN $keys AND {real("x")} }}
        MATCH p = (s)-[:OWNS*1..{max_depth}]->(c:Company)
        WHERE all(r IN relationships(p) WHERE r.current AND r.percent IS NOT NULL AND {_REAL_R}) AND NOT c.key IN $keys
          AND {_SIMPLE}
        WITH c, s, sum(reduce(x = 100.0, r IN relationships(p) | x * r.percent / 100.0)) AS eff
        WITH c, sum(eff) AS lookthrough, collect({{key: s.key, name: s.name, percent: round(eff, 3)}}) AS via
        OPTIONAL MATCH (d:Entity)-[o:OWNS {{current: true}}]->(c) WHERE d.key IN $keys AND {real("o")}
        WITH c, lookthrough, via, sum(coalesce(o.percent, 0)) AS direct,
             collect(CASE WHEN d IS NULL THEN NULL ELSE {{key: d.key, name: d.name, percent: o.percent}} END) AS holders
        RETURN c.key AS key, c.ticker AS ticker, c.name AS name, coalesce(c.listed, true) AS listed,
               round(direct, 3) AS direct_percent, round(lookthrough, 3) AS lookthrough_percent,
               holders AS direct_holders, via AS top_owners, c.market_cap AS market_cap,
               coalesce(c.price_stale, false) AS price_stale, c.price_date AS price_date
        ORDER BY lookthrough DESC, ticker
        """,
        keys=keys,
    )
    from .market import value_mnt

    out = [r for r in rows if r["lookthrough_percent"] > 0 or r["direct_percent"] > 0]
    for r in out:   # MNT (issue #31): the state's share of the company's market cap
        r["direct_value_mnt"] = value_mnt(r["direct_percent"], r["market_cap"])
        r["lookthrough_value_mnt"] = value_mnt(r["lookthrough_percent"], r["market_cap"])
    return out


def state_value_summary(rows: list[dict]) -> dict:
    """Totals over state_ownership(): companies, and the MNT value of the state's stakes in the
    ones with a market cap (issue #31)."""
    valued = [r for r in rows if r["market_cap"] is not None]
    return {"companies": len(rows), "listed": sum(1 for r in rows if r["listed"]),
            "valued": len(valued),
            "direct_value_mnt": round(sum(r["direct_value_mnt"] or 0 for r in valued), 0) if valued else None,
            "lookthrough_value_mnt": round(sum(r["lookthrough_value_mnt"] or 0 for r in valued), 0) if valued else None,
            "market_cap_of_valued": round(sum(r["market_cap"] for r in valued), 0) if valued else None}


# ---- nominee and treasury holdings (issue #39) --------------------------------------

# Names of holders that often keep shares for clients, but not always: brokerages, banks,
# investment firms. They are candidates for data/reference/nominees.csv, never marked by rule.
_CANDIDATE_RE = (r"(?i).*(ҮЦК|үнэт цаас|securities|секьюр|секюр|брокер|broker|банк|bank|капитал|capital|"
                 r"хөрөнгө оруулалт|invest|asset management).*")


def nominee_candidates(g: Graph, min_companies: int = 2, limit: int = 100) -> list[dict]:
    """Holders not marked as nominees whose name suggests a broker, bank or investment firm, or
    that FRC licenses to hold for clients (custodian, broker, nominee account, trust: `licensed`,
    frc.py), and that hold stakes in `min_companies`+ companies: the rows to check before adding
    them to data/reference/nominees.csv (a firm's own investments are direct stakes)."""
    from .frc import NOMINEE_KINDS
    return g.run(
        f"""
        MATCH (h:Entity)-[o:OWNS {{current: true}}]->(c:Company)
        WHERE NOT h:Person AND NOT coalesce(h.nominee, false) AND h <> c AND {real("o")}
          AND (h.name =~ $rx OR any(k IN coalesce(h.frc_kinds, []) WHERE k IN $kinds))
        WITH h, collect({{ticker: c.ticker, name: c.name, key: c.key, percent: o.percent}}) AS stakes
        WHERE size(stakes) >= $min
        RETURN h.key AS key, h.name AS name, h.register_no AS register_no,
               [l IN labels(h) WHERE l IN ['Company', 'Person', 'Organization']][0] AS type,
               [k IN coalesce(h.frc_kinds, []) WHERE k IN $kinds] AS licensed,
               size(stakes) AS companies, stakes
        ORDER BY size(licensed) > 0 DESC, companies DESC, name LIMIT $limit
        """, rx=_CANDIDATE_RE, kinds=list(NOMINEE_KINDS), min=min_companies, limit=limit)


def nominees(g: Graph) -> dict:
    """Holders that keep shares for others, current stakes counted by holding type, the
    companies with the largest nominee share, and candidates to check (issue #39)."""
    counts = {r["t"]: r["n"] for r in g.run(
        """MATCH ()-[o:OWNS {current: true}]->() RETURN coalesce(o.holding_type, 'direct') AS t, count(o) AS n""")}
    largest = g.run(
        """
        MATCH (h:Entity)-[o:OWNS {current: true}]->(c:Company) WHERE o.holding_type IN ['nominee', 'treasury']
        WITH c, o.holding_type AS t, sum(o.percent) AS pct, count(o) AS n
        WITH c, collect({holding_type: t, percent: round(pct, 3), holders: n}) AS parts,
             sum(CASE WHEN t = 'nominee' THEN coalesce(pct, 0) ELSE 0 END) AS nominee_pct
        RETURN c.key AS key, c.ticker AS ticker, c.name AS name, parts, round(nominee_pct, 3) AS nominee_percent
        ORDER BY nominee_pct DESC, ticker LIMIT 50
        """)
    return {"counts": {t: counts.get(t, 0) for t in ("direct", "nominee", "treasury", "unknown")},
            "nominees": g.nominee_holders(), "companies": largest, "candidates": nominee_candidates(g)}


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
    ticker = g.resolve_ticker(ticker)
    me = g.company_keys().get(ticker, "company:" + ticker)
    flags: list[dict] = []

    def add(kind, level, text, keys=()):
        flags.append({"kind": kind, "level": level, "text": text, "keys": list(keys)})

    from .memo import cached
    for cyc in cached(g, "cycles", cycles):
        tickers = [_co(n) for n in cyc["companies"]]
        if ticker in tickers:
            i = tickers.index(ticker)
            ring = tickers[i:] + tickers[:i] + [ticker]
            pct = f" (давталтаар өөрийгөө {cyc['loop_percent']:g}% эзэмшинэ)" if cyc["loop_percent"] else ""
            kind = "mutual_holding" if cyc["mutual"] else "ownership_cycle"
            label = "Харилцан эзэмшил" if cyc["mutual"] else "Тойрог эзэмшил"
            add(kind, "warn", f"{label}: {' → '.join(ring)}{pct}", [n["key"] for n in cyc["companies"]])

    for x in cached(g, "cross_holdings", cross_holdings):
        if x["company"] == ticker and x["reverse_percent"] is None:
            add("listed_holder", "info", f"Бүртгэлтэй компани {x['holder']} {x['percent']:g}% эзэмшдэг"
                if x["percent"] is not None else f"Бүртгэлтэй компани {x['holder']} эзэмшдэг", [x["holder_key"]])

    uo = g.ultimate_owners(ticker)
    if uo and uo[0]["effective_percent"] >= 50:
        add("controller", "info", f"Хяналтын эзэмшигч: {uo[0]['name']} ({uo[0]['effective_percent']:g}% look-through)",
            [uo[0]["key"]])

    held = g.ownership_summary(me)
    if held["nominee"]["holders"]:
        pct = held["nominee"]["percent"]
        add("nominee_holding", "info",
            f"Номини / кастодиан эзэмшил: {held['nominee']['holders']} эзэмшигч"
            + (f", нийт {pct:g}%" if pct is not None else "") + " (харилцагчдын нэрийн өмнөөс; эзэн нь тодорхойгүй)")
    if held["treasury"]["holders"] and held["treasury"]["percent"] is not None:
        add("treasury", "info", f"Өөрийн (буцаан авсан) хувьцаа: {held['treasury']['percent']:g}%")

    for s in cached(g, "state_ownership", state_ownership):
        if s["key"] == me:
            direct = f"шууд {s['direct_percent']:g}%, " if s["direct_percent"] else ""
            add("state", "info", f"Төрийн оролцоо: {direct}look-through {s['lookthrough_percent']:g}%",
                [o["key"] for o in s["top_owners"]])

    for c in cached(g, "insider_owners", insider_owners):
        co = c["companies"][0]
        if co["ticker"] == ticker:
            pct = f" {co['percent']:g}%-ийг" if co["percent"] is not None else ""
            add("insider_owner", "info", f"{c['name']} ({', '.join(co['titles'])}) компанийн хувьцааны{pct} эзэмшдэг",
                [c["key"]])

    for c in cached(g, "competitor_links", competitor_links, min_stake):
        tickers = [co["ticker"] for co in c["companies"]]
        if ticker in tickers:
            mine = next(co for co in c["companies"] if co["ticker"] == ticker)
            others = "; ".join(f"{_co(co)}: {', '.join(co['roles'])}"
                               for co in c["companies"] if co["ticker"] != ticker)
            add("competitor", "warn",
                f"{c['name']} ({', '.join(mine['roles'])}) ижил салбарын ({c['sector']}) өөр компанид бас оролцоотой — {others}",
                [c["key"]] + [co["key"] for co in c["companies"]])

    for r in cached(g, "related_parties", related_parties, min_stake):
        sides = {r["company"]["key"], r["counterparty"]["key"]}
        if me in sides and r["relative"]:
            q = r["relative"]
            basis = BASIS_MN.get(q["basis"], q["basis"] or "төрөл")
            where = ("мөн энэ компанид" if r["relation"] == "same_company" else
                     f"түүний {'хувьцаа эзэмшигч' if r['relation'] == 'shareholder' else 'охин компани'} "
                     f"{_co(r['counterparty'])}-д")
            add("related_party", "info",
                f"{r['name']} ({_co(r['company'])}: {', '.join(r['company']['roles'])}) — {basis} {q['name']} "
                f"{where} ({', '.join(r['counterparty']['roles'])}); баталгаажсан холбоо, эх сурвалж: {q['source']}",
                [r["key"], q["key"], r["counterparty"]["key"], r["company"]["key"]])
        elif me in sides:
            rel = "хувьцаа эзэмшигч" if r["relation"] == "shareholder" else "охин компани"
            add("related_party", "info",
                f"{r['name']}: {_co(r['company'])} ({', '.join(r['company']['roles'])}) болон түүний {rel} "
                f"{_co(r['counterparty'])} ({', '.join(r['counterparty']['roles'])})",
                [r["key"], r["counterparty"]["key"], r["company"]["key"]])

    # the FRC's adverse decisions about the company (frc.py): suspension, revocation, penalty, ...
    from datetime import date as _date
    from .frc import _env_float
    since = f"{_date.today().year - int(_env_float('FRC_FLAG_YEARS', 3))}-01-01"
    for d in g.run("""MATCH (d:FrcDecision)-[:CONCERNS]->(c:Company {key: $me}) WHERE d.adverse AND d.date >= $since
                      RETURN d.date AS date, d.number AS number, d.name AS name ORDER BY d.date DESC LIMIT 10""",
                   me=me, since=since):
        add("frc_decision", "warn", f"СЗХ-ны шийдвэр {d['date']}" + (f", №{d['number']}" if d["number"] else "")
            + f": {d['name']}", [me])

    order = {"warn": 0, "info": 1}
    flags.sort(key=lambda f: order[f["level"]])
    return flags
