"""Coverage (issue #27), concentration (#7) and the data-quality report (#8).

All three rest on one counting pass over the **listed** companies (`company_rows`): each
company's current stakes, officers, register numbers and sector. Only current edges count.

    coverage        how many listed companies each kind of data covers, and the `coverage`
                    block every /api/analysis/* response carries ("based on 158/160
                    companies; registry owners for 2/160"), so a result never looks more
                    certain than the data behind it
    concentration   per company: the largest direct and look-through holders, a free-float
                    estimate, HHI, top-1 / top-3 shares and the owner-type mix (MSE
                    percentages only: a registry row without a percent counts for nothing)
    report          per company: the problems in its data (shareholdings over 100%, no
                    officers or CEO, no register number, stale `as_of`, no sector, a holder
                    listed twice), most severe first, with counts per problem

Stakes are typed (issue #39): only `direct` / `unknown` stakes are ownership. A custodian's
stake is held for its clients, so it is reported as the nominee share and counted as part
of the free float; treasury shares are no one's and are not free float. All three use the
same sums, so the quality check "percentages add up to more than 100%" is the coverage
part "holdings that add up to a plausible total".
"""
from __future__ import annotations

import statistics
from datetime import date, timedelta

from . import holdings as hold
from .normalize import _LEGAL_FORM_RE, clean

# a stake at or above this is a "disclosed large holding" (MSE lists holders of 5% and more)
MIN_STAKE = 5.0
# percentages may add up to a little over 100 through rounding
SUM_TOLERANCE = 0.5
# a shareholder table whose latest `as_of` is older than this is stale (annual disclosure + slack)
STALE_AS_OF_DAYS = 548

_TYPE = "[l IN labels({0}) WHERE l IN ['Company', 'Person', 'Organization']][0]"
_REGISTRY = "any(s IN coalesce({r}.sources, [{r}.source]) WHERE coalesce(s, '') CONTAINS 'opendatalab.mn')"

_COMPANIES = f"""
MATCH (c:Company) WHERE coalesce(c.listed, true)
CALL (c) {{
    OPTIONAL MATCH (h:Entity)-[o:OWNS {{current: true}}]->(c)
    RETURN collect(CASE WHEN h IS NULL THEN NULL ELSE
        {{key: h.key, name: h.name, type: {_TYPE.format("h")}, org_type: h.org_type,
          listed: CASE WHEN h:Company THEN coalesce(h.listed, true) END,
          percent: o.percent, as_of: o.as_of, holding_type: coalesce(o.holding_type, 'direct'),
          registry: {_REGISTRY.format(r="o")}}} END) AS stakes
}}
CALL (c) {{
    OPTIONAL MATCH (p:Person)-[r:HOLDS_POSITION {{current: true}}]->(c)
    RETURN collect(CASE WHEN p IS NULL THEN NULL ELSE
        {{key: p.key, role_std: coalesce(r.role_std, 'unknown'), title: r.title,
          registry: {_REGISTRY.format(r="r")}}} END) AS officers
}}
RETURN c.key AS key, c.ticker AS ticker, c.name AS name, c.sector AS sector, c.sector_mse AS sector_mse,
       c.sector_std AS sector_std, c.register_no AS register_no, c.state_reg_no AS state_reg_no,
       c.registry_url AS registry_url, c.registry_lists_at AS registry_lists_at, c.market_cap AS market_cap,
       stakes, officers
ORDER BY c.ticker
"""

# Look-through (Graph.ultimate_owners), for every listed company at once: chains of current,
# percent-bearing ownership stakes from owners nobody owns.
_LOOKTHROUGH = f"""
MATCH p = (owner:Entity)-[:OWNS*1..5]->(c:Company)
WHERE coalesce(c.listed, true)
  AND all(r IN relationships(p) WHERE r.current AND r.percent IS NOT NULL AND {hold.real('r')})
  AND NOT EXISTS {{ MATCH (owner)<-[x:OWNS {{current: true}}]-(:Entity) WHERE {hold.real('x')} }}
  AND all(i IN range(0, size(nodes(p)) - 2) WHERE NOT nodes(p)[i] IN nodes(p)[i + 1..])
WITH c, owner, sum(reduce(x = 100.0, r IN relationships(p) | x * r.percent / 100.0)) AS eff
ORDER BY eff DESC
WITH c, collect({{key: owner.key, name: owner.name, type: {_TYPE.format("owner")}, percent: round(eff, 3)}}) AS owners
RETURN c.key AS key, owners[0] AS top
"""


