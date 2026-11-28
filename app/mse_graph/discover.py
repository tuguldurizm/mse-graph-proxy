"""Site discovery: log how mse.mn / opendatalab.mn are built so parsers can target them.

Logs, for each site: robots.txt and sitemap URLs, internal links, framework
markers (Next.js / Nuxt), API-like strings found in the page and its JS
bundles, and the responses of a set of likely JSON endpoints.

    DISCOVER=1 on the hosted service runs this before the pipeline.
    python -m mse_graph.discover [https://site ...]
"""
from __future__ import annotations

import logging
import re
import sys
from collections import Counter
from urllib.parse import urljoin, urlsplit

log = logging.getLogger("discover")

SITES = ["https://mse.mn", "https://opendatalab.mn"]
PROBE_PATHS = [
    "/api", "/api/companies", "/api/company", "/api/securities", "/api/security", "/api/listed",
    "/api/v1/companies", "/api/v1/securities", "/api/v2/companies", "/api/issuers", "/api/stocks",
    "/api/datasets", "/api/v1/datasets", "/api/search", "/graphql", "/sitemap.xml", "/sitemap_index.xml",
    "/mn/company", "/en/company", "/company", "/companies", "/securities", "/listed-companies",
    "/live-market", "/mn/live-market", "/datasets", "/dataset", "/data", "/en", "/mn",
]
INTERESTING = re.compile(
    r"compan|securit|issuer|stock|symbol|ticker|sharehold|owner|board|director|executive|listed|"
    r"dataset|ownership|beneficial|registry|api|graphql|json|csv|xlsx|download", re.I)
URLISH = re.compile(r"""["'`]((?:https?://[^"'`\s]{4,200})|(?:/[A-Za-z0-9_\-./{}$:?=&%]{2,160}))["'`]""")


def _client():
    # Deliberately not routed through ODL_PROXY_URL (egress.py): discovery probes paths other
    # than /search/<reg no>, the kind of traffic that got the server's address refused (issue #4).
    # Redirects are followed by _get, one throttled hop at a time.
    import httpx
    return httpx.Client(timeout=30, follow_redirects=False,
                        headers={"User-Agent": "Mozilla/5.0 (compatible; mse-graph/0.1)"})


# Seconds between two requests to one domain (all its hosts share the clock, as in
# scraper.Fetcher); other sites get DEFAULT_DELAY.
HOST_DELAY = {"opendatalab.mn": 11.0}
DEFAULT_DELAY = 1.0
MAX_REDIRECTS = 5
_last_hit: dict[str, float] = {}


def _delay_key(host: str) -> str:
    from .egress import host_matches
    return next((d for d in HOST_DELAY if host_matches(host, d)), host.lower().rstrip("."))


def _wait_turn(url: str) -> None:
    import time
    key = _delay_key(urlsplit(url).hostname or "")
    wait = HOST_DELAY.get(key, DEFAULT_DELAY) - (time.monotonic() - _last_hit.get(key, float("-inf")))
    if wait > 0:
        time.sleep(wait)
    _last_hit[key] = time.monotonic()


def _get(c, url):
    """GET `url` after its domain's delay; each redirect hop waits its turn too."""
    try:
        for _ in range(MAX_REDIRECTS + 1):
            _wait_turn(url)
            r = c.get(url)
            if not r.is_redirect:
                return r
            url = urljoin(str(r.url), r.headers["location"])
        log.info("DISC GET %s: more than %d redirects", url, MAX_REDIRECTS)
        return None
    except Exception as exc:
        log.info("DISC GET %s failed: %s", url, exc)
        return None


def _interesting_strings(text: str, base_host: str) -> list[str]:
    out = []
    for s in URLISH.findall(text):
        if s.startswith("/") and (s.startswith("//") or len(s) < 4):
            continue
        if re.search(r"\.(png|jpe?g|svg|gif|webp|ico|woff2?|ttf|css|mp4)(\?|$)", s, re.I):
            continue
        if "${" in s:
            continue
        try:
            host = urlsplit(s).hostname if s.startswith("http") else base_host
        except ValueError:
            continue
        if s.startswith("http") and host and not INTERESTING.search(s) and base_host not in host:
            continue
        if INTERESTING.search(s) or "/api" in s:
            out.append(s)
    return out


