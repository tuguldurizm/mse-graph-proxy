"""Market data (issue #31): share prices, shares outstanding and market capitalisation from
mse.mn, so stakes, state holdings and groups can be valued in MNT, not only counted.

Source: each listed company's trading tab, open.mse.mn/securities/<id>/tab/tradeinfo, read
through the scraper's Fetcher (the same egress route, 1 s crawl delay and 20 h page cache as the
company scrape). As served on 2026-11-23 it holds two label/value tables (Өмнөх өдрийн хаалтын ханш,
Хаалтын ханш (0.00 until the day's first trade), Нийт гаргасан хувьцаа, Эргэлтэд гаргасан хувьцаа,
Зах зээлийн үнэлгээ = previous close × all shares issued, ...) and "Арилжигдсан түүх", every
trading day newest first (Огноо | Нээлтийн ханш | Хаалтын ханш | ...): the last close and its date
come from there when there was no trade today. A page is 0.5 to 2 MB. The profile page
(open.mse.mn/securities/<id>) is read only when the tab has no share count.

Parsing goes by labels (and, for JSON, key names) rather than positions, and skips the previous
day's, bid / ask, 52-week and range values. MARKET_LAYOUT_LOG=N logs the layout of the first N
pages (`MARKET layout` lines) to check it again if the site changes. The trade report
(`MSE_TRADE_REPORT_API`, mse.mn/api/public/trade-report/detailed) turned out to be an .xlsx
download; it is read only with MARKET_TRADE_REPORT=1 and the JSON / HTML-table parsers here.

Stored on the Company node (only listed companies, matched by ticker):

    price               the last close (MNT per share)
    price_date          the date of that trade (the last trade, often weeks ago: many MSE
                        stocks barely trade), else the day it was read
    price_as_of         the day it was read
    price_stale         price_date more than MARKET_STALE_DAYS (30) before price_as_of
    shares_outstanding  also a dated (:Fact) `shares_outstanding` of source mse.mn (#38),
                        so a share issue or buy-back shows as a `company_fact_changed`
    market_cap          the source's figure, else price × shares_outstanding
                        (`market_cap_basis`: "source" | "price_x_shares")
    px_dates, px_close, px_cap   the daily series: one point per trade date, oldest first,
                        at most MARKET_HISTORY_POINTS (property lists: no extra nodes on Aura Free)

Each read is also archived as data/archive/<date>/market/quotes.csv.gz.

A stake's value is percent / 100 × market_cap of the company it is in (`value_mnt`); it is
an indication, not a price anyone would pay: a thinly traded share's last close can be old.
"""
from __future__ import annotations

import csv
import gzip
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

from bs4 import BeautifulSoup

from .normalize import clean, parse_date

log = logging.getLogger(__name__)

TRADE_REPORT_API = os.getenv("MSE_TRADE_REPORT_API",
                             "https://mse.mn/api/public/trade-report/detailed?lang=mn")


def stale_days() -> int:
    return int(os.getenv("MARKET_STALE_DAYS", "30"))


def history_points() -> int:
    return int(os.getenv("MARKET_HISTORY_POINTS", "1000"))


# ---- numbers ------------------------------------------------------------------------

_UNITS = (("их наяд", 1e12), ("тэрбум", 1e9), ("сая", 1e6), ("мянга", 1e3),
          ("trillion", 1e12), ("billion", 1e9), ("million", 1e6), ("bn", 1e9), ("mln", 1e6))
_NUM_RE = re.compile(r"[-+]?\d[\d\s,.'’]*")


def unit_factor(*texts: str) -> float:
    """×1e6 for "сая", ×1e9 for "тэрбум" ... named in a label or a value ("сая ₮")."""
    t = " ".join(clean(str(x)).lower() for x in texts if x)
    for word, f in _UNITS:
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", t):
            return f
    return 1.0


