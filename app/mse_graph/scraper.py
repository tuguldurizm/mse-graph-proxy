"""Scraper for mse.mn listed-company profiles.

mse.mn's markup changes over time and was not reachable while this was
written, so parsing is label-driven rather than tied to CSS selectors:

* key/value facts (symbol, sector, registration no., address, ...) are read
  from 2-column tables, <dl> lists and "Label: value" text;
* tables are classified as *shareholders* or *management* from their header
  cells and the nearest preceding heading/caption, in Mongolian or English;
* columns are located by header keywords (name / % / shares / position).

If the live site needs different URLs, set MSE_COMPANY_LIST_PATH and
MSE_COMPANY_LINK_REGEX, or save pages from a browser and use
`python -m mse_graph.cli parse-html <dir>`.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from . import egress
from .config import settings
from .models import Company, Dataset, Position, Shareholding
from .normalize import clean, is_aggregate, parse_int, parse_percent

log = logging.getLogger(__name__)

COMPANY_LIST_PATHS = [
    p for p in os.getenv("MSE_COMPANY_LIST_PATH", "").split(",") if p
] or [
    "/{lang}/companies", "/{lang}/company", "/{lang}/listed-companies",
    "/{lang}/company_list", "/{lang}/market/companies",
]
COMPANY_LINK_RE = re.compile(
    os.getenv("MSE_COMPANY_LINK_REGEX", r"/(?:mn|en)/(?:company|companies|listed[-_]companies)/([\w-]+)/?$"),
    re.IGNORECASE,
)

# ---- label vocabularies -----------------------------------------------------

FACT_LABELS = {
    "ticker": ["симбол", "symbol", "ticker", "код", "тэмдэглэгээ"],
    "name": ["компанийн нэр", "company name", "нэр"],
    "name_en": ["англи нэр", "english name", "name in english"],
    "sector": ["салбар", "sector", "industry", "үйл ажиллагааны чиглэл"],
    "reg_no": ["регистр", "registration", "улсын бүртгэл", "reg no", "register"],
    "address": ["хаяг", "address"],
    "website": ["вэб", "веб", "website", "web"],
}

SHAREHOLDER_WORDS = ["хувьцаа эзэмшигч", "эзэмшигч", "shareholder", "owner", "ownership", "эзэмшил"]
MANAGEMENT_WORDS = [
    "удирдлага", "төлөөлөн удирдах", "туз", "гүйцэтгэх", "захирал", "албан тушаал",
    "management", "board", "director", "executive", "officer", "position",
]
BOARD_WORDS = ["туз", "төлөөлөн удирдах", "board", "director", "гишүүн", "дарга", "chairman", "chair"]
EXEC_TITLE_WORDS = ["гүйцэтгэх захирал", "ерөнхий захирал", "ceo", "cfo", "coo", "менежер",
                    "нягтлан", "executive", "officer", "дэд захирал"]

COL_NAME = ["нэр", "name", "эзэмшигч", "shareholder", "овог", "гишүүн", "member", "holder"]
COL_PCT = ["хувь", "%", "percent", "ownership", "эзлэх"]
COL_SHARES = ["тоо", "ширхэг", "number", "shares", "хувьцааны тоо", "quantity"]
COL_TITLE = ["албан тушаал", "position", "title", "role", "тушаал"]


def _has(text: str, words: list[str]) -> bool:
    t = text.lower()
    return any(w in t for w in words)


# ---- HTTP -------------------------------------------------------------------

class Attempt(NamedTuple):
    response: object | None   # httpx.Response when step == "ok"
    status: int | None        # the target site's HTTP status, None if it never answered
    error: str
    step: str                 # "ok" | "retry" (same route again) | "next" (next route) | "stop" (final answer)
    seconds: float = 0.0      # time the request took (the crawl-delay wait not included)


class Fetcher:
    """Throttled, disk-cached HTTP GET with per-host egress routes (see egress.py).

    Requests to opendatalab.mn leave through ODL_PROXY_URL when it is set. Other
    requests go through the allow-listed fetch proxy in `proxy/app.py` when
    MSE_PROXY_URL (and MSE_PROXY_TOKEN) are set, and directly otherwise. A route
    that fails is tried again (ODL_PROXY_ATTEMPTS times), then the next route;
    every try waits out the host's crawl delay.
    """

    # Statuses worth another try: a block or rate limit, a timeout, a proxy or upstream failure.
    RETRY_STATUS = {403, 408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}

    def __init__(self, cache_dir: str | Path = "data/raw", delay: float | None = None,
                 proxy_url: str | None = None, proxy_token: str | None = None,
                 cache_max_age: float | None = None, router: egress.Router | None = None):
        import httpx  # local import so offline parsing works without httpx

        self.cache_dir = Path(cache_dir)
        # Cached pages older than this (seconds) are fetched again. Without a limit
        # every later scrape would replay the first one and no change (a new CEO, a
        # sold stake) would ever be recorded. The default (20 h) lets a re-run on the
        # same day reuse the pages while a daily scrape always sees fresh ones.
        self.cache_max_age = (cache_max_age if cache_max_age is not None
                              else float(os.getenv("MSE_CACHE_MAX_AGE_HOURS", "20")) * 3600)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.delay = settings.request_delay if delay is None else delay
        self.proxy_url = (proxy_url if proxy_url is not None else settings.proxy_url).rstrip("/")
        self.proxy_token = proxy_token if proxy_token is not None else settings.proxy_token
        self.router = router or egress.Router.from_env(self.proxy_url, self.proxy_token)
        self.client = self._make_client()
        self._proxy_clients: dict[str, httpx.Client] = {}
        self._last_by_host: dict[str, float] = {}
        self._hinted = False
        # Outcome of the last request that missed the cache: the site's HTTP status
        # (None if it never answered) and the last error seen.
        self.last_status: int | None = None
        self.last_error = ""

    # robots.txt crawl-delay of sites that ask for one
    HOST_DELAY = {"opendatalab.mn": 11.0, "www.opendatalab.mn": 11.0}

    @staticmethod
    def _make_client(**kw):
        import httpx
        return httpx.Client(
            # generous timeout: a free Render instance can take ~1 min to wake up
            timeout=httpx.Timeout(90, connect=30), follow_redirects=True,
            headers={"User-Agent": "mse-graph/0.1 (research crawler)"}, **kw,
        )

    def _proxy_client(self, route: egress.Route):
        client = self._proxy_clients.get(route.url)
        if client is None:
            verify = egress.ssl_context(route.ca) if route.ca else True
            client = self._proxy_clients[route.url] = self._make_client(proxy=route.url, verify=verify)
        return client

    def _throttle(self, host: str) -> None:
        delay = max(self.delay, self.HOST_DELAY.get(host, 0.0))
        wait = delay - (time.monotonic() - self._last_by_host.get(host, float("-inf")))
        if wait > 0:
            time.sleep(wait)
        self._last_by_host[host] = time.monotonic()

    def try_route(self, route: egress.Route, url: str) -> Attempt:
        """One GET of `url` through `route`, after the host's crawl delay."""
        client = self._proxy_client(route) if route.kind == "proxy" else self.client
        self._throttle(urlsplit(url).hostname or "")
        start = time.monotonic()
        a = self._send(client, route, url)
        return a._replace(seconds=round(time.monotonic() - start, 2))

    def _send(self, client, route: egress.Route, url: str) -> Attempt:
        try:
            if route.kind == "gateway":
                r = client.get(route.url, params={"url": url}, headers={"X-Proxy-Token": route.token})
            else:
                r = client.get(url)
        except Exception as exc:  # network errors are logged, not fatal
            import httpx

            error = f"{type(exc).__name__}: {exc}"
            if route.kind == "proxy" and "CERTIFICATE_VERIFY_FAILED" in error:
                return Attempt(None, None, error + " (does the proxy re-sign HTTPS? set ODL_PROXY_CA_BUNDLE)",
                               "next")
            if isinstance(exc, httpx.ProxyError) and str(exc).startswith("407"):
                return Attempt(None, None, error + " (check the proxy's username and password)", "next")
            return Attempt(None, None, error, "retry")
        if route.kind == "gateway" and r.status_code != 200 and "x-upstream-status" not in r.headers:
            # the gateway itself refused (token, allow-list, not configured) or is waking up / failing
            step = "retry" if r.status_code >= 500 else "next"
            return Attempt(None, None, f"gateway HTTP {r.status_code}: {r.text[:200]}", step)
        if r.status_code == 200:
            return Attempt(r, 200, "", "ok")
        if r.status_code == 407:
            return Attempt(None, None, "HTTP 407 (check the proxy's username and password)", "next")
        step = "retry" if r.status_code in self.RETRY_STATUS else "stop"
        return Attempt(None, r.status_code, f"HTTP {r.status_code}", step)

    def _request(self, url: str):
        host = urlsplit(url).hostname or ""
        routes = self.router.routes_for(host)          # a bad ODL_PROXY_URL raises here
        self.last_status, self.last_error = None, ""
        for route in routes:
            for n in range(1, route.attempts + 1):
                a = self.try_route(route, url)
                if a.step == "ok":
                    self.last_status = 200
                    return a.response
                self.last_status, self.last_error = a.status, a.error
                via = f" via {route.label}" if route is not egress.DIRECT else ""
                tries = f" (try {n}/{route.attempts})" if route.attempts > 1 else ""
                log.warning("GET %s%s%s -> %s", url, via, tries, a.error)
                if a.step == "stop":
                    return None
                if a.step == "next":
                    break
        if (self.last_status is None and not self._hinted and egress.host_matches(host, egress.ODL_DOMAIN)
                and not self.router.is_custom(host)):
            self._hinted = True
            log.warning("opendatalab.mn did not answer. It refuses connections from some hosting providers' "
                        "addresses (issue #4); set ODL_PROXY_URL to reach it through a proxy "
                        "(README: 'Egress for Open Data Lab').")
        return None

    def _cache_path(self, url: str, ext: str) -> Path:
        return self.cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ext)

    def _fresh(self, path: Path) -> bool:
        try:
            return time.time() - path.stat().st_mtime < self.cache_max_age
        except OSError:
            return False

    def get(self, url: str, use_cache: bool = True) -> str | None:
        path = self._cache_path(url, ".html")
        if use_cache and self._fresh(path):
            return path.read_text(encoding="utf-8")
        r = self._request(url)
        if r is None:
            return None
        path.write_text(r.text, encoding="utf-8")
        return r.text

    def get_bytes(self, url: str, use_cache: bool = True) -> bytes | None:
        path = self._cache_path(url, ".bin")
        if use_cache and self._fresh(path):
            return path.read_bytes()
        r = self._request(url)
        if r is None:
            return None
        path.write_bytes(r.content)
        return r.content