def discover_site(base: str, max_scripts: int = 40) -> None:
    from bs4 import BeautifulSoup

    c = _client()
    host = urlsplit(base).hostname or ""
    log.info("DISC ===== %s =====", base)

    r = _get(c, base + "/robots.txt")
    if r is not None and r.status_code == 200:
        log.info("DISC robots.txt: %s", r.text[:800].replace("\n", " | "))
        for sm in re.findall(r"(?im)^sitemap:\s*(\S+)", r.text):
            _sitemap(c, sm)

    r = _get(c, base + "/")
    if r is None:
        return
    log.info("DISC home %s -> %s %s (%d bytes, final %s)", base, r.status_code,
             r.headers.get("content-type"), len(r.content), r.url)
    html = r.text
    soup = BeautifulSoup(html, "lxml")
    markers = [m for m in ("__NEXT_DATA__", "__NUXT__", "_next/static", "_nuxt/", "ng-version",
                           "data-reactroot", "wp-content", "vite", "svelte") if m in html]
    log.info("DISC framework markers: %s", markers)
    nd = soup.find("script", id="__NEXT_DATA__")
    if nd:
        log.info("DISC __NEXT_DATA__ (first 1500): %s", nd.get_text()[:1500])

    links = sorted({urljoin(base + "/", a["href"]).split("#")[0] for a in soup.find_all("a", href=True)})
    internal = [l for l in links if host in (urlsplit(l).hostname or "")]
    log.info("DISC internal links (%d): %s", len(internal), " | ".join(internal[:120]))

    found: Counter = Counter(_interesting_strings(html, host))
    scripts = [urljoin(base + "/", s["src"]) for s in soup.find_all("script", src=True)]
    log.info("DISC scripts (%d): %s", len(scripts), " | ".join(scripts[:60]))
    for src in scripts[:max_scripts]:
        sh = urlsplit(src).hostname or ""
        if host not in sh:
            continue
        rs = _get(c, src)
        if rs is not None and rs.status_code == 200 and len(rs.content) < 6_000_000:
            found.update(_interesting_strings(rs.text, host))
    log.info("DISC api-like strings (%d):", len(found))
    for s, n in sorted(found.items(), key=lambda kv: (-kv[1], kv[0]))[:250]:
        log.info("DISC   %s", s)

    for path in PROBE_PATHS:
        r = _get(c, base + path)
        if r is None:
            continue
        ctype = r.headers.get("content-type", "")
        snippet = re.sub(r"\s+", " ", r.text[:300]) if ("json" in ctype or "xml" in ctype) else ""
        log.info("DISC probe %-24s %s %-28s %6d  %s", path, r.status_code, ctype[:28], len(r.content), snippet)
        if r.status_code == 200 and "html" in ctype and path not in ("/en", "/mn"):
            s2 = BeautifulSoup(r.text, "lxml")
            sub = sorted({urljoin(str(r.url), a["href"]).split("#")[0] for a in s2.find_all("a", href=True)})
            sub = [l for l in sub if host in (urlsplit(l).hostname or "") and l not in internal]
            if sub:
                log.info("DISC   links on %s (%d): %s", path, len(sub), " | ".join(sub[:80]))


def _sitemap(c, url: str, depth: int = 0) -> None:
    r = _get(c, url)
    if r is None or r.status_code != 200:
        return
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)
    patterns = Counter(re.sub(r"/[^/]*\d[^/]*(?=/|$)", "/{id}", urlsplit(u).path) for u in locs)
    log.info("DISC sitemap %s: %d urls; patterns: %s", url, len(locs), patterns.most_common(25))
    log.info("DISC sitemap sample: %s", " | ".join(locs[:30]))
    if depth == 0:
        for sub in [u for u in locs if u.endswith(".xml")][:10]:
            _sitemap(c, sub, 1)