def company_rows(g) -> list[dict]:
    """One row per listed company: its current stakes and officers, with the sums the three
    reports share (`disclosed`: ownership stakes with a percent; `total`: every stake with a
    percent, nominee and treasury included)."""
    rows = g.run(_COMPANIES)
    for r in rows:
        own = [s for s in r["stakes"] if s["percent"] is not None]
        r["with_percent"] = own
        r["total"] = round(sum(s["percent"] for s in own), 4) if own else None
        r["sector_any"] = bool(r["sector_std"] or clean(r["sector_mse"]) or clean(r["sector"]))
    return rows


def _plausible(r: dict) -> bool:
    return r["total"] is not None and 0 < r["total"] <= 100 + SUM_TOLERANCE


# ---- coverage (issue #27) ---------------------------------------------------------

# name -> (English basis, Mongolian label, test on a company row)
PARTS = {
    "holdings": ("disclosed holdings with a percent (MSE)", "хувьтай эзэмшил (MSE)",
                 lambda r: bool(r["with_percent"])),
    "holdings_plausible": ("holdings that add up to at most 100%", "нийлбэр нь 100%-иас хэтрээгүй эзэмшил",
                           _plausible),
    "holders": ("any shareholder (MSE or registry)", "хувьцаа эзэмшигчтэй",
                lambda r: bool(r["stakes"])),
    "officers": ("board members or executives", "ТУЗ / удирдлагатай",
                 lambda r: bool(r["officers"])),
    "registry": ("registry page read (Open Data Lab)", "Open Data Lab бүртгэл уншсан",
                 lambda r: bool(r["registry_url"])),
    "registry_owners": ("registry owners (Open Data Lab)", "бүртгэлийн эзэмшигч (Open Data Lab)",
                        lambda r: any(s["registry"] for s in r["stakes"])),
    "registry_officers": ("registry officers (Open Data Lab)", "бүртгэлийн удирдлага (Open Data Lab)",
                          lambda r: any(o["registry"] for o in r["officers"])),
    "register_no": ("a 7-digit register number", "7 оронтой регистрийн дугаартай",
                    lambda r: bool(r["register_no"])),
    "sector": ("a sector", "салбартай", lambda r: r["sector_any"]),
    "sector_std": ("a standard sector (registry activity codes)", "стандарт салбартай (үйл ажиллагааны код)",
                   lambda r: bool(r["sector_std"])),
    "registry_lists": ("registry page read for officials, licences and tenders (Open Data Lab)",
                       "албан тушаалтан, тусгай зөвшөөрөл, тендер уншсан (Open Data Lab)",
                       lambda r: bool(r.get("registry_lists_at"))),
    "market_cap": ("a market cap (mse.mn price x shares)", "зах зээлийн үнэлгээтэй (mse.mn)",
                   lambda r: r.get("market_cap") is not None),
}

# The data each analysis rests on: the first part is its basis, the others add a line.
ANALYSES = {
    "interlocks": ("officers", "holdings", "registry_officers"),
    "common-owners": ("holders", "registry_owners"),
    "board-overlap": ("officers", "registry_officers"),
    "moves": ("officers", "registry_officers"),
    "cycles": ("holdings", "registry_owners"),
    "cross-holdings": ("holdings", "registry_owners"),
    "conflicts": ("officers", "holdings", "sector", "sector_std"),
    "state-ownership": ("holdings", "registry_owners"),
    "nominees": ("holdings", "registry_owners"),
    "roles": ("officers", "registry_officers"),
    "resolution": ("officers", "registry_officers"),
    "concentration": ("holdings", "holdings_plausible", "registry_owners", "market_cap"),
    "quality": ("holdings", "officers", "register_no", "registry", "sector"),
    "groups": ("holdings", "holdings_plausible", "registry_owners", "market_cap"),
    "communities": ("holders", "officers", "registry_owners", "registry_officers"),
    "centrality": ("holders", "officers", "registry_owners", "registry_officers"),
    "turnover": ("officers", "registry_officers"),
    "accumulation": ("holdings", "registry_owners"),
    "trends": ("holdings", "officers", "registry_owners", "registry_officers"),
    "market": ("market_cap", "holdings"),
    "officials": ("registry_lists", "registry"),
}