# ---- parsing ----------------------------------------------------------------

def find_company_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        url = urljoin(base_url, a["href"]).split("#")[0].split("?")[0]
        if COMPANY_LINK_RE.search(url) and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _context_text(table: Tag) -> str:
    """Caption plus the nearest preceding heading-ish element of a table."""
    parts = []
    if table.caption:
        parts.append(table.caption.get_text(" "))
    prev = table.find_previous(["h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "legend"])
    if prev:
        parts.append(prev.get_text(" "))
    # tab panes: <div id="shareholders"> ... <table>
    for parent in table.parents:
        if isinstance(parent, Tag) and (parent.get("id") or parent.get("class")):
            parts.append(" ".join([parent.get("id") or "", *(parent.get("class") or [])]))
            break
    return clean(" ".join(parts))


def _table_rows(table: Tag) -> tuple[list[str], list[list[str]]]:
    rows = [
        [clean(c.get_text(" ")) for c in tr.find_all(["th", "td"])]
        for tr in table.find_all("tr")
    ]
    rows = [r for r in rows if any(r)]
    if not rows:
        return [], []
    header_tr = table.find("tr")
    if table.find("thead") or (header_tr and header_tr.find("th") and not header_tr.find("td")):
        return [h.lower() for h in rows[0]], rows[1:]
    return [], rows


def _col(header: list[str], words: list[str], exclude: set[int] = frozenset()) -> int | None:
    for i, h in enumerate(header):
        if i not in exclude and _has(h, words):
            return i
    return None


def _extract_facts(soup: BeautifulSoup) -> dict[str, str]:
    pairs: list[tuple[str, str]] = []
    for tr in soup.find_all("tr"):
        table = tr.find_parent("table")
        if table is not None and table.find("thead"):
            continue  # a data table (shareholders/management), not a facts table
        cells = tr.find_all(["th", "td"])
        if len(cells) == 2 and tr.find("td"):
            pairs.append((clean(cells[0].get_text(" ")), clean(cells[1].get_text(" "))))
    for dl in soup.find_all("dl"):
        for dt in dl.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            if dd:
                pairs.append((clean(dt.get_text(" ")), clean(dd.get_text(" "))))
    for el in soup.find_all(["li", "p", "div", "span"]):
        if el.find(["li", "p", "div", "table"]):
            continue
        m = re.match(r"^([^:：]{2,40})[:：]\s*(.+)$", clean(el.get_text(" ")))
        if m:
            pairs.append((m.group(1), m.group(2)))

    facts: dict[str, str] = {}
    for label, value in pairs:
        lab = label.lower().strip(" :")
        if not value or len(lab) > 40:
            continue
        for key, words in FACT_LABELS.items():
            if key not in facts and any(lab == w or lab.startswith(w) or w in lab for w in words):
                # "нэр" is too generic: only accept it as an exact label
                if key == "name" and lab not in ("нэр", "компанийн нэр", "company name"):
                    continue
                facts[key] = value
                break
    return facts


def parse_company_page(html: str, url: str = "", ticker_hint: str = "") -> Dataset:
    soup = BeautifulSoup(html, "lxml")
    facts = _extract_facts(soup)

    name = facts.get("name")
    if not name:
        h = soup.find(["h1", "h2"])
        name = clean(h.get_text(" ")) if h else clean(soup.title.get_text() if soup.title else "")
    raw_ticker = facts.get("ticker") or ticker_hint
    ticker = raw_ticker.split()[0].upper() if raw_ticker else ""
    mse_id = ""
    if url and (m := COMPANY_LINK_RE.search(url)):
        mse_id = m.group(1)
    if not ticker:
        ticker = mse_id.upper() or re.sub(r"\W+", "_", name).upper()[:20]

    company = Company(
        ticker=ticker, name=name, name_en=facts.get("name_en", ""), mse_id=mse_id,
        sector=facts.get("sector", ""), reg_no=facts.get("reg_no", ""),
        address=facts.get("address", ""), website=facts.get("website", ""), url=url,
    )
    ds = Dataset(companies=[company])
    source = url or "html"

    for table in soup.find_all("table"):
        header, rows = _table_rows(table)
        if not rows:
            continue
        ctx = _context_text(table)
        htext = " ".join(header)
        is_sh = _has(htext + " " + ctx, SHAREHOLDER_WORDS) or (
            header and _col(header, COL_PCT) is not None and _col(header, COL_TITLE) is None)
        is_mg = _has(htext + " " + ctx, MANAGEMENT_WORDS) and _col(header, COL_PCT) is None
        if is_sh and not (is_mg and _col(header, COL_TITLE) is not None):
            ds.shareholdings += _parse_shareholders(header, rows, ticker, source)
        elif is_mg:
            ds.positions += _parse_management(header, rows, ticker, source, ctx)
    return ds


def _parse_shareholders(header, rows, ticker, source) -> list[Shareholding]:
    if header:
        c_pct = _col(header, COL_PCT)
        c_sh = _col(header, COL_SHARES, exclude={c_pct} if c_pct is not None else set())
        c_name = _col(header, COL_NAME, exclude={i for i in (c_pct, c_sh) if i is not None})
    else:
        c_name = c_pct = c_sh = None
    out = []
    for r in rows:
        if c_name is None:
            # headerless: first non-numeric cell is the name, a cell with % or <=100 is pct
            texts = [(i, v) for i, v in enumerate(r) if v and not re.fullmatch(r"[\d.,%\s]+", v)]
            if not texts:
                continue
            name = texts[0][1]
            nums = [v for v in r if re.fullmatch(r"[\d.,%\s]+", v or "")]
            pct = next((parse_percent(v) for v in nums if "%" in v), None)
            if pct is None:
                pct = next((parse_percent(v) for v in reversed(nums)
                            if (p := parse_percent(v)) is not None and p <= 100 and "." in v.replace(",", ".")), None)
            shares = next((parse_int(v) for v in nums if (parse_int(v) or 0) > 100), None)
        else:
            name = r[c_name] if c_name < len(r) else ""
            pct = parse_percent(r[c_pct]) if c_pct is not None and c_pct < len(r) else None
            shares = parse_int(r[c_sh]) if c_sh is not None and c_sh < len(r) else None
        if not name or is_aggregate(name) or re.fullmatch(r"[\d.\s]+", name):
            continue
        out.append(Shareholding(ticker, name, pct, shares, source=source))
    return out


def _parse_management(header, rows, ticker, source, ctx) -> list[Position]:
    c_title = _col(header, COL_TITLE) if header else None
    c_name = _col(header, COL_NAME, exclude={c_title} if c_title is not None else set()) if header else None
    board_ctx = _has(ctx, BOARD_WORDS) and not _has(ctx, ["гүйцэтгэх удирдлага", "executive management"])
    out = []
    for r in rows:
        cells = [v for v in r if v and not re.fullmatch(r"\d+\.?", v)]
        if c_name is not None and c_name < len(r):
            name = r[c_name]
            title = r[c_title] if c_title is not None and c_title < len(r) else ""
        elif len(cells) >= 2:
            # guess: the cell that looks like a job title is the title
            if _has(cells[0], BOARD_WORDS + EXEC_TITLE_WORDS):
                title, name = cells[0], cells[1]
            else:
                name, title = cells[0], cells[1]
        elif cells:
            name, title = cells[0], ""
        else:
            continue
        if not name:
            continue
        if _has(title, EXEC_TITLE_WORDS) and not _has(title, ["туз", "board"]):
            role = "executive"
        elif _has(title, BOARD_WORDS) or board_ctx:
            role = "board"
        else:
            role = "executive"
        out.append(Position(ticker, name, title, role, source=source))
    return out


# ---- crawl ------------------------------------------------------------------

def describe_page(html: str, url: str, brief: bool = False) -> None:
    """Log a compact outline of a page (links, headings, tables) to debug parsing remotely."""
    soup = BeautifulSoup(html, "lxml")
    title = clean(soup.title.get_text()) if soup.title else ""
    log.info("PAGE %s title=%r size=%d tables=%d scripts=%d", url, title[:80], len(html),
             len(soup.find_all("table")), len(soup.find_all("script")))
    if brief:
        return
    hrefs = []
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if h not in hrefs and not h.startswith(("#", "javascript", "mailto", "tel")):
            hrefs.append(h)
    log.info("PAGE links (%d): %s", len(hrefs), " | ".join(hrefs[:60]))
    heads = [clean(h.get_text(" "))[:60] for h in soup.find_all(["h1", "h2", "h3", "h4"])][:25]
    log.info("PAGE headings: %s", " | ".join(heads))
    for i, t in enumerate(soup.find_all("table")[:8]):
        rows = [[clean(c.get_text(" "))[:30] for c in tr.find_all(["th", "td"])] for tr in t.find_all("tr")[:3]]
        log.info("PAGE table %d (%d rows, ctx=%r): %s", i, len(t.find_all("tr")), _context_text(t)[:80], rows)
    api_hints = sorted({m for m in re.findall(r"""["'](/[\w/.-]*api[\w/.?=&-]*)["']""", html)})[:20]
    if api_hints:
        log.info("PAGE api-like paths: %s", api_hints)


def crawl(limit: int | None = None, fetcher: Fetcher | None = None) -> Dataset:
    fetcher = fetcher or Fetcher()
    base = settings.mse_base_url.rstrip("/")
    links: list[str] = []
    # the home page first: its links reveal the real company-list URL if ours are wrong
    for path in ["/", "/{lang}"] + COMPANY_LIST_PATHS:
        url = base + path.format(lang=settings.mse_lang)
        html = fetcher.get(url)
        if html:
            links = find_company_links(html, url)
            log.info("%s -> %d company links", url, len(links))
            if not links:
                describe_page(html, url)
            if len(links) >= 5:   # the home page may link only a few companies
                break
    if not links:
        raise RuntimeError(
            "No company links found on mse.mn. Check network access to mse.mn, or set "
            "MSE_COMPANY_LIST_PATH / MSE_COMPANY_LINK_REGEX to match the current site."
        )
    ds = Dataset(observed_at=date.today().isoformat())
    for i, link in enumerate(links[:limit] if limit else links):
        html = fetcher.get(link)
        if not html:
            continue
        page = parse_company_page(html, url=link)
        # the whole company page was read: it is the current picture of the company
        for kind in ("shareholdings", "positions"):
            page.mark_snapshot(kind, page.companies[0].ticker, link)
        log.info("[%d/%d] %s: %s", i + 1, len(links), page.companies[0].ticker, page.summary())
        if i < 2 or not (page.shareholdings or page.positions):
            describe_page(html, link, brief=i >= 2)
        ds.extend(page)
    return ds


def parse_html_dir(path: str | Path) -> Dataset:
    """Parse company pages saved manually (one .html file per company).

    The file stem is used as the ticker if the page doesn't show one,
    e.g. `APU.html`.
    """
    ds = Dataset()
    for f in sorted(Path(path).glob("*.htm*")):
        ds.extend(parse_company_page(f.read_text(encoding="utf-8", errors="replace"),
                                     url=f.name, ticker_hint=f.stem))
    return ds


# ---- open.mse.mn (current MSE site) ------------------------------------------
#
# mse.mn (Next.js) lists companies via /api/all_companies; each company's
# profile is server-rendered on open.mse.mn/securities/<id>, with tabs loaded
# from /securities/<id>/tab/<name>.

OPEN_BASE = os.getenv("MSE_OPEN_BASE", "https://open.mse.mn")
COMPANY_LIST_API = os.getenv("MSE_COMPANY_LIST_API", "https://mse.mn/api/all_companies?lang=mn")
TABS = {"shareholders": "shareholders", "boardmembers": "board", "officials": "executive"}

INFO_LABELS = {
    "reg_no": ["улсын бүртгэлийн дугаар", "регистр"],
    "address": ["байршил хаяг", "хаяг"],
    "website": ["вэб сайт"],
    "sector": ["үйл ажиллагааны чиглэл", "бизнесийн чиглэл"],
    "ceo": ["гүйцэтгэх захирал"],
}


def list_companies_api(fetcher: Fetcher) -> list[Company]:
    raw = fetcher.get(COMPANY_LIST_API, use_cache=False)
    if not raw:
        raise RuntimeError(f"could not load company list from {COMPANY_LIST_API}")
    import json
    out = []
    for it in json.loads(raw):
        label = clean(str(it.get("name", "")))
        if "|" not in label:
            continue
        name, _, ticker = label.rpartition("|")
        name, ticker = clean(name) or label, clean(ticker).upper()
        cid = str(it.get("id") or it.get("value") or "")
        if not (cid and ticker):
            continue
        out.append(Company(ticker=ticker, name=name, mse_id=cid, url=f"{OPEN_BASE}/securities/{cid}"))
    return out


def parse_open_info(html: str, company: Company) -> list[Position]:
    """Fill company facts from the 'Ерөнхий мэдээлэл' key/value table; returns the CEO position."""
    soup = BeautifulSoup(html, "lxml")
    found: dict = {}
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) != 2:
            continue
        label, value = clean(cells[0].get_text(" ")).lower(), clean(cells[1].get_text(" "))
        if not value or value.upper() == "N/A":
            continue
        for key, words in INFO_LABELS.items():
            for rank, w in enumerate(words):   # earlier label = more specific
                if label.startswith(w) and rank < found.get(key, ("", 99))[1]:
                    found[key] = (value, rank)
                    break
    found = {k: v for k, (v, _) in found.items()}
    company.reg_no = found.get("reg_no", company.reg_no)
    company.address = found.get("address", company.address)
    company.website = found.get("website", company.website)
    company.sector = found.get("sector", company.sector)
    if "ceo" in found:
        return [Position(company.ticker, found["ceo"], "Гүйцэтгэх захирал", "executive", source=company.url)]
    return []


SHARES_PCT_RE = re.compile(r"^\s*([\d\s,.]+?)\s*(?:/\s*([\d.,]+)\s*%\s*/?)?\s*$")


def _split_shares_pct(cell: str) -> tuple[int | None, float | None]:
    """'46,300 /33.28%/' -> (46300, 33.28)."""
    m = SHARES_PCT_RE.match(cell or "")
    if not m:
        return parse_int(cell), None
    return parse_int(m.group(1)), parse_percent(m.group(2)) if m.group(2) else None


def _hcol(header: list[str], *words: str) -> int | None:
    for w in words:
        for i, h in enumerate(header):
            if w in h:
                return i
    return None


HEADER_LABELS = {"нэр", "овог нэр", "овог, нэр", "компанийн нэр", "name", "full name",
                 "хувьцаа эзэмшигч", "албан тушаал"}
_logged_company_tables: dict[str, bool] = {}


def parse_open_tab(html: str, ticker: str, kind: str, source: str) -> Dataset:
    """Parse an open.mse.mn tab fragment: shareholders / boardmembers / officials.

    Layouts (as served by open.mse.mn):
      shareholders: № | Нэр | Хувьцааны тоо ('46,300 /33.28%/') | Бусад компанийн хувьцааг эзэмших |
                    Түүнтэй нэгдмэл сонирхолтой этгээд | Огноо     (+ a '...түүх' history table: skipped)
      boardmembers: № | Нэр | Гишүүнчлэл | Албан тушаал | Сонгогдсон огноо | ... | Хувьцааны тоо | ...
      officials:    № | Нэр | Албан тушаал | Хувьцааны тоо          (КЭБАТ, plus securities officer table)
    """
    ds = Dataset()
    soup = BeautifulSoup(html, "lxml")
    for table in soup.find_all("table"):
        ctx = _context_text(table).lower()
        if "түүх" in ctx or "history" in ctx:
            continue  # historical holders/officers, not current
        header, rows = _table_rows(table)
        if not header and len(rows) > 1 and not any(re.search(r"\d", c) for c in rows[0]):
            header, rows = [h.lower() for h in rows[0]], rows[1:]
        if not header or not rows:
            continue
        c_name = _hcol(header, "нэр", "name")
        if c_name is None:
            continue
        if "компани" in header[c_name] or "company" in header[c_name]:
            # e.g. a member's positions in other companies: different shape, parsed separately
            if not _logged_company_tables.get(kind):
                _logged_company_tables[kind] = True
                log.info("TAB %s has a company-keyed table (skipped): header=%s first=%s",
                         source, header, rows[:2])
            continue
        date_col = _hcol(header, "огноо", "date")
        for r in rows:
            if c_name >= len(r):
                continue
            name = clean(r[c_name])
            if (not name or is_aggregate(name) or re.search(r"\d", name) or len(name) < 3
                    or name.lower() in HEADER_LABELS or name.lower() in header):
                continue  # empty, totals, junk, or a repeated header row
            if kind == "shareholders":
                c_sh = _hcol(header, "хувьцааны тоо", "тоо", "shares")
                c_pct = _hcol(header, "эзэмшлийн хувь", "эзлэх хувь", "%")
                shares, pct = _split_shares_pct(r[c_sh]) if c_sh is not None and c_sh < len(r) else (None, None)
                if pct is None and c_pct is not None and c_pct < len(r):
                    pct = parse_percent(r[c_pct])
                as_of = r[date_col] if date_col is not None and date_col < len(r) else ""
                ds.shareholdings.append(Shareholding(ticker, name, pct, shares, as_of=as_of, source=source))
            else:
                c_title = _hcol(header, "албан тушаал", "position", "title")
                title = clean(r[c_title]) if c_title is not None and c_title < len(r) else ""
                if title and re.fullmatch(r"[\d\s.,/-]+", title):
                    title = ""  # junk value in source
                if kind == "board":
                    c_mem = _hcol(header, "гишүүнчлэл")
                    member = clean(r[c_mem]) if c_mem is not None and c_mem < len(r) else ""
                    title = title or "ТУЗ-ийн гишүүн"
                    if member and member.lower() not in title.lower():
                        title = f"{title} ({member})"
                    role = "board"
                else:
                    if "мэргэжилтэн" in ctx and not title:
                        title = "Үнэт цаас хариуцсан мэргэжилтэн"
                    role = "board" if re.search(r"туз-ийн (дарга|гишүүн)", title.lower()) else "executive"
                since = r[date_col] if date_col is not None and date_col < len(r) else ""
                ds.positions.append(Position(ticker, name, title, role, since=since, source=source))
    return ds


def crawl_open(limit: int | None = None, fetcher: Fetcher | None = None,
               shard: tuple[int, int] | None = None) -> Dataset:
    from . import shard as sharding

    fetcher = fetcher or Fetcher()
    companies = list_companies_api(fetcher)
    log.info("company list: %d companies", len(companies))
    if shard:
        companies = sharding.select(companies, shard, key=lambda c: c.ticker)
        log.info("shard %d/%d: %d companies", shard[0], shard[1], len(companies))
    ds = Dataset(observed_at=date.today().isoformat())
    for i, c in enumerate(companies[:limit] if limit else companies):
        page = Dataset(companies=[c])
        html = fetcher.get(c.url)
        fetched = {"info": html is not None}
        if html:
            page.positions += parse_open_info(html, c)
            if i < 2:
                describe_page(html, c.url, brief=True)
        for tab, kind in TABS.items():
            url = f"{c.url}/tab/{tab}"
            frag = fetcher.get(url)
            fetched[kind] = frag is not None
            if not frag:
                continue
            if frag.lstrip()[:1] in "[{":
                log.info("TAB %s is JSON: %s", url, frag[:400])
            part = parse_open_tab(frag, c.ticker, kind, url)
            if i < 1:
                describe_page(frag, url)
            page.shareholdings += part.shareholdings
            page.positions += part.positions
        # The CEO appears in the info table and (usually) the officials tab, often in a
        # different name format; keep the tab entry when one exists.
        tab_ceo = any("гүйцэтгэх захирал" in p.title.lower() and p.source.endswith("/officials")
                      for p in page.positions)
        if tab_ceo:
            page.positions = [p for p in page.positions if p.source != c.url]
        seen, uniq = set(), []
        for p in page.positions:
            k = (p.person_name, p.title)
            if k not in seen:
                seen.add(k)
                uniq.append(p)
        page.positions = uniq
        # Only a fully fetched company is a snapshot: then whoever is missing compared
        # with the previous scrape has left. A failed fetch must not look like a departure.
        if fetched["shareholders"]:
            page.mark_snapshot("shareholdings", c.ticker, c.url)
        if fetched["info"] and fetched["board"] and fetched["executive"]:
            page.mark_snapshot("positions", c.ticker, c.url)
        log.info("[%d/%d] %s %s: %s", i + 1, len(companies), c.ticker, c.name, page.summary())
        ds.extend(page)
    return ds
