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


@app.get("/api/analysis/interlocks")
def interlocks(min_companies: int = 2):
    return g().interlocks(min_companies)


@app.get("/api/analysis/common-owners")
def common_owners(min_companies: int = 2):
    return g().common_owners(min_companies)


@app.get("/api/analysis/board-overlap")
def board_overlap():
    return g().board_overlap()


@app.get("/api/analysis/moves")
def moves():
    """People who used to hold a position at one listed company and now hold one at another."""
    return g().moves()


# ---- governance checks (analysis.py) ---------------------------------------------

@app.get("/api/analysis/cycles")
def cycles(max_len: int = 6):
    from . import analysis
    return analysis.cycles(g(), max_len)


@app.get("/api/analysis/cross-holdings")
def cross_holdings():
    from . import analysis
    return analysis.cross_holdings(g())


@app.get("/api/analysis/conflicts")
def conflicts(kind: str | None = Query(None, pattern="^(insider_owner|competitor|related_party)$"),
              min_stake: float = 5.0):
    from . import analysis
    return analysis.conflicts(g(), kind, min_stake)


@app.get("/api/analysis/state-ownership")
def state_ownership():
    from . import analysis
    return analysis.state_ownership(g())


@app.get("/api/analysis/roles")
def position_roles():
    """Current positions per standard role, and the titles no rule recognises (issue #34)."""
    from . import analysis
    return analysis.position_roles(g())


@app.get("/api/analysis/resolution")
def resolution():
    """The entity-resolution review queue (issue #6). So far: initial-form people
    ("Б.Батболд") that a full registry name may stand for but the rule didn't link
    (issue #36): two fitting full names at one company, or one at other companies only."""
    people = g().ambiguous_people()
    return {"ambiguous_people": people, "counts": {
        r: sum(1 for p in people if p["reason"] == r) for r in ("several_at_company", "no_shared_company")}}


@app.get("/api/companies/{ticker}/flags")
def company_flags(ticker: str, min_stake: float = 5.0):
    from . import analysis
    return analysis.company_flags(g(), ticker, min_stake)
