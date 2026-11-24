"""The company dossier (issue #10): everything the graph knows about one company, for one printable
page (`/dossier/<ticker>`, static/dossier.html) and `/api/companies/<ticker>/dossier`.

It gathers what the details panel loads piece by piece, in one response:

    entity, owners, people, beneficial_owners, holdings, history, facts,
    related_companies, ownership, declared_interests      Graph.entity (with provenance)
    ultimate_owners                                       look-through owners (Graph.ultimate_owners)
    controls                                              companies it controls or influences (groups.group_of)
    flags                                                 governance checks (analysis.company_flags)
    changes                                               significant changes, newest first (history)
    ownership_history                                     the shareholder table over time (history)
    concentration, turnover, market                       quality / trends / market, as the panel shows them
    reports                                               its documents on open.mse.mn and their findings (reports.py)
    quality                                               the company's data problems (quality.company_issues)
    sources                                               every source behind the page, once each

Nothing is stored; every part reads current edges and their history.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import analysis, groups, history, provenance, quality, trends
from . import market as mk

# the most changes one page lists (the page says when there are more)
MAX_CHANGES = 60


def _sources(out: dict) -> list[dict]:
    """Every source behind the rows of the page, once each, the most used first."""
    seen: dict[str, dict] = {}
    rows = []
    for part in ("owners", "people", "beneficial_owners", "holdings", "beneficially_owns", "facts"):
        rows += out.get(part) or []
        rows += (out.get("history") or {}).get(part) or []
    for r in rows:
        for p in r.get("provenance") or []:
            s = seen.setdefault(p["source"], {**p, "rows": 0})
            s["rows"] += 1
    return sorted(seen.values(), key=lambda s: (-s["rows"], s["label"] or ""))


def company_dossier(g, ticker: str, changes_limit: int = MAX_CHANGES) -> dict | None:
    """The dossier of the company `ticker` names (a ticker, a former ticker or a register number);
    None when no company has it."""
    t = g.resolve_ticker(ticker)
    key = g.company_keys().get(t)
    if not key:
        return None
    out = g.entity(key)
    if not out or out["entity"]["type"] != "Company":
        return None
    key = out["entity"]["key"]
    out["ticker"] = t
    out["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out["ultimate_owners"] = g.ultimate_owners(t)
    from . import affiliations
    # the related parties the company's own page discloses per shareholder (mse.mn)
    out["disclosed_affiliations"] = affiliations.company_affiliations(g, t)
    for a in out["disclosed_affiliations"]:
        provenance.annotate(a)
    grp = groups.group_of(g, key)
    out["controls"] = [c for c in (grp or {}).get("companies", []) if c["key"] != key]
    out["flags"] = analysis.company_flags(g, t)
    from . import frc
    out["frc_decisions"] = frc.company_decisions(g, key)
    from . import reports
    # the documents the company publishes on open.mse.mn and the unchecked findings in its PDFs
    out["reports"] = reports.company_reports(g, key, limit=40)
    chg = history.recent_changes(g, ticker=t, significant_only=True, limit=changes_limit + 1)
    out["changes"], out["changes_more"] = chg[:changes_limit], len(chg) > changes_limit
    out["ownership_history"] = history.ownership_timeline(g, t)
    rows = quality.company_rows(g)
    out["concentration"] = quality.company_concentration(g, key)
    out["turnover"] = trends.company_turnover(g, key)
    out["market"] = mk.company_market(g, key)
    mine = [r for r in rows if r["key"] == key]
    out["quality"] = quality.company_issues(mine[0]) if mine else []
    out["coverage"] = quality.coverage(g, None, rows)
    out["sources"] = _sources(out)
    return out