def parse_number(value) -> float | None:
    """'1,234.56' / '1 234,56' / '12,500' / '0.85' / '-3.2%' / 1234 -> float; '' / 'N/A' -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = _NUM_RE.search(clean(str(value)).replace("−", "-"))
    if not m:
        return None
    s = re.sub(r"[\s'’]", "", m.group(0)).rstrip(".,")
    if "," in s and "." in s:
        # the later one is the decimal mark
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif s.count(",") == 1 and len(s.split(",")[1]) != 3:
        s = s.replace(",", ".")                    # "12,5": a decimal comma
    else:
        s = s.replace(",", "")                     # "12,500" / "1,234,567": thousands
        if s.count(".") > 1:
            s = s.replace(".", "")                 # "1.234.567": thousands with dots
    try:
        return float(s)
    except ValueError:
        return None


# ---- quotes -------------------------------------------------------------------------

@dataclass
class Quote:
    ticker: str
    close: float | None = None
    trade_date: str = ""             # date of the last trade (YYYY-MM-DD), if the source says
    shares_outstanding: int | None = None
    market_cap: float | None = None  # as the source gives it (MNT)
    volume: float | None = None
    turnover: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    change_percent: float | None = None
    source: str = ""
    # (date, close) rows of a price-history table, oldest first
    series: list[tuple[str, float]] = field(default_factory=list)

    def has_data(self) -> bool:
        return self.close is not None or self.market_cap is not None or self.shares_outstanding is not None


def merge(a: Quote | None, b: Quote | None) -> Quote | None:
    """One quote from two sources for the same ticker. The close (and its date) comes from the
    one with the later trade date (on a tie, or with no dates, `a`); the rest from `a` first."""
    if a is None or b is None:
        return a or b
    later = b if b.close is not None and (a.close is None or (b.trade_date or "") > (a.trade_date or "")) else a
    out = Quote(a.ticker)
    for f in ("shares_outstanding", "market_cap", "volume", "turnover", "open", "high", "low", "change_percent"):
        setattr(out, f, getattr(a, f) if getattr(a, f) is not None else getattr(b, f))
    out.close, out.trade_date = later.close, later.trade_date or a.trade_date or b.trade_date
    out.source = "; ".join(s for s in dict.fromkeys([a.source, b.source]) if s)
    out.series = sorted({**dict(b.series), **dict(a.series)}.items())
    return out


# ---- labels (HTML) -------------------------------------------------------------------

# label -> field, tested in this order (the first match wins): "Зах зээлийн үнэлгээ" holds
# "үнэ", "Нээлтийн ханш" holds "ханш", so the specific ones come first.
LABEL_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("market_cap", ("зах зээлийн үнэлгээ", "зах зээлийн капитал", "капитализаци", "market cap",
                    "market capitalization", "market value")),
    ("change_percent", ("өөрчлөлт (%)", "өөрчлөлт %", "өөрчлөлтийн хувь", "change (%)", "change %", "% change")),
    ("turnover", ("үнийн дүн", "арилжааны дүн", "turnover", "trade value", "traded value")),
    ("volume", ("арилжсан тоо", "арилжаалагдсан тоо", "арилжааны тоо", "тоо ширхэг", "ширхэг", "volume",
                "traded quantity", "quantity")),
    ("shares_outstanding", ("гаргасан хувьцаа", "нийт хувьцаа", "бүртгэлтэй хувьцаа", "эргэлтэд байгаа",
                            "хувьцааны нийт тоо", "хувьцааны тоо", "shares outstanding", "outstanding shares",
                            "listed shares", "issued shares", "number of shares", "total shares")),
    ("open", ("нээлтийн ханш", "нээлтийн үнэ", "нээлт", "opening price", "open")),
    ("high", ("дээд ханш", "хамгийн их ханш", "хамгийн өндөр", "дээд үнэ", "high")),
    ("low", ("доод ханш", "хамгийн бага ханш", "хамгийн бага", "доод үнэ", "low")),
    ("trade_date", ("сүүлд арилжаалагдсан огноо", "сүүлийн арилжааны огноо", "арилжааны огноо",
                    "сүүлд арилжаалагдсан", "last trade date", "last traded", "trade date", "огноо", "date")),
    ("close", ("хаалтын ханш", "хаалтын үнэ", "сүүлийн ханш", "сүүлийн үнэ", "сүүлд арилжаалагдсан ханш",
               "closing price", "close price", "last price", "last traded price", "ханш", "үнэ", "price",
               "close", "last")),
]
# words in a label that make it about something else (the previous day, a 52-week range, a dividend)
_SKIP_LABEL = re.compile(r"өмнөх|prev|52|жилийн|year|ногдол|dividend|нэрлэсэн|nominal|par value|ipo|санал|bid|ask|"
                         r"авах|зарах|vwap|хязгаар|range|захиалга|order|шинэчлэгдсэн|updated|нийтэлсэн|published|"
                         r"цэвэр хөрөнгө|нэгж эрх")
# an absolute change ("Өөрчлөлт", "Change") is no price; its percent form is matched first
_CHANGE = re.compile(r"өөрчлөлт|change")


def _matches(t: str, w: str) -> bool:
    return (t == w or t.startswith((w + " ", w + ":", w + "(", w + ",")) or t.endswith(" " + w)
            or (len(w) > 5 and w in t))


def classify_label(label: str) -> str | None:
    """The quote field a label names ("Хаалтын ханш" -> "close"), or None."""
    t = clean(label).lower().rstrip(":").strip()
    if not t or len(t) > 60 or _SKIP_LABEL.search(t):
        return None
    if _CHANGE.search(t):
        pct = dict(LABEL_RULES)["change_percent"]
        return "change_percent" if any(_matches(t, w) for w in pct) else None
    for name, words in LABEL_RULES:
        if any(_matches(t, w) for w in words):
            return name
    return None


def _set(q: Quote, name: str, raw: str, label: str = "") -> bool:
    """Put one labelled value on the quote unless that field is already set."""
    if getattr(q, name) not in (None, ""):
        return False
    if name == "trade_date":
        d = parse_date(raw)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            q.trade_date = d
            return True
        return False
    v = parse_number(raw)
    if v is None:
        return False
    if name in ("market_cap", "turnover"):
        v *= unit_factor(label, raw)
    if name == "shares_outstanding":
        v *= unit_factor(label, raw)
        if v < 1:
            return False
        q.shares_outstanding = int(round(v))
        return True
    if name in ("close", "open", "high", "low") and v <= 0:
        return False
    setattr(q, name, v)
    return True


def _pairs(soup) -> list[tuple[str, str]]:
    """(label, value) pairs: 2-cell table rows, <dt>/<dd>, an element with exactly two short
    text children, and "Label: value" text."""
    out: list[tuple[str, str]] = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"], recursive=False) or tr.find_all(["th", "td"])
        if len(cells) == 2:
            out.append((cells[0].get_text(" "), cells[1].get_text(" ")))
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            out.append((dt.get_text(" "), dd.get_text(" ")))
    for el in soup.find_all(["div", "li", "p", "span"]):
        kids = [k for k in el.find_all(recursive=False) if k.name not in ("br", "i", "svg", "img")]
        if len(kids) == 2 and not any(k.find(["div", "table", "ul"]) for k in kids):
            a, b = clean(kids[0].get_text(" ")), clean(kids[1].get_text(" "))
            if a and b and len(a) <= 60 and len(b) <= 40:
                out.append((a, b))
    for line in soup.get_text("\n").split("\n"):
        m = re.match(r"\s*([^:\d][^:]{1,58}):\s*(\S.{0,38})$", line)
        if m:
            out.append((m.group(1), m.group(2)))
    return [(clean(a), clean(b)) for a, b in out if clean(a) and clean(b)]


def _header_cells(table) -> tuple[list[str], list[list[str]]]:
    rows = [[clean(c.get_text(" ")) for c in tr.find_all(["th", "td"])] for tr in table.find_all("tr")]
    rows = [r for r in rows if any(r)]
    if not rows:
        return [], []
    return [h.lower() for h in rows[0]], rows[1:]


def _series(table) -> list[tuple[str, float]]:
    """(date, close) rows of a price-history table: a date column and a close/price column."""
    header, rows = _header_cells(table)
    kinds = [classify_label(h) for h in header]
    if "trade_date" not in kinds or "close" not in kinds:
        return []
    d, c = kinds.index("trade_date"), kinds.index("close")
    out = {}
    for r in rows:
        if max(d, c) < len(r):
            day, px = parse_date(r[d]), parse_number(r[c])
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) and px and px > 0:
                out[day] = px
    return sorted(out.items())


def _row_quote(table, ticker: str, q: Quote) -> None:
    """A quote table with one row per security (Симбол | Нэр | Ханш | ...): read this ticker's row."""
    header, rows = _header_cells(table)
    if not header:
        return
    sym = next((i for i, h in enumerate(header) if h in ("симбол", "symbol", "код", "ticker")), None)
    if sym is None:
        return
    for r in rows:
        if sym < len(r) and _ticker(r[sym]) == ticker:
            for i, h in enumerate(header):
                name = classify_label(h)
                if name and i < len(r) and i != sym:
                    _set(q, name, r[i], h)


