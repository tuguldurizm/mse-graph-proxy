"""Allow-listed fetch proxy for the MSE graph scraper.

Deployed on Render so the scraper can reach mse.mn / opendatalab.mn from
networks that can't. It is NOT an open proxy:

* every request needs the shared secret in the `X-Proxy-Token` header;
* only hosts in ALLOWED_HOSTS (and their subdomains) can be fetched,
  re-checked on every redirect hop;
* GET only, http/https only, response size capped.

    GET /fetch?url=https://mse.mn/mn/companies     (header X-Proxy-Token: ...)
    GET /healthz
"""
from __future__ import annotations

import hmac
import os
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Response

PROXY_TOKEN = os.environ.get("PROXY_TOKEN", "")
ALLOWED_HOSTS = [
    h.strip().lower()
    for h in os.environ.get("ALLOWED_HOSTS", "mse.mn,opendatalab.mn").split(",")
    if h.strip()
]
MAX_BYTES = int(os.environ.get("MAX_BYTES", str(25 * 1024 * 1024)))
MAX_REDIRECTS = 5
PASS_HEADERS = ("content-type", "last-modified", "etag", "content-disposition")

app = FastAPI(title="mse-graph fetch proxy", docs_url=None, redoc_url=None)
client = httpx.AsyncClient(
    timeout=httpx.Timeout(60, connect=15),
    follow_redirects=False,
    headers={"User-Agent": "mse-graph/0.1 (research crawler)"},
)


def host_allowed(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    host = parts.hostname.lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


@app.get("/healthz")
def healthz():
    return {"ok": True, "allowed_hosts": ALLOWED_HOSTS, "token_configured": bool(PROXY_TOKEN)}


@app.get("/fetch")
async def fetch(url: str = Query(...), x_proxy_token: str = Header(default="")):
    if not PROXY_TOKEN:
        raise HTTPException(503, "PROXY_TOKEN is not configured on the server")
    if not hmac.compare_digest(x_proxy_token, PROXY_TOKEN):
        raise HTTPException(401, "bad or missing X-Proxy-Token")

    for _ in range(MAX_REDIRECTS + 1):
        if not host_allowed(url):
            raise HTTPException(403, f"host not allowed: {urlsplit(url).hostname}")
        try:
            req = client.build_request("GET", url)
            upstream = await client.send(req, stream=True)
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"upstream error: {exc.__class__.__name__}: {exc}")
        if upstream.is_redirect and "location" in upstream.headers:
            await upstream.aclose()
            url = urljoin(url, upstream.headers["location"])
            continue
        break
    else:
        raise HTTPException(508, "too many redirects")

    body = bytearray()
    try:
        async for chunk in upstream.aiter_bytes():
            body += chunk
            if len(body) > MAX_BYTES:
                raise HTTPException(413, "upstream response too large")
    finally:
        await upstream.aclose()

    headers = {k: upstream.headers[k] for k in PASS_HEADERS if k in upstream.headers}
    headers["X-Final-Url"] = url
    headers["X-Upstream-Status"] = str(upstream.status_code)
    return Response(content=bytes(body), status_code=upstream.status_code, headers=headers)
