"""HTTP API + web UI.  Run:  uvicorn mse_graph.api:app --reload"""
from __future__ import annotations

import base64
import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .graph import Graph

STATIC = Path(__file__).parent / "static"
_graph: Graph | None = None


@asynccontextmanager
async def lifespan(_app):
    if os.getenv("RUN_PIPELINE") == "1":
        import logging
        import threading

        from . import pipeline

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        threading.Thread(target=pipeline.run_forever, name="pipeline", daemon=True).start()
    yield
    if _graph:
        _graph.close()


app = FastAPI(title="MSE Connections Graph", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# Optional password gate for public deployments: set APP_PASSWORD and log in
# with any username and that password (HTTP Basic auth).
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
OPEN_PATHS = {"/healthz"}


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if APP_PASSWORD and request.url.path not in OPEN_PATHS:
        ok = False
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                _user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = hmac.compare_digest(pw, APP_PASSWORD)
            except (ValueError, UnicodeDecodeError):
                ok = False
        if not ok:
            return Response("Login required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="MSE graph"'})
    return await call_next(request)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


@app.get("/api/status")
def status():
    from .pipeline import read_status
    return read_status()


def g() -> Graph:
    global _graph
    if _graph is None:
        _graph = Graph()
    return _graph


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/stats")
def stats():
    return g().stats()


@app.get("/api/search")
def search(q: str = Query(..., min_length=1), limit: int = 20):
    return g().search(q, limit)


@app.get("/api/entity")
def entity(key: str):
    e = g().entity(key)     # old keys (company:APU, company:~2702673) still resolve
    if not e:
        raise HTTPException(404, "not found")
    return e


@app.get("/api/graph/neighborhood")
def neighborhood(key: str, depth: int = 1):
    return g().neighborhood(key, depth)


@app.get("/api/graph/overview")
def overview(limit: int = 400):
    return g().overview(limit)


@app.get("/api/graph/path")
def path(a: str, b: str, max_hops: int = 8):
    return g().shortest_path(a, b, max_hops)


@app.get("/api/companies/{ticker}/ultimate-owners")
def ultimate_owners(ticker: str, max_depth: int = 5, min_percent: float = 1.0):
    return g().ultimate_owners(ticker, max_depth, min_percent)


# ---- history / changes (read from the graph's edge history, see history.py) --------

def _ts(day: str) -> str:
    """Observation date -> RFC 3339 timestamp for Atom."""
    return (day or "1970-01-01")[:10] + "T00:00:00Z"


@app.get("/api/changes")
def changes(ticker: str | None = None, key: str | None = None, source: str | None = None,
            since: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}"),
            all_: bool = Query(False, alias="all"), limit: int = Query(200, le=2000)):
    """Detected changes, newest first. Small moves and data gaps are hidden unless all=true."""
    from . import history
    ticker = g().resolve_ticker(ticker) if ticker else None
    key = g().resolve_key(key) if key else None
    return history.recent_changes(g(), source=source, ticker=ticker, key=key, since=since,
                                  significant_only=not all_, limit=limit)


@app.get("/api/companies/{ticker}/ownership-history")
def ownership_history(ticker: str):
    from . import history
    return history.ownership_timeline(g(), g().resolve_ticker(ticker))


@app.get("/feed.xml", include_in_schema=False)
def feed(request: Request, ticker: str | None = None, limit: int = Query(100, le=500)):
    """Atom feed of significant changes, for feed readers or chat integrations."""
    from . import history
    from .notify import describe

    ticker = g().resolve_ticker(ticker) if ticker else None
    items = history.recent_changes(g(), ticker=ticker, significant_only=True, limit=limit)
    base = str(request.base_url).rstrip("/")
    updated = _ts(items[0]["detected_at"]) if items else _ts("")
    title = "MSE граф: өөрчлөлтүүд" + (f" · {ticker.upper()}" if ticker else "")
    entries = []
    for c in items:
        line = f"{c['ticker']}: {describe(c)}"
        uid = "|".join(str(c.get(k, "")) for k in ("detected_at", "source", "kind", "ticker", "entity_key", "old", "new"))
        entries.append(
            "<entry>"
            f"<id>urn:mse-graph:{xml_escape(uid)}</id>"
            f"<title>{xml_escape(line)}</title>"
            f"<updated>{xml_escape(_ts(c['detected_at']))}</updated>"
            f'<link href="{xml_escape(base)}/?focus=company:{xml_escape(c["ticker"])}"/>'
            f"<category term=\"{xml_escape(c['kind'])}\"/>"
            f"<content type=\"text\">{xml_escape(c.get('company', '') + ' — ' + describe(c))}</content>"
            "</entry>"
        )
    xml = ('<?xml version="1.0" encoding="utf-8"?>'
           '<feed xmlns="http://www.w3.org/2005/Atom">'
           f"<title>{xml_escape(title)}</title><id>urn:mse-graph:feed:{xml_escape(ticker or 'all')}</id>"
           f"<updated>{xml_escape(updated)}</updated>"
           f'<link rel="self" href="{xml_escape(str(request.url))}"/>'
           + "".join(entries) + "</feed>")
    return Response(xml, media_type="application/atom+xml")


# Every /api/analysis/* response carries `coverage` (issue #27, quality.coverage): how many
# listed companies the data behind it covers. A list result comes as {"items": [...], "coverage"}.

def _covered(name: str, result):
    from . import quality
    cov = quality.coverage(g(), name)
    if isinstance(result, dict):
        return {**result, "coverage": cov}
    return {"items": result, "coverage": cov}


@app.get("/api/analysis/interlocks")
def interlocks(min_companies: int = 2):
    return _covered("interlocks", g().interlocks(min_companies))


@app.get("/api/analysis/common-owners")
def common_owners(min_companies: int = 2):
    return _covered("common-owners", g().common_owners(min_companies))


@app.get("/api/analysis/board-overlap")
def board_overlap():
    return _covered("board-overlap", g().board_overlap())


@app.get("/api/analysis/moves")
def moves():
    """People who used to hold a position at one listed company and now hold one at another."""
    return _covered("moves", g().moves())


# ---- governance checks (analysis.py) ---------------------------------------------

@app.get("/api/analysis/cycles")
def cycles(max_len: int = 6):
    from . import analysis
    return _covered("cycles", analysis.cycles(g(), max_len))


@app.get("/api/analysis/cross-holdings")
def cross_holdings():
    from . import analysis
    return _covered("cross-holdings", analysis.cross_holdings(g()))


@app.get("/api/analysis/conflicts")
def conflicts(kind: str | None = Query(None, pattern="^(insider_owner|competitor|related_party)$"),
              min_stake: float = 5.0):
    from . import analysis
    return _covered("conflicts", analysis.conflicts(g(), kind, min_stake))


@app.get("/api/analysis/state-ownership")
def state_ownership():
    from . import analysis
    return _covered("state-ownership", analysis.state_ownership(g()))


@app.get("/api/analysis/nominees")
def nominees():
    """Holders that keep shares for others, stakes by holding type, and candidates (issue #39)."""
    from . import analysis
    return _covered("nominees", analysis.nominees(g()))


@app.get("/api/analysis/roles")
def position_roles():
    """Current positions per standard role, and the titles no rule recognises (issue #34)."""
    from . import analysis
    return _covered("roles", analysis.position_roles(g()))


@app.get("/api/analysis/resolution")
def resolution(min_companies: int = Query(6, ge=2), min_sectors: int = Query(4, ge=2)):
    """The entity-resolution review queue (issue #6, review.py): initial-form people a
    full registry name may stand for (`ambiguous_people`, #36), nodes that may be several
    people (`suspicious_people`) and keys that may be one entity (`possible_duplicates`).
    Items a reviewer decided or dismissed are left out."""
    from . import review
    return _covered("resolution", review.queue(g(), min_companies, min_sectors))


# ---- concentration (issue #7) and data quality (issue #8): quality.py ------------------

@app.get("/api/analysis/concentration")
def concentration(sort: str = Query("hhi", pattern="^(hhi|top1|top3|free_float|ticker)$")):
    """Per listed company: largest direct and look-through holders, free-float estimate,
    HHI, top-1 / top-3 shares and the owner-type mix (MSE percentages only)."""
    from . import quality
    return quality.concentration(g(), sort)


@app.get("/api/companies/{ticker}/concentration")
def company_concentration(ticker: str):
    from . import quality
    t = g().resolve_ticker(ticker)
    row = quality.company_concentration(g(), g().company_keys().get(t, "company:" + t))
    return {"concentration": row, "coverage": quality.coverage(g(), "concentration")}


@app.get("/api/quality")
def data_quality():
    """The data-quality report: each listed company's data problems, most severe first."""
    from . import quality
    return quality.report(g())


# ---- groups and control (issue #28): groups.py ------------------------------------------

@app.get("/api/analysis/groups")
def control_groups(min_companies: int = Query(1, ge=1), listed: bool = False):
    """One group per top owner (an owner no one controls): the companies it controls (> 50%
    through majority chains) or influences (the largest block, >= 20%), with the chain and
    the look-through %. Only current stakes with a percent count."""
    from . import groups
    return _covered("groups", groups.control_groups(g(), min_companies=min_companies, listed_only=listed))


@app.get("/api/analysis/groups/owner")
def control_group(key: str):
    """The control group of one owner (any holder), with its subgraph."""
    from . import groups, quality
    grp = groups.group_of(g(), key)
    if not grp:
        raise HTTPException(404, f"no entity {key}")
    return {"group": grp, "graph": groups.group_subgraph(g(), grp["key"]),
            "coverage": quality.coverage(g(), "groups")}


@app.get("/api/analysis/communities")
def communities(fresh: bool = False):
    """Detected groups (Louvain on the combined graph), as last written by the pipeline
    (`group_id`), else computed now; plus how well they recover the hand-curated groups."""
    from . import groups
    return _covered("communities", groups.communities(g(), fresh=fresh))


@app.get("/api/graph/community")
def community_graph(id: str):
    from . import groups
    return groups.community_subgraph(g(), id)


# ---- review decisions (issue #6) -----------------------------------------------------
# Writing needs REVIEW_PASSWORD (else APP_PASSWORD) in the X-Review-Password header.
# Without either, the review is read-only: a public site never takes decisions from anyone.

def _review_password() -> str:
    return os.getenv("REVIEW_PASSWORD") or os.getenv("APP_PASSWORD") or ""


@app.get("/api/review/status")
def review_status():
    from . import review
    return {"writable": bool(_review_password()), "pending_export": review.pending(g())}


@app.get("/api/review/decisions")
def review_decisions():
    from . import review
    return review.decisions(g())


@app.post("/api/review/decide")
async def review_decide(request: Request):
    """Record a decision and apply it at once. Body (JSON): `action` (merge | split |
    keep_apart | dismiss | revoke), and `key`, `into`, `companies`, `new_key`, `item`, `id`,
    `by`, `note` as the action needs (review.decide)."""
    from . import review
    pw = _review_password()
    if not pw:
        raise HTTPException(403, "the review is read-only here: set REVIEW_PASSWORD to take decisions")
    if not hmac.compare_digest(request.headers.get("x-review-password", "").encode(), pw.encode()):
        raise HTTPException(401, "wrong review password")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "send a JSON body") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "send a JSON object")
    args = {k: body.get(k) for k in ("key", "into", "new_key", "item", "id", "by", "note") if body.get(k)}
    if body.get("companies"):
        args["companies"] = [str(c) for c in body["companies"]][:50]
    try:
        return review.decide(g(), str(body.get("action") or ""), **{k: (v if k == "companies" else str(v))
                                                                    for k, v in args.items()})
    except review.ReviewError as exc:
        raise HTTPException(400, str(exc)) from None


@app.get("/api/review/export/{name}", include_in_schema=False)
def review_export(name: str):
    """The overrides files as they would be with the graph's decisions (to commit to git)."""
    from . import review
    if name == "identity.csv":
        text = review.identity_csv(review.overrides_in_force(g()))
    elif name == "reviewed.csv":
        text = review.reviewed_csv(review.all_reviewed(g()))
    else:
        raise HTTPException(404, "identity.csv or reviewed.csv")
    return Response(text, media_type="text/csv; charset=utf-8")


@app.get("/api/records")
def records(key: str):
    """The source records (issue #37) that name an entity: each row as its source gave it,
    how the loader decided who it names (REFERS_TO: method, confidence, decided_by) and
    the relationships it is behind."""
    rows = g().records(key)
    if not rows and g().entity(key) is None:
        raise HTTPException(404, f"no entity {key}")
    return {"key": g().resolve_key(key), "records": rows}


@app.get("/api/companies/{ticker}/flags")
def company_flags(ticker: str, min_stake: float = 5.0):
    from . import analysis
    return analysis.company_flags(g(), ticker, min_stake)
