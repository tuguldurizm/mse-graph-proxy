"""HTTP API + web UI.  Run:  uvicorn mse_graph.api:app --reload"""
from __future__ import annotations

import base64
import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path

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
        threading.Thread(target=pipeline.run, name="pipeline", daemon=True).start()
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
    e = g().entity(key)
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


@app.get("/api/changes")
def changes(since: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"), limit: int = 500):
    """Positions and stakes that started or ended on or after `since`."""
    return g().changes(since, limit)