def coverage_counts(rows: list[dict]) -> dict:
    """{part: number of listed companies it covers} plus `of`, the number of listed companies."""
    out = {name: sum(1 for r in rows if test(r)) for name, (_b, _l, test) in PARTS.items()}
    out["of"] = len(rows)
    return out


def coverage(g, analysis: str | None = None, rows: list[dict] | None = None) -> dict:
    """The coverage block for one analysis (ANALYSES), or for every part when `analysis` is None:

        {"basis": "disclosed holdings with a percent (MSE)", "companies": 158, "of": 160,
         "parts": [{"name", "basis", "label", "companies", "of"}, ...],
         "text": "based on 158/160 companies; registry owners (Open Data Lab) for 2/160"}
    """
    counts = coverage_counts(company_rows(g) if rows is None else rows)
    names = ANALYSES.get(analysis or "", tuple(PARTS))
    of = counts["of"]
    parts = [{"name": n, "basis": PARTS[n][0], "label": PARTS[n][1], "companies": counts[n], "of": of}
             for n in names]
    text = f"based on {parts[0]['companies']}/{of} companies ({parts[0]['basis']})" + "".join(
        f"; {p['basis']} for {p['companies']}/{of}" for p in parts[1:])
    return {"basis": parts[0]["basis"], "companies": parts[0]["companies"], "of": of,
            "parts": parts, "text": text}


# ---- concentration (issue #7) -------------------------------------------------------

def _owner_kind(s: dict, state: set[str]) -> str:
    if s["key"] in state:
        return "state"
    if s["type"] == "Company" and s["listed"]:
        return "listed_company"
    if s["type"] == "Person":
        return "individual"
    return "organization"


OWNER_KINDS = ("state", "listed_company", "organization", "individual", "nominee")


def _concentration(r: dict, state: set[str], top: dict | None) -> dict | None:
    stakes = r["with_percent"]
    if not stakes:
        return None
    own = sorted((s for s in stakes if s["holding_type"] in ("direct", "unknown") and s["key"] != r["key"]),
                 key=lambda s: -s["percent"])
    pcts = [s["percent"] for s in own]
    treasury = sum(s["percent"] for s in stakes if s["holding_type"] == "treasury")
    nominee = sum(s["percent"] for s in stakes if s["holding_type"] == "nominee")
    large = sum(p for p in pcts if p >= MIN_STAKE)
    mix = dict.fromkeys(OWNER_KINDS, 0.0)
    for s in own:
        mix[_owner_kind(s, state)] += s["percent"]
    mix["nominee"] = nominee
    first = own[0] if own else None
    return {
        "key": r["key"], "ticker": r["ticker"], "name": r["name"],
        "holders": len(own),
        "largest": {"key": first["key"], "name": first["name"], "type": first["type"],
                    "percent": first["percent"]} if first else None,
        "largest_lookthrough": top,
        "controlled": bool(top and top["percent"] >= 50),
        "disclosed_percent": round(sum(pcts), 3),
        "large_holdings_percent": round(large, 3),
        "nominee_percent": round(nominee, 3),
        "treasury_percent": round(treasury, 3),
        # MSE tables list only large holders: what they don't list is assumed to float
        "free_float_estimate": round(max(0.0, 100.0 - large - treasury), 3),
        "hhi": round(sum(p * p for p in pcts), 1),
        "top1": round(pcts[0], 3) if pcts else 0.0,
        "top3": round(sum(pcts[:3]), 3),
        "owner_mix": {k: round(v, 3) for k, v in mix.items()},
        "plausible": _plausible(r),
        "total_percent": r["total"],
    }


SORTS = {"hhi": lambda c: -c["hhi"], "top1": lambda c: -c["top1"], "top3": lambda c: -c["top3"],
         "free_float": lambda c: c["free_float_estimate"], "ticker": lambda c: c["ticker"] or ""}


