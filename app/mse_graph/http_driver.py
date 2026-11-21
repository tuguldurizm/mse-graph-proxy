"""Neo4j over HTTPS: the Query API (POST /db/<database>/query/v2), for hosts that can't open Bolt.

`Graph` uses this when NEO4J_URI starts with http:// or https://, e.g.

    NEO4J_URI=https://xxxxxxxx.databases.neo4j.io     # Neo4j Aura, port 443

Each `run` is one auto-commit (implicit) transaction, like the Bolt `session.run` it replaces.
The bookmarks of each response are sent with the next request, so a query always sees the
writes of the ones before it, also on a clustered (Aura) database.

Values come back as plain JSON: numbers, strings, booleans, lists and maps as usual; temporal
values as ISO strings. Every query in graph.py returns maps or scalars (and converts its
datetimes with toString), so nothing depends on the driver's own types.
"""
from __future__ import annotations

import base64
import time
from typing import Any
from urllib.parse import urlsplit

import httpx


class QueryApiError(RuntimeError):
    def __init__(self, errors: list[dict]):
        self.errors = errors
        self.code = errors[0].get("code", "") if errors else ""
        super().__init__("; ".join(f'{e.get("code", "")}: {e.get("message", "")}' for e in errors))


class HttpDriver:
    """The subset of `neo4j.Driver` that `Graph` needs, over the Query API."""

    RETRY_CODES = ("Neo.TransientError.", "Neo.ClientError.Cluster.NotALeader")

    def __init__(self, uri: str, auth: tuple[str, str], database: str | None = None,
                 timeout: float = 120.0, retries: int = 3, transport: httpx.BaseTransport | None = None):
        parts = urlsplit(uri)
        self.base = f"{parts.scheme}://{parts.netloc}"
        self.host = parts.hostname or ""
        # NEO4J_DATABASE or a path (https://host/mydb) names the database. Otherwise: on Aura
        # (<id>.databases.neo4j.io) the instance id, which newer instances use as the database
        # name (the console's "Query API URL" shows it), else "neo4j".
        aura = self.host.split(".", 1)[0] if self.host.endswith(".databases.neo4j.io") else ""
        self.database = database or parts.path.strip("/") or aura or "neo4j"
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        self.client = httpx.Client(timeout=timeout, transport=transport, headers={
            "Authorization": f"Basic {token}", "Accept": "application/json",
            "Content-Type": "application/json", "User-Agent": "mse-graph/0.1"})
        self.retries = retries
        self.backoff = 1.0          # seconds; doubled on each retry
        self.bookmarks: list[str] = []

    def run(self, cypher: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"statement": cypher, "parameters": parameters or {}}
        for attempt in range(self.retries + 1):
            if self.bookmarks:
                body["bookmarks"] = self.bookmarks
            try:
                r = self.client.post(f"{self.base}/db/{self.database}/query/v2", json=body)
            except httpx.TransportError:
                if attempt == self.retries:
                    raise
                time.sleep(self.backoff * 2 ** attempt)
                continue
            payload = _json(r)
            errors = payload.get("errors") or []
            if r.status_code in (401, 403):
                raise QueryApiError(errors or [{"code": f"HTTP {r.status_code}",
                                                "message": "authentication failed"}])
            if errors:
                err = QueryApiError(errors)
                if attempt < self.retries and err.code.startswith(self.RETRY_CODES):
                    time.sleep(self.backoff * 2 ** attempt)
                    continue
                raise err
            if r.status_code >= 400:
                if attempt < self.retries and r.status_code in (429, 502, 503, 504):
                    time.sleep(self.backoff * 2 ** attempt)
                    continue
                raise QueryApiError([{"code": f"HTTP {r.status_code}", "message": r.text[:300]}])
            self.bookmarks = payload.get("bookmarks") or self.bookmarks
            data = payload.get("data") or {}
            fields = data.get("fields") or []
            return [dict(zip(fields, row)) for row in data.get("values") or []]
        raise AssertionError("unreachable")

    def verify_connectivity(self) -> None:
        self.run("RETURN 1 AS ok")

    def close(self) -> None:
        self.client.close()


def _json(r: httpx.Response) -> dict:
    try:
        out = r.json()
        return out if isinstance(out, dict) else {}
    except ValueError:
        return {}
