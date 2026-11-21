"""Outbound (egress) routing: which way a request leaves the server, per target host.

opendatalab.mn refuses connections from the hosted deployment's address (issue #4:
Render's outbound IP got "connection refused" after the first probe traffic), so
requests to it can leave through a different egress than the server's own:

    ODL_PROXY_URL        comma-separated routes for opendatalab.mn (and its subdomains),
                         tried in order until one answers:
                           http://user:pass@host:port     HTTP forward proxy: Bright Data, Webshare,
                           https://user:pass@host:port    Oxylabs, a squid/tinyproxy box, ...
                           socks5://user:pass@host:port   SOCKS5 proxy (socks5h:// resolves DNS at the proxy)
                           https://host/fetch             fetch gateway: proxy/app.py or the Cloudflare
                                                          Worker in proxy/cloudflare/ (needs ODL_PROXY_TOKEN)
                           direct                         this server's own address
    ODL_PROXY_TOKEN      X-Proxy-Token for the fetch gateways in ODL_PROXY_URL
    ODL_PROXY_CA_BUNDLE  extra CA certificate trusted on proxy routes, for providers that
                         re-sign HTTPS (Bright Data): a PEM file path, or the PEM text itself
    ODL_PROXY_ATTEMPTS   tries per route before moving to the next one (default 2)

Every other host goes through MSE_PROXY_URL (the fetch proxy, proxy/app.py) when it
is set, and direct otherwise. Direct requests still honour the standard
HTTPS_PROXY / ALL_PROXY / NO_PROXY variables, which httpx reads.

Routing never changes *what* is fetched: the URL, the cache key, the `source`
stored on every record and the per-host crawl delay (11 s for opendatalab.mn,
retries included) are the same whichever route carries the request.
"""
from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from .config import settings

ODL_DOMAIN = "opendatalab.mn"
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")


class EgressConfigError(ValueError):
    """A route setting (ODL_PROXY_URL, ...) can't be used. Raised on first use."""


@dataclass(frozen=True, repr=False)
class Route:
    kind: str              # "direct" | "proxy" (forward proxy) | "gateway" (GET <url>?url=<target>)
    url: str = ""          # proxy URL, credentials included, or the gateway's /fetch endpoint
    token: str = ""        # X-Proxy-Token for a gateway
    attempts: int = 1
    ca: str = ""           # extra CA for a proxy route (PEM path or PEM text)

    @property
    def label(self) -> str:
        """Printable form: no password, no token."""
        if self.kind == "direct":
            return "direct"
        return f"{self.kind} {_redact(self.url)}"

    def __repr__(self) -> str:
        return f"Route({self.label}, attempts={self.attempts})"


DIRECT = Route("direct")


def _redact(url: str) -> str:
    p = urlsplit(url)
    if not p.netloc:            # not a URL, e.g. "user:pass@host:port" without a scheme
        return url.rsplit("@", 1)[-1]
    try:
        port = p.port
    except ValueError:          # e.g. a template's "PORT" left in: show it as written
        port = p.netloc.rsplit(":", 1)[-1]
    netloc = p.hostname or ""
    if ":" in netloc:
        netloc = f"[{netloc}]"  # IPv6
    if port:
        netloc += f":{port}"
    return urlunsplit((p.scheme, netloc, p.path, "", ""))