def parse_html(html: str, ticker: str, source: str = "") -> Quote:
    """A quote from an HTML page or tab fragment (tradeinfo tab, profile page)."""
    soup = BeautifulSoup(html or "", "lxml")
    q = Quote(ticker.upper(), source=source)
    for label, value in _pairs(soup):
        name = classify_label(label)
        if name:
            _set(q, name, value, label)
    for table in soup.find_all("table"):
        if not q.series:
            q.series = _series(table)
        _row_quote(table, q.ticker, q)
    if q.series and q.close is None:
        # no trade yet today (today's close is 0.00): the last close is the history's newest row,
        # and today's zero volume and turnover don't belong to it
        q.trade_date, q.close = q.series[-1]
        q.volume = q.turnover = None
    if q.series and not q.trade_date and q.close == q.series[-1][1]:
        q.trade_date = q.series[-1][0]
    return q


# ---- JSON (the trade report API) --------------------------------------------------------

def _norm_key(k: str) -> str:
    return re.sub(r"[^a-zа-яөү0-9]", "", str(k).lower())


JSON_KEYS = {
    "ticker": ("symbol", "ticker", "securitycode", "seccode", "stockcode", "symbolcode", "code", "симбол"),
    "close": ("closingprice", "closeprice", "close", "lastprice", "lasttradedprice", "lasttradeprice",
              "last", "price", "ханш", "хаалтынханш"),
    "trade_date": ("tradedate", "tradingdate", "lasttradedate", "date", "mdentrydate", "огноо"),
    "volume": ("volume", "tradedvolume", "totalvolume", "quantity", "qty", "тоо"),
    "turnover": ("turnover", "tradevalue", "totalvalue", "value", "amount", "үнийндүн"),
    "open": ("openingprice", "openprice", "open"),
    "high": ("highprice", "high", "maxprice"),
    "low": ("lowprice", "low", "minprice"),
    "change_percent": ("changepercent", "changepct", "percentchange", "pctchange", "changep"),
    "shares_outstanding": ("sharesoutstanding", "outstandingshares", "totalshares", "listedshares",
                           "issuedshares", "numberofshares"),
    "market_cap": ("marketcap", "marketcapitalization", "marketvalue", "capitalization"),
}
_TICKER_RE = re.compile(r"^([A-ZА-ЯӨҮ0-9]{2,6})(?:-[A-Z]-\d{3,4})?$")