def run(sites: list[str] | None = None) -> None:
    for s in sites or SITES:
        try:
            discover_site(s.rstrip("/"))
        except Exception as exc:  # never break the pipeline
            log.exception("DISC %s failed: %s", s, exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run(sys.argv[1:] or None)


# ---- pass 2: explore specific pages --------------------------------------------

def _rsc_snippets(html: str, limit: int = 6) -> list[str]:
    """Next.js app-router flight data (self.__next_f.push) often embeds the page data."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S)
    text = " ".join(chunks).encode("utf-8", "ignore").decode("unicode_escape", "ignore")
    text = text.encode("latin-1", "ignore").decode("utf-8", "ignore")
    hits = []
    for m in re.finditer(r'\{[^{}]{0,40}"(?:symbol|ticker|companyName|name|register|regNo|shareholder|percent)"[^{}]{20,600}\}', text):
        hits.append(m.group(0)[:600])
        if len(hits) >= limit:
            break
    return hits


def explore(url: str, seen_scripts: set[str]) -> None:
    from bs4 import BeautifulSoup

    c = _client()
    host = urlsplit(url).hostname or ""
    r = _get(c, url)
    if r is None:
        return
    ctype = r.headers.get("content-type", "")
    log.info("DISC2 %s -> %s %s %d bytes final=%s", url, r.status_code, ctype[:30], len(r.content), r.url)
    if "json" in ctype:
        log.info("DISC2   json: %s", re.sub(r"\s+", " ", r.text[:1500]))
        try:
            data = r.json()
        except ValueError:
            return
        items = data if isinstance(data, list) else next(
            (v for v in (data.values() if isinstance(data, dict) else []) if isinstance(v, list)), [])
        log.info("DISC2   json items=%d keys=%s", len(items), sorted(items[0].keys()) if items and isinstance(items[0], dict) else None)
        for it in items[:2]:
            log.info("DISC2   item: %s", str(it)[:1500])
        FOLLOW.extend(_detail_urls(items[:2]))
        return
    if r.status_code != 200 or "html" not in ctype:
        return
    html = r.text
    soup = BeautifulSoup(html, "lxml")
    links = sorted({urljoin(str(r.url), a["href"]).split("#")[0] for a in soup.find_all("a", href=True)})
    links = [l for l in links if host in (urlsplit(l).hostname or "")]
    log.info("DISC2   links (%d): %s", len(links), " | ".join(links[:100]))
    for f in soup.find_all("form")[:5]:
        fields = [(i.get("name"), i.get("type")) for i in f.find_all(["input", "select"])]
        log.info("DISC2   form action=%r method=%r fields=%s", f.get("action"), f.get("method"), fields[:15])
    for i, t in enumerate(soup.find_all("table")[:6]):
        rows = [[clean_text(c.get_text(" "))[:40] for c in tr.find_all(["th", "td"])] for tr in t.find_all("tr")[:4]]
        log.info("DISC2   table %d (%d rows): %s", i, len(t.find_all("tr")), rows)
    heads = [clean_text(h.get_text(" "))[:60] for h in soup.find_all(["h1", "h2", "h3", "h4", "h5"])][:30]
    log.info("DISC2   headings: %s", " | ".join(heads))
    body = clean_text(soup.get_text(" "))
    log.info("DISC2   text (first 1200): %s", body[:1200])
    for s in _rsc_snippets(html):
        log.info("DISC2   rsc: %s", s)
    found: Counter = Counter(_interesting_strings(html, host))
    for s in soup.find_all("script", src=True):
        src = urljoin(str(r.url), s["src"])
        try:
            src_host = urlsplit(src).hostname or ""
        except ValueError:
            continue
        if src in seen_scripts or host not in src_host:
            continue
        seen_scripts.add(src)
        rs = _get(c, src)
        if rs is not None and rs.status_code == 200:
            found.update(_interesting_strings(rs.text, host))
    if found:
        log.info("DISC2   api-like strings: %s", sorted(found)[:150])


def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


EXPLORE_DEFAULT = [
    "https://mse.mn/companies-info", "https://mse.mn/stock-list", "https://mse.mn/shareholders",
    "https://mse.mn/company-governance", "https://mse.mn/live-market",
    "https://mse.mn/api/public/securities", "https://mse.mn/api/public/companies",
    "https://mse.mn/api/public/stocks", "https://mse.mn/api/public/stock-list",
    "https://mse.mn/api/public/companies-info", "https://mse.mn/api/public/shareholders",
    "https://mse.mn/api/public/settings",
    "https://opendatalab.mn/legalentity", "https://opendatalab.mn/search?query=2007029",
    "https://opendatalab.mn/declarationform",
]


FOLLOW: list[str] = []


def _detail_urls(items: list) -> list[str]:
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        for k, v in it.items():
            if v and re.search(r"symbol|ticker|code|^id$|companyid|company_id|secid", k, re.I) and len(str(v)) < 20:
                out.append(f"https://mse.mn/securities/{v}")
    return out[:4]


def run_explore(urls: list[str] | None = None) -> None:
    seen: set[str] = set()
    queue = list(urls or EXPLORE_DEFAULT)
    done: set[str] = set()
    while queue:
        u = queue.pop(0)
        if u in done:
            continue
        done.add(u)
        try:
            explore(u, seen)
        except Exception as exc:
            log.exception("DISC2 %s failed: %s", u, exc)
        while FOLLOW:
            queue.append(FOLLOW.pop(0))