def concentration(g, sort: str = "hhi", rows: list[dict] | None = None) -> dict:
    """Ownership concentration of each listed company with disclosed percentages (issue #7).

    `hhi` is Σ s² over the ownership stakes, in percent points (0 to 10,000); `free_float_estimate`
    is 100 − the stakes of 5% and more − treasury shares (an estimate: MSE lists only large
    holders, and a nominee's stake is assumed to float). `owner_mix` sums the stakes per owner
    type. `largest_lookthrough` follows chains of holdings (Graph.ultimate_owners); a holding of
    50% or more there marks the company `controlled`."""
    from .analysis import state_entities

    rows = company_rows(g) if rows is None else rows
    state = {s["key"] for s in state_entities(g)}
    tops = {t["key"]: t["top"] for t in g.run(_LOOKTHROUGH)}
    items = [c for c in (_concentration(r, state, tops.get(r["key"])) for r in rows) if c]
    items.sort(key=lambda c: (SORTS.get(sort, SORTS["hhi"])(c), c["ticker"] or ""))
    ok = [c for c in items if c["plausible"]]
    summary = {
        "companies": len(items),
        "plausible": len(ok),
        "controlled": sum(1 for c in items if c["controlled"]),
        "median_hhi": round(statistics.median(c["hhi"] for c in ok), 1) if ok else None,
        "median_top1": round(statistics.median(c["top1"] for c in ok), 3) if ok else None,
        "median_free_float": round(statistics.median(c["free_float_estimate"] for c in ok), 3) if ok else None,
    }
    # market level weighted by market cap (issue #31): the HHI of the average tugrik invested
    caps = {r["key"]: r["market_cap"] for r in rows if r.get("market_cap")}
    weighted = [(c["hhi"], caps[c["key"]]) for c in ok if caps.get(c["key"])]
    total = sum(w for _h, w in weighted)
    summary["weighted_hhi"] = round(sum(h * w for h, w in weighted) / total, 1) if total else None
    summary["weighted_companies"] = len(weighted)
    return {"items": items, "summary": summary, "coverage": coverage(g, "concentration", rows)}


def company_concentration(g, key: str) -> dict | None:
    """The concentration row of one company (None when it has no disclosed percentages)."""
    key = g.resolve_key(key)
    rows = [r for r in company_rows(g) if r["key"] == key]
    if not rows:
        return None
    from .analysis import state_entities
    state = {s["key"] for s in state_entities(g)}
    top = {t["key"]: t["top"] for t in g.run(_LOOKTHROUGH)}.get(key)
    return _concentration(rows[0], state, top)


# ---- data-quality report (issue #8) ---------------------------------------------------

LEVELS = ("error", "warn", "info")

# code -> (level, Mongolian description)
ISSUES = {
    "percent_over_100": ("error", "эзэмшлийн хувийн нийлбэр 100%-иас их"),
    "duplicate_holder": ("error", "нэг эзэмшигч хоёр удаа, өөр хувьтай"),
    "duplicate_holder_same": ("warn", "нэг эзэмшигч хоёр удаа"),
    "no_shareholders": ("warn", "хувьцаа эзэмшигч алга"),
    "no_percent": ("warn", "эзэмшигчид хувь алга (зөвхөн бүртгэлээс)"),
    "no_officers": ("warn", "ТУЗ / удирдлага алга"),
    "no_ceo": ("warn", "гүйцэтгэх захирал алга"),
    "no_register_no": ("warn", "7 оронтой регистрийн дугааргүй: Open Data Lab-аас баяжуулах боломжгүй"),
    "stale_as_of": ("warn", "эзэмшлийн мэдээлэл хуучирсан"),
    "no_sector": ("warn", "салбаргүй: ижил салбарын шалгалтад орохгүй"),
    "registry_not_read": ("info", "регистрийн дугаартай ч бүртгэлийн хуудас уншаагүй"),
    "missing_as_of": ("info", "эзэмшлийн огноо (as_of) алга"),
    "no_sector_std": ("info", "стандарт салбаргүй (зөвхөн MSE-ийн текст)"),
    "unknown_roles": ("info", "танигдаагүй албан тушаалын нэр"),
}


def _date_end(v: str) -> str:
    """An ISO date, year or year-month as the last day it can mean, for comparing."""
    v = (v or "")[:10]
    if len(v) == 4:
        return v + "-12-31"
    if len(v) == 7:
        return v + "-28"
    return v


def _holder_name(name: str) -> str:
    """A holder's name for spotting duplicates: case, punctuation, word order and legal form ignored."""
    from .review import fold
    return fold(_LEGAL_FORM_RE.sub("", clean(name).casefold()))


