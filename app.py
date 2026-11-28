"""Allow-listed fetch proxy for the MSE graph scraper.

Deployed on Render so the scraper can reach mse.mn / opendatalab.mn from
networks that can't. It is NOT an open proxy:

* every request needs the shared secret in the `X-Proxy-Token` header;
* only hosts in ALLOWED_HOSTS (and their subdomains) can be fetched,
  re-checked on every redirect hop;
* GET only, http/https only, response size capped.

A redirect from or to a host in PASS_REDIRECT_HOSTS (default opendatalab.mn, which asks
for 10 s between requests) is not followed here: the 3xx goes back to the caller with an
absolute Location, and the caller (mse_graph.scraper.Fetcher) asks for the next hop after
its crawl delay. Sleeping 11 s inside the request instead would eat the caller's timeout.

Hosts in UPSTREAM_PROXY_HOSTS (default opendatalab.mn, which refuses Render's own
address, issue #4) always leave through the forward proxy in UPSTREAM_PROXY_URL
(e.g. a Webshare rotating endpoint) when it is set: never directly, not even on
error. /healthz shows the route without credentials.

    GET /fetch?url=https://mse.mn/mn/companies     (header X-Proxy-Token: ...)
    GET /healthz
"""
from __future__ import annotations

import hmac
import os
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Response

PROXY_TOKEN = os.environ.get("PROXY_TOKEN", "")
ALLOWED_HOSTS = [
    h.strip().lower()
    for h in os.environ.get("ALLOWED_HOSTS", "mse.mn,opendatalab.mn").split(",")
    if h.strip()
]
UPSTREAM_PROXY_URL = os.environ.get("UPSTREAM_PROXY_URL", "").strip()
UPSTREAM_PROXY_HOSTS = [
    h.strip().lower()
    for h in os.environ.get("UPSTREAM_PROXY_HOSTS", "opendatalab.mn").split(",")
    if h.strip()
]
PASS_REDIRECT_HOSTS = [
    h.strip().lower()
    for h in os.environ.get("PASS_REDIRECT_HOSTS", "opendatalab.mn").split(",")
    if h.strip()
]
MAX_BYTES = int(os.environ.get("MAX_BYTES", str(25 * 1024 * 1024)))
MAX_REDIRECTS = 5
PASS_HEADERS = ("content-type", "last-modified", "etag", "content-disposition")

app = FastAPI(title="mse-graph fetch proxy", docs_url=None, redoc_url=None)
_CLIENT_OPTS = dict(
    timeout=httpx.Timeout(60, connect=15),
    follow_redirects=False,
    headers={"User-Agent": "mse-graph/0.1 (research crawler)"},
)
client = httpx.AsyncClient(**_CLIENT_OPTS)
upstream_client = httpx.AsyncClient(proxy=UPSTREAM_PROXY_URL, **_CLIENT_OPTS) if UPSTREAM_PROXY_URL else None


def _redact(url: str) -> str:
    """scheme://host:port of a proxy URL: no user name, no password."""
    p = urlsplit(url)
    netloc = p.hostname or ""
    try:
        if p.port:
            netloc += f":{p.port}"
    except ValueError:
        pass
    return urlunsplit((p.scheme, netloc, "", "", ""))


def _scrub(text: str) -> str:
    """Remove the upstream proxy's credentials from an error message."""
    if UPSTREAM_PROXY_URL:
        p = urlsplit(UPSTREAM_PROXY_URL)
        text = text.replace(UPSTREAM_PROXY_URL, _redact(UPSTREAM_PROXY_URL))
        for secret in (p.password, p.username):
            if secret:
                text = text.replace(secret, "***")
    return text


def _matches(url: str, hosts: list[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return bool(host) and any(host == h or host.endswith("." + h) for h in hosts)


def host_allowed(url: str) -> bool:
    if urlsplit(url).scheme not in ("http", "https"):
        return False
    return _matches(url, ALLOWED_HOSTS)


def client_for(url: str) -> httpx.AsyncClient:
    """The upstream proxy for UPSTREAM_PROXY_HOSTS when one is configured, else direct."""
    if upstream_client is not None and _matches(url, UPSTREAM_PROXY_HOSTS):
        return upstream_client
    return client


@app.get("/healthz")
def healthz():
    return {
        "ok": True, "allowed_hosts": ALLOWED_HOSTS, "token_configured": bool(PROXY_TOKEN),
        "pass_redirect_hosts": PASS_REDIRECT_HOSTS,
        "upstream_proxy": ({"hosts": UPSTREAM_PROXY_HOSTS, "route": _redact(UPSTREAM_PROXY_URL)}
                           if UPSTREAM_PROXY_URL else None),
    }


@app.get("/fetch")
async def fetch(url: str = Query(...), x_proxy_token: str = Header(default="")):
    if not PROXY_TOKEN:
        raise HTTPException(503, "PROXY_TOKEN is not configured on the server")
    # bytes: compare_digest raises TypeError on str with non-ASCII, which was a 500 instead of a 401
    if not hmac.compare_digest(x_proxy_token.encode(), PROXY_TOKEN.encode()):
        raise HTTPException(401, "bad or missing X-Proxy-Token")

    for _ in range(MAX_REDIRECTS + 1):
        if not host_allowed(url):
            raise HTTPException(403, f"host not allowed: {urlsplit(url).hostname}")
        c = client_for(url)
        try:
            req = c.build_request("GET", url)
            upstream = await c.send(req, stream=True)
        except httpx.HTTPError as exc:
            raise HTTPException(502, _scrub(f"upstream error: {exc.__class__.__name__}: {exc}"))
        if upstream.is_redirect and "location" in upstream.headers:
            await upstream.aclose()
            target = urljoin(url, upstream.headers["location"])
            if not host_allowed(target):
                raise HTTPException(403, f"host not allowed: {urlsplit(target).hostname}")
            if _matches(url, PASS_REDIRECT_HOSTS) or _matches(target, PASS_REDIRECT_HOSTS):
                # the caller makes the next request, after the host's crawl delay
                return Response(status_code=upstream.status_code, headers={
                    "Location": target, "X-Final-Url": url, "X-Upstream-Status": str(upstream.status_code)})
            url = target
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