def parse_routes(spec: str, token: str = "", attempts: int = 2, ca: str = "",
                 var: str = "ODL_PROXY_URL") -> list[Route]:
    """Parse a comma-separated route list (see the module docstring)."""
    if attempts < 1:
        raise EgressConfigError(f"{var}: attempts per route must be at least 1 (got {attempts})")
    if ca:
        ca = ca.replace("\\n", "\n")       # PEM pasted into a one-line env var
        if not ca.lstrip().startswith("-----BEGIN") and not os.path.isfile(ca):
            raise EgressConfigError(f"ODL_PROXY_CA_BUNDLE: no such file: {ca}")
    routes = []
    for item in (s.strip() for s in spec.split(",")):
        if not item:
            continue
        if item.lower() == "direct":
            routes.append(Route("direct", attempts=attempts))
            continue
        p = urlsplit(item)
        scheme = p.scheme.lower()
        if scheme not in PROXY_SCHEMES or not p.hostname:
            raise EgressConfigError(
                f"{var}: {_redact(item)!r} is not a route; use http://, https://, socks5:// or socks5h:// "
                f"[user:pass@]host:port, a fetch gateway https://host/fetch, or 'direct'")
        try:
            p.port
        except ValueError:
            raise EgressConfigError(
                f"{var}: the port in {_redact(item)} is not a number; put the proxy's port there (e.g. :80)"
            ) from None
        path = p.path.rstrip("/")
        if path.endswith("/fetch"):
            if scheme not in ("http", "https"):
                raise EgressConfigError(f"{var}: a fetch gateway must be http(s): {_redact(item)}")
            if not token:
                raise EgressConfigError(f"{var}: the fetch gateway {_redact(item)} needs ODL_PROXY_TOKEN")
            routes.append(Route("gateway", urlunsplit((scheme, p.netloc, path, "", "")), token, attempts))
        elif path or p.query:
            raise EgressConfigError(
                f"{var}: a proxy URL has no path ({_redact(item)}); a fetch gateway's URL ends in /fetch")
        else:
            routes.append(Route("proxy", item, attempts=attempts, ca=ca))
    return routes


def ssl_context(ca: str) -> ssl.SSLContext:
    """The default trust store plus `ca` (a PEM file path, or PEM text)."""
    import httpx

    ctx = httpx.create_ssl_context()
    ca = ca.replace("\\n", "\n")
    try:
        if ca.lstrip().startswith("-----BEGIN"):
            ctx.load_verify_locations(cadata=ca)
        else:
            ctx.load_verify_locations(cafile=ca)
    except (OSError, ssl.SSLError) as exc:
        raise EgressConfigError(f"ODL_PROXY_CA_BUNDLE is not a usable PEM certificate: {exc}") from exc
    return ctx


def host_matches(host: str, domain: str) -> bool:
    host = host.lower().rstrip(".")
    return host == domain or host.endswith("." + domain)


class Router:
    """Routes per target domain (first match wins), else the default routes.

    A domain's routes may be given as a zero-argument function: they're then parsed
    on the first request to that domain, so a bad ODL_PROXY_URL fails the
    Open Data Lab import (with its message) without touching the mse.mn scrape.
    """

    def __init__(self, default: list[Route] | None = None,
                 domains: dict[str, list[Route] | Callable[[], list[Route]]] | None = None):
        self.default = default or [DIRECT]
        self._domains = dict(domains or {})

    def routes_for(self, url_or_host: str) -> list[Route]:
        host = urlsplit(url_or_host).hostname if "://" in url_or_host else url_or_host
        for domain, routes in self._domains.items():
            if host_matches(host or "", domain):
                if callable(routes):
                    routes = routes()
                    if not routes:
                        raise EgressConfigError(f"no usable route for {domain}")
                    self._domains[domain] = routes
                return routes
        return self.default

    def describe(self, url_or_host: str) -> str:
        return ", ".join(r.label for r in self.routes_for(url_or_host))

    def is_custom(self, url_or_host: str) -> bool:
        """True if the host has its own routes rather than the default ones."""
        return self.routes_for(url_or_host) is not self.default

    @classmethod
    def from_env(cls, proxy_url: str | None = None, proxy_token: str | None = None) -> "Router":
        base = (settings.proxy_url if proxy_url is None else proxy_url).rstrip("/")
        token = settings.proxy_token if proxy_token is None else proxy_token
        default = [Route("gateway", base + "/fetch", token)] if base else [DIRECT]
        domains: dict = {}
        if settings.odl_proxy_url.strip():
            domains[ODL_DOMAIN] = lambda: parse_routes(
                settings.odl_proxy_url, settings.odl_proxy_token, settings.odl_proxy_attempts,
                settings.odl_proxy_ca_bundle)
        return cls(default, domains)