def company_issues(r: dict, today: date | None = None) -> list[dict]:
    """The data problems of one company row, as [{code, level, text, ...details}]."""
    today = today or date.today()
    out: list[dict] = []

    def add(code: str, detail: str = "", **extra):
        level, text = ISSUES[code]
        # `detail` apart too, so the English UI can put it after its own text (issue #10)
        out.append({"code": code, "level": level, "text": text + (f": {detail}" if detail else ""),
                    "detail": detail, **extra})

    stakes, pct = r["stakes"], r["with_percent"]
    if not stakes:
        add("no_shareholders")
    elif not pct:
        add("no_percent", f"{len(stakes)} эзэмшигч")
    if r["total"] is not None and r["total"] > 100 + SUM_TOLERANCE:
        add("percent_over_100", f"{r['total']:g}%", total=r["total"])
    by_name: dict[str, list[dict]] = {}
    for s in stakes:
        by_name.setdefault(_holder_name(s["name"] or s["key"]), []).append(s)
    for same in by_name.values():
        if len(same) > 1:
            percents = [s["percent"] for s in same]
            differ = len({p for p in percents}) > 1
            add("duplicate_holder" if differ else "duplicate_holder_same",
                f"{same[0]['name']} ({', '.join('?' if p is None else f'{p:g}%' for p in percents)})",
                keys=[s["key"] for s in same])
    if not r["officers"]:
        add("no_officers")
    elif not any(o["role_std"] == "ceo" for o in r["officers"]):
        add("no_ceo")
    unknown = sorted({o["title"] or "" for o in r["officers"] if o["role_std"] == "unknown"})
    if unknown:
        add("unknown_roles", "; ".join(unknown[:5]), titles=unknown)
    if not r["register_no"]:
        add("no_register_no", f"MSE-ийн дугаар {r['state_reg_no']}" if r["state_reg_no"] else "")
    elif not r["registry_url"]:
        add("registry_not_read", r["register_no"])
    if pct:
        dated = [s["as_of"] for s in pct if s["as_of"]]
        if len(dated) < len(pct):
            add("missing_as_of", f"{len(pct) - len(dated)}/{len(pct)} эзэмшил")
        latest = max(dated, default=None)
        if latest and _date_end(latest) < (today - timedelta(days=STALE_AS_OF_DAYS)).isoformat():
            add("stale_as_of", latest, as_of=latest)
    if not r["sector_any"]:
        add("no_sector")
    elif not r["sector_std"]:
        add("no_sector_std")
    out.sort(key=lambda i: LEVELS.index(i["level"]))
    return out


def report(g, today: date | None = None, rows: list[dict] | None = None) -> dict:
    """The data-quality report over the listed companies: each company with its problems,
    most severe first (errors, then warnings, then notes), and the number of companies per
    problem. Companies without problems are counted in `clean`."""
    rows = company_rows(g) if rows is None else rows
    companies = []
    counts = dict.fromkeys(ISSUES, 0)
    for r in rows:
        issues = company_issues(r, today)
        for code in {i["code"] for i in issues}:
            counts[code] += 1
        if issues:
            per = {lv: sum(1 for i in issues if i["level"] == lv) for lv in LEVELS}
            companies.append({"key": r["key"], "ticker": r["ticker"], "name": r["name"],
                              "level": issues[0]["level"], "counts": per, "issues": issues})
    companies.sort(key=lambda c: (LEVELS.index(c["level"]), -c["counts"]["error"], -c["counts"]["warn"],
                                  -c["counts"]["info"], c["ticker"] or ""))
    summary = {code: {"level": ISSUES[code][0], "text": ISSUES[code][1], "companies": n}
               for code, n in counts.items()}
    return {"companies": companies, "summary": summary, "of": len(rows),
            "clean": len(rows) - len(companies),
            "levels": {lv: sum(1 for c in companies if c["level"] == lv) for lv in LEVELS},
            "coverage": coverage(g, "quality", rows)}


def summary_line(g) -> dict:
    """Short counts for the pipeline log after each run: companies per problem level and the
    main coverage figures."""
    rows = company_rows(g)
    rep = report(g, rows=rows)
    cov = coverage_counts(rows)
    return {"companies": rep["of"], "with_errors": rep["levels"]["error"], "with_warnings": rep["levels"]["warn"],
            "coverage": {k: cov[k] for k in ("holdings", "officers", "register_no", "registry", "sector")}}