def _ticker(value) -> str:
    """'APU' / 'apu' / 'APU-O-0000' (MSE feed codes) -> 'APU'; anything else -> ''."""
    m = _TICKER_RE.match(clean(str(value or "")).upper())
    return m.group(1) if m else ""


def _field_of(key: str) -> str | None:
    k = _norm_key(key)
    for name, keys in JSON_KEYS.items():
        if k in keys:
            return name
    return None


def _records(obj, depth: int = 0):
    """Every dict, anywhere in the JSON, that names a ticker."""
    if depth > 6:
        return
    if isinstance(obj, dict):
        if any(_field_of(k) == "ticker" and _ticker(v) for k, v in obj.items() if not isinstance(v, (dict, list))):
            yield obj
        for v in obj.values():
            if isinstance(v, (dict, list)):
                yield from _records(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _records(v, depth + 1)


def parse_json(obj, source: str = "") -> dict[str, Quote]:
    """{ticker: Quote} from a JSON document (the trade report). A ticker listed twice (e.g. one
    row per board or per trade) keeps its latest trade date's values, the first row on a tie."""
    out: dict[str, Quote] = {}
    for rec in _records(obj):
        fields: dict[str, object] = {}
        for k, v in rec.items():
            name = _field_of(k)
            if name and name not in fields and not isinstance(v, (dict, list)):
                fields[name] = v
        t = _ticker(fields.pop("ticker", ""))
        if not t:
            continue
        q = Quote(t, source=source)
        for name, v in fields.items():
            if v not in (None, ""):
                _set(q, name, str(v) if not isinstance(v, (int, float)) else v, name)  # type: ignore[arg-type]
        if not q.has_data():
            continue
        had = out.get(t)
        out[t] = merge(q, had) if had and (q.trade_date or "") > (had.trade_date or "") else merge(had, q)
    return out


def parse_report(text: str, source: str = TRADE_REPORT_API) -> dict[str, Quote]:
    """The trade report: JSON when it is JSON, else an HTML table with one row per security."""
    if not text:
        return {}
    s = text.lstrip()
    if s[:1] in "[{":
        try:
            return parse_json(json.loads(s), source)
        except ValueError:
            return {}
    soup = BeautifulSoup(text, "lxml")
    out: dict[str, Quote] = {}
    for table in soup.find_all("table"):
        header, rows = _header_cells(table)
        sym = next((i for i, h in enumerate(header) if h in ("симбол", "symbol", "код", "ticker")), None)
        if sym is None:
            continue
        for r in rows:
            t = _ticker(r[sym]) if sym < len(r) else ""
            if t and t not in out:
                q = Quote(t, source=source)
                for i, h in enumerate(header):
                    name = classify_label(h)
                    if name and i != sym and i < len(r):
                        _set(q, name, r[i], h)
                if q.has_data():
                    out[t] = q
    return out


# ---- layout logging -----------------------------------------------------------------------

def describe(text: str, url: str) -> None:
    """Log the layout of a market page or JSON document (`MARKET layout`), to tighten the parser."""
    s = (text or "").lstrip()
    if s[:1] in "[{":
        try:
            obj = json.loads(s)
        except ValueError:
            log.info("MARKET layout %s: not JSON after all: %r", url, s[:300])
            return
        recs = list(_records(obj))
        top = list(obj)[:30] if isinstance(obj, dict) else f"list[{len(obj)}]"
        log.info("MARKET layout %s: JSON top=%s records=%d keys=%s first=%s", url, top, len(recs),
                 sorted(recs[0])[:40] if recs else None,
                 json.dumps(recs[:2], ensure_ascii=False)[:800] if recs else s[:600])
        return
    soup = BeautifulSoup(text or "", "lxml")
    tables = soup.find_all("table")
    log.info("MARKET layout %s: size=%d tables=%d scripts=%d", url, len(text or ""), len(tables),
             len(soup.find_all("script")))
    for i, t in enumerate(tables[:6]):
        rows = [[clean(c.get_text(" "))[:28] for c in tr.find_all(["th", "td"])] for tr in t.find_all("tr")[:3]]
        log.info("MARKET layout %s table %d (%d rows): %s", url, i, len(t.find_all("tr")), rows)
    pairs = [(a[:40], b[:30]) for a, b in _pairs(soup)][:40]
    log.info("MARKET layout %s pairs: %s", url, pairs)
    words = re.compile(r"ханш|үнэлгээ|хувьцаа|арилжаа|price|cap|shares|volume", re.I)
    for script in soup.find_all("script"):
        body = script.string or ""
        hits = sorted(set(re.findall(r"""["'](/[\w/.?=&{}$-]*(?:api|chart|trade|price|history)[\w/.?=&{}$-]*)["']""",
                                     body, re.I)))[:15]
        if hits or (body and words.search(body) and len(body) < 4000):
            log.info("MARKET layout %s script: paths=%s text=%r", url, hits, body[:300] if not hits else "")
    text_only = clean(soup.get_text(" "))
    log.info("MARKET layout %s text: %s", url, text_only[:900])


# ---- reading mse.mn ------------------------------------------------------------------------

def listed_companies(g) -> list[dict]:
    return g.run(
        """
        MATCH (c:Company) WHERE coalesce(c.listed, true) AND NOT c.ticker STARTS WITH '~'
        RETURN c.ticker AS ticker, c.name AS name, c.mse_id AS mse_id, c.url AS url
        ORDER BY c.ticker
        """)


def read(g=None, fetcher=None, companies: list[dict] | None = None, limit: int | None = None,
         profile: bool | None = None, layout: int | None = None) -> tuple[dict[str, Quote], dict]:
    """Read today's quotes: each listed company's trading tab (and its profile page when the tab
    gives no share count; first the trade report with MARKET_TRADE_REPORT=1).
    Returns ({ticker: Quote}, report)."""
    from .scraper import OPEN_BASE, Fetcher

    fetcher = fetcher or Fetcher()
    companies = companies if companies is not None else listed_companies(g)
    if limit:
        companies = companies[:limit]
    profile = os.getenv("MARKET_PROFILE", "1") != "0" if profile is None else profile
    layout = int(os.getenv("MARKET_LAYOUT_LOG", "0")) if layout is None else layout
    report = {"companies": len(companies), "report_quotes": 0, "tabs_read": 0, "tabs_failed": 0,
              "profiles_read": 0, "errors": []}
    quotes: dict[str, Quote] = {}

    if os.getenv("MARKET_TRADE_REPORT", "0") == "1":
        text = fetcher.get(TRADE_REPORT_API, use_cache=False)
        if text and text.startswith("PK"):
            # what mse.mn serves there (2026-11-23) is an .xlsx download, not JSON
            report["errors"].append("trade report: a spreadsheet (.xlsx), not read")
        elif text:
            if layout:
                describe(text, TRADE_REPORT_API)
            quotes = parse_report(text)
            report["report_quotes"] = len(quotes)
        else:
            report["errors"].append(f"trade report: {fetcher.last_error or fetcher.last_status}")

    for i, c in enumerate(companies):
        t = (c.get("ticker") or "").upper()
        base = c.get("url") or (f"{OPEN_BASE}/securities/{c['mse_id']}" if c.get("mse_id") else "")
        if not t or not base:
            continue
        url = f"{base}/tab/tradeinfo"
        html = fetcher.get(url)
        if html is None:
            report["tabs_failed"] += 1
            continue
        report["tabs_read"] += 1
        if i < layout:
            describe(html, url)
        q = parse_html(html, t, url)
        if profile and q.shares_outstanding is None and q.market_cap is None:
            page = fetcher.get(base)
            if page:
                report["profiles_read"] += 1
                if i < layout:
                    describe(page, base)
                q = merge(q, parse_html(page, t, base))
        q = merge(q, quotes.get(t)) if t in quotes else q
        if q and q.has_data():
            quotes[t] = q
    report["quotes"] = sum(1 for q in quotes.values() if q.has_data())
    report["with_close"] = sum(1 for q in quotes.values() if q.close is not None)
    report["with_shares"] = sum(1 for q in quotes.values() if q.shares_outstanding is not None)
    report["with_market_cap"] = sum(1 for q in quotes.values() if q.market_cap is not None)
    return quotes, report


# ---- archive -------------------------------------------------------------------------------

FIELDS = ["ticker", "close", "trade_date", "shares_outstanding", "market_cap", "volume", "turnover",
          "open", "high", "low", "change_percent", "source"]


def archive(quotes: dict[str, Quote], when: str, root: str | Path = "data/archive") -> Path:
    """data/archive/<date>/market/quotes.csv.gz (the read's raw values, without the series)."""
    target = Path(root) / when / "market"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "quotes.csv.gz"
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for q in sorted(quotes.values(), key=lambda x: x.ticker):
            w.writerow({k: v for k, v in asdict(q).items() if k in FIELDS})
    return path


def read_archive(path: str | Path) -> dict[str, Quote]:
    out = {}
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            q = Quote(row["ticker"], source=row.get("source", ""), trade_date=row.get("trade_date", ""))
            for k in FIELDS[1:-1]:
                if k != "trade_date" and row.get(k):
                    v = float(row[k])
                    setattr(q, k, int(v) if k == "shares_outstanding" else v)
            out[q.ticker] = q
    return out


# ---- writing to the graph ---------------------------------------------------------------------

def _is_stale(price_date: str | None, as_of: str) -> bool:
    if not price_date:
        return False
    try:
        return date.fromisoformat(as_of) - date.fromisoformat(price_date[:10]) > timedelta(days=stale_days())
    except ValueError:
        return False


def write(g, quotes: dict[str, Quote], when: str | None = None, run: str | None = None) -> dict:
    """Store the quotes on the listed companies (module docstring). A quote without a close keeps
    the company's last price; a missing share count keeps the last one, so the market cap is
    price × the last known shares. Returns counts."""
    from .graph import new_run_id

    when = when or date.today().isoformat()
    run = run or new_run_id()
    have = {r["ticker"]: r for r in g.run(
        """
        MATCH (c:Company) WHERE c.ticker IN $tickers AND coalesce(c.listed, true)
        RETURN c.ticker AS ticker, c.price AS price, c.price_date AS price_date, c.shares_outstanding AS shares,
               coalesce(c.px_dates, []) AS dates, coalesce(c.px_close, []) AS closes, coalesce(c.px_cap, []) AS caps
        """, tickers=sorted(quotes))}
    rows, facts = [], []
    keep = history_points()
    for t, q in sorted(quotes.items()):
        h = have.get(t)
        if h is None or not q.has_data():
            continue
        close = q.close if q.close is not None else h["price"]
        pdate = (q.trade_date or when) if q.close is not None else h["price_date"]
        shares = q.shares_outstanding or h["shares"]
        if q.market_cap is not None:
            cap, basis = q.market_cap, "source"
        elif close is not None and shares:
            cap, basis = close * shares, "price_x_shares"
        else:
            cap, basis = None, None
        series = dict(zip(h["dates"], zip(h["closes"], h["caps"])))
        for day, px in q.series:      # a price-history table fills in the days before
            if day not in series and day <= (pdate or when):
                series[day] = (px, px * shares if shares else -1.0)
        if close is not None and pdate:
            series[pdate[:10]] = (close, cap if cap is not None else -1.0)
        days = sorted(series)[-keep:]
        rows.append({"ticker": t, "price": close, "price_date": pdate, "shares": shares, "cap": cap, "basis": basis,
                     "volume": q.volume, "turnover": q.turnover, "change": q.change_percent,
                     "stale": _is_stale(pdate, when) if close is not None else None, "source": q.source,
                     "dates": days, "closes": [series[d][0] for d in days], "caps": [series[d][1] for d in days]})
        if q.shares_outstanding:
            facts.append({"ticker": t, "value": str(q.shares_outstanding)})
    g.run(
        """
        UNWIND $rows AS r
        MATCH (c:Company {ticker: r.ticker})
        SET c.price = r.price, c.price_date = r.price_date, c.price_as_of = $when, c.price_stale = r.stale,
            c.shares_outstanding = r.shares, c.market_cap = r.cap, c.market_cap_basis = r.basis,
            c.trade_volume = r.volume, c.trade_turnover = r.turnover, c.price_change_percent = r.change,
            c.market_source = r.source, c.px_dates = r.dates, c.px_close = r.closes, c.px_cap = r.caps
        """, rows=rows, when=when)
    changed = _record_share_facts(g, facts, when, run)
    g.run("MERGE (m:Meta {key: 'market'}) SET m.at = $at, m.as_of = $when, m.companies = $n",
          at=_utc_now(), when=when, n=len(rows))
    return {"companies": len(rows), "with_price": sum(1 for r in rows if r["price"] is not None),
            "with_market_cap": sum(1 for r in rows if r["cap"] is not None),
            "stale": sum(1 for r in rows if r["stale"]), "shares_changed": changed, "run": run}


def _record_share_facts(g, rows: list[dict], when: str, run: str) -> int:
    """shares_outstanding as a dated (:Fact) chain of source mse.mn (as graph._record_facts)."""
    if not rows:
        return 0
    got = g.run(
        """
        UNWIND $rows AS r
        MATCH (c:Company {ticker: r.ticker})
        OPTIONAL MATCH (c)-[:HAS_FACT]->(f:Fact {name: 'shares_outstanding', source: 'mse.mn'})
        WITH c, r, collect(f) AS had
        WITH c, r, size(had) = 0 AS first, [f IN had WHERE f.current] AS open
        WITH c, r, first, [f IN open WHERE f.norm = r.value] AS same, [f IN open WHERE f.norm <> r.value] AS other
        FOREACH (f IN same | SET f.last_seen = CASE WHEN $when > f.last_seen THEN $when ELSE f.last_seen END)
        FOREACH (f IN other | SET f.current = false, f.until = $when, f.closed_at = $when, f.closed_run = $run)
        FOREACH (_ IN CASE WHEN size(same) = 0 THEN [1] ELSE [] END |
            CREATE (c)-[:HAS_FACT]->(:Fact {name: 'shares_outstanding', value: r.value, norm: r.value,
                                            source: 'mse.mn', current: true, first_seen: $when, last_seen: $when,
                                            opened_run: $run, baseline: first}))
        RETURN size(other) AS closed
        """, rows=rows, when=when, run=run)
    return sum(1 for r in got if r["closed"])


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hours_since_read(g) -> float | None:
    from datetime import datetime, timezone
    rows = g.run("MATCH (m:Meta {key: 'market'}) RETURN m.at AS at")
    if not rows or not rows[0]["at"]:
        return None
    return (datetime.now(timezone.utc) - datetime.fromisoformat(rows[0]["at"])).total_seconds() / 3600


def step(g, fetcher=None, limit: int | None = None, when: str | None = None) -> dict:
    """Read, archive and store today's quotes (the pipeline's and the daily job's market step)."""
    when = when or date.today().isoformat()
    quotes, report = read(g, fetcher=fetcher, limit=limit)
    if not quotes:
        return {**report, "stored": None}
    try:
        archive(quotes, when)
    except OSError as exc:
        report["errors"].append(f"archive: {exc}")
    return {**report, "stored": write(g, quotes, when)}


# ---- reading it back ---------------------------------------------------------------------------

def value_mnt(percent, market_cap) -> float | None:
    """The MNT value of a stake: percent / 100 × the company's market cap (None if either is missing)."""
    if percent is None or market_cap is None or market_cap < 0:
        return None
    return round(float(percent) / 100.0 * float(market_cap), 0)


_MARKET = """
MATCH (c:Company) WHERE coalesce(c.listed, true) AND NOT c.ticker STARTS WITH '~'
RETURN c.key AS key, c.ticker AS ticker, c.name AS name, c.sector AS sector, c.price AS price,
       c.price_date AS price_date, c.price_as_of AS price_as_of, coalesce(c.price_stale, false) AS stale,
       c.shares_outstanding AS shares_outstanding, c.market_cap AS market_cap,
       c.market_cap_basis AS market_cap_basis, c.price_change_percent AS change_percent,
       c.trade_volume AS volume, size(coalesce(c.px_dates, [])) AS points
"""
SORTS = {
    "cap": lambda r: (r["market_cap"] is None, -(r["market_cap"] or 0)),
    "price_date": lambda r: (r["price_date"] is None, r["price_date"] or ""),
    "ticker": lambda r: (r["ticker"] or "",),
}


def market(g, sort: str = "cap") -> dict:
    """Every listed company's price and market cap, largest first, with market totals."""
    rows = g.run(_MARKET)
    total = sum(r["market_cap"] for r in rows if r["market_cap"] is not None)
    for r in rows:
        r["weight_percent"] = round(100.0 * r["market_cap"] / total, 3) if total and r["market_cap"] is not None else None
    rows.sort(key=SORTS.get(sort, SORTS["cap"]))
    meta = g.run("MATCH (m:Meta {key: 'market'}) RETURN m.at AS at, m.as_of AS as_of")
    priced = [r for r in rows if r["price"] is not None]
    summary = {
        "companies": len(rows),
        "priced": len(priced),
        "with_market_cap": sum(1 for r in rows if r["market_cap"] is not None),
        "stale": sum(1 for r in priced if r["stale"]),
        "total_market_cap": round(total, 0) if total else None,
        "stale_days": stale_days(),
        "read_at": meta[0]["at"] if meta else None,
        "as_of": meta[0]["as_of"] if meta else None,
    }
    return {"items": rows, "summary": summary}


def company_market(g, key: str, max_points: int = 400) -> dict | None:
    """One company's quote and its price series (thinned to `max_points`)."""
    rows = g.run(
        """
        MATCH (c:Company {key: $key})
        RETURN c.ticker AS ticker, c.price AS price, c.price_date AS price_date, c.price_as_of AS price_as_of,
               coalesce(c.price_stale, false) AS stale, c.shares_outstanding AS shares_outstanding,
               c.market_cap AS market_cap, c.market_cap_basis AS market_cap_basis,
               c.price_change_percent AS change_percent, c.trade_volume AS volume, c.market_source AS source,
               coalesce(c.px_dates, []) AS dates, coalesce(c.px_close, []) AS closes, coalesce(c.px_cap, []) AS caps
        """, key=g.resolve_key(key))
    if not rows:
        return None
    r = rows[0]
    pts = [{"date": d, "close": c, "market_cap": m if m is not None and m >= 0 else None}
           for d, c, m in zip(r.pop("dates"), r.pop("closes"), r.pop("caps"))]
    if len(pts) > max_points:
        step_ = len(pts) / max_points
        pts = [pts[int(i * step_)] for i in range(max_points - 1)] + [pts[-1]]
    r["series"] = pts
    r["stale_days"] = stale_days()
    return r


def caps(g) -> dict[str, dict]:
    """{company key: {market_cap, stale, price_date}} for the companies that have a market cap."""
    return {r["key"]: r for r in g.run(
        "MATCH (c:Company) WHERE c.market_cap IS NOT NULL "
        "RETURN c.key AS key, c.market_cap AS market_cap, coalesce(c.price_stale, false) AS stale, "
        "c.price_date AS price_date")}


def summary_line(g) -> dict:
    """The `PIPELINE market` log line: coverage and the largest companies."""
    m = market(g)
    s = m["summary"]
    return {"priced": s["priced"], "with_market_cap": s["with_market_cap"], "of": s["companies"],
            "stale": s["stale"], "total_market_cap": s["total_market_cap"], "as_of": s["as_of"],
            "top": [r["ticker"] for r in m["items"][:5] if r["market_cap"] is not None]}
