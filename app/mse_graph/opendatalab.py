"""Importer for Open Data Lab Mongolia (opendatalab.mn) datasets.

Open Data Lab publishes datasets as downloadable files (CSV / XLSX / JSON),
often via a CKAN data portal. This module:

* `search(query)`   - lists datasets and their downloadable resources, via
                      the CKAN API if available, otherwise by scanning the
                      page for data-file links;
* `import_table()`  - reads one CSV/XLSX/JSON table and maps its columns to
                      companies / shareholders / officers by header keywords
                      (Mongolian or English), returning a `Dataset`.

Company rows are matched to MSE-listed companies by registration number or
name. Companies that aren't listed on MSE become *unlisted* :Company nodes
(listed=false, ticker "~<reg no>"), so ownership chains that pass through
private holding companies (listed co. <- holding LLC <- people) are kept.

opendatalab.mn could not be reached while this was written; the portal
layout is assumed, not verified. `ODL_BASE_URL` / `ODL_CKAN_API` override
the endpoints, and any dataset file can be imported directly by URL or path.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

from .config import settings
from .models import Company, Dataset, Position, Shareholding
from .normalize import clean, entity_key, is_aggregate, parse_int, parse_percent

log = logging.getLogger(__name__)

DATA_EXT_RE = re.compile(r"\.(csv|xlsx|xls|json)(?:$|\?)", re.IGNORECASE)

# Header keywords per target field. Order matters: first match wins, and a
# column is only assigned to one field.
COLUMNS: dict[str, list[str]] = {
    "ticker": ["симбол", "ticker", "symbol", "тикер"],
    "company_reg": ["компанийн регистр", "хуулийн этгээдийн регистр", "company reg", "company_registration",
                    "регистрийн дугаар", "улсын бүртгэлийн дугаар", "registration number", "reg_no", "регистр"],
    "holder_name": ["эцсийн өмчлөгч", "эцсийн эзэмшигч", "beneficial owner", "хувьцаа эзэмшигч", "үүсгэн байгуулагч",
                    "shareholder", "founder", "эзэмшигч", "owner", "holder"],
    "holder_type": ["эзэмшигчийн төрөл", "holder type", "owner type", "type of owner"],
    "percent": ["эзэмшлийн хувь", "эзлэх хувь", "ownership %", "ownership", "percent", "хувь", "share %", "%"],
    "shares": ["хувьцааны тоо", "ширхэг", "number of shares", "shares", "quantity"],
    "title": ["албан тушаал", "position", "title", "role"],
    "person_name": ["овог нэр", "удирдлагын нэр", "захирлын нэр", "гүйцэтгэх захирал", "director", "officer",
                    "person", "full name", "хүний нэр"],
    "company_name": ["компанийн нэр", "хуулийн этгээдийн нэр", "аж ахуйн нэгжийн нэр", "байгууллагын нэр",
                     "company name", "entity name", "company", "компани", "нэр", "name"],
    "sector": ["салбар", "үйл ажиллагааны чиглэл", "sector", "industry"],
    "address": ["хаяг", "address"],
    "as_of": ["огноо", "date", "as of", "year"],
}


# ---- reading tables --------------------------------------------------------

def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_table(data: bytes, name: str = "") -> list[dict[str, str]]:
    """Parse CSV / XLSX / JSON bytes into a list of row dicts."""
    lower = name.lower()
    if lower.endswith((".xlsx", ".xlsm")) or data[:2] == b"PK":
        return _read_xlsx(data)
    text = _decode(data).strip()
    if lower.endswith(".json") or text[:1] in "[{":
        return _read_json(text)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [dict(r) for r in csv.DictReader(io.StringIO(text), dialect=dialect)]


def _read_json(text: str) -> list[dict]:
    obj = json.loads(text)
    if isinstance(obj, dict):
        for key in ("records", "data", "result", "results", "items", "rows"):
            if isinstance(obj.get(key), list):
                obj = obj[key]
                break
            if isinstance(obj.get(key), dict) and isinstance(obj[key].get("records"), list):
                obj = obj[key]["records"]
                break
    if not isinstance(obj, list):
        raise ValueError("JSON does not contain a list of records")
    return [{str(k): ("" if v is None else str(v)) for k, v in r.items()} for r in obj if isinstance(r, dict)]


def _read_xlsx(data: bytes) -> list[dict]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = [["" if v is None else str(v) for v in r] for r in ws.iter_rows(values_only=True)]
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return []
    # header = first row with >= 2 non-empty cells (skips title rows above the table)
    hi = next(i for i, r in enumerate(rows) if sum(1 for c in r if c.strip()) >= 2)
    header = [clean(h) or f"col{i}" for i, h in enumerate(rows[hi])]
    return [dict(zip(header, r)) for r in rows[hi + 1:]]


# ---- column mapping ---------------------------------------------------------

def map_columns(headers: list[str]) -> dict[str, str]:
    """Map target fields -> source header, using keyword matching."""
    norm = {h: clean(h).lower() for h in headers}
    used: set[str] = set()
    out: dict[str, str] = {}
    for field, words in COLUMNS.items():
        for w in words:  # prefer earlier (more specific) keywords
            hit = next((h for h, n in norm.items() if h not in used and (n == w or w in n)), None)
            if hit:
                out[field] = hit
                used.add(hit)
                break
    return out


def detect_kind(cols: dict[str, str]) -> str:
    if "holder_name" in cols:
        return "shareholders"
    if "person_name" in cols or "title" in cols:
        return "executives"
    return "companies"


# ---- company resolution -----------------------------------------------------

class CompanyResolver:
    """Resolve company name / reg no. to an MSE ticker, or mint an unlisted one."""

    def __init__(self, lookup: dict[str, str] | None = None):
        self.lookup = dict(lookup or {})          # alias key or "reg:<no>" -> ticker
        self.new: dict[str, Company] = {}

    def resolve(self, name: str = "", reg_no: str = "", ticker: str = "", **extra) -> str | None:
        name, reg_no, ticker = clean(name), re.sub(r"\s", "", clean(reg_no)), clean(ticker).upper()
        if ticker:
            return ticker
        if reg_no and f"reg:{reg_no}" in self.lookup:
            return self.lookup[f"reg:{reg_no}"]
        if name and entity_key(name) in self.lookup:
            return self.lookup[entity_key(name)]
        if not (name or reg_no):
            return None
        t = "~" + (reg_no or hashlib.sha1(entity_key(name).encode()).hexdigest()[:10])
        if t not in self.new:
            self.new[t] = Company(ticker=t, name=name or reg_no, reg_no=reg_no, listed=False,
                                  sector=clean(extra.get("sector", "")),
                                  address=clean(extra.get("address", "")))
        if reg_no:
            self.lookup[f"reg:{reg_no}"] = t
        if name:
            self.lookup[entity_key(name)] = t
        return t


def import_rows(rows: list[dict], kind: str = "auto", source: str = "opendatalab",
                lookup: dict[str, str] | None = None, columns: dict[str, str] | None = None) -> Dataset:
    if not rows:
        return Dataset()
    cols = {**map_columns(list(rows[0].keys())), **(columns or {})}
    kind = detect_kind(cols) if kind == "auto" else kind
    log.info("kind=%s columns=%s", kind, cols)
    resolver = CompanyResolver(lookup)
    ds = Dataset()

    def v(row, field):
        return clean(row.get(cols[field], "")) if field in cols else ""

    for row in rows:
        company = resolver.resolve(name=v(row, "company_name"), reg_no=v(row, "company_reg"),
                                   ticker=v(row, "ticker"), sector=v(row, "sector"),
                                   address=v(row, "address"))
        if kind == "companies":
            continue  # resolver already collected unlisted companies
        if not company:
            continue
        if kind == "shareholders":
            holder = v(row, "holder_name")
            if not holder or is_aggregate(holder):
                continue
            htype = v(row, "holder_type").lower()
            htype = ("organization" if any(w in htype for w in ("хуулийн", "аж ахуй", "org", "company", "legal"))
                     else "person" if any(w in htype for w in ("иргэн", "хувь хүн", "person", "individual"))
                     else "")
            ds.shareholdings.append(Shareholding(
                company, holder, parse_percent(v(row, "percent")), parse_int(v(row, "shares")),
                htype, v(row, "as_of"), source))
        else:
            person = v(row, "person_name") or v(row, "holder_name")
            if not person:
                continue
            title = v(row, "title")
            role = "board" if re.search(r"туз|төлөөлөн удирдах|board|director|гишүүн", title, re.I) and \
                not re.search(r"гүйцэтгэх|ерөнхий захирал|executive|ceo", title, re.I) else "executive"
            ds.positions.append(Position(company, person, title, role, source=source))

    ds.companies = list(resolver.new.values())
    return ds


# ---- remote discovery -------------------------------------------------------

def _fetcher():
    from .scraper import Fetcher
    return Fetcher()


def ckan_api_base() -> str:
    return os.getenv("ODL_CKAN_API", settings.odl_base_url.rstrip("/") + "/api/3/action")


def search(query: str = "", rows: int = 50, fetcher=None) -> list[dict]:
    """Datasets matching `query`, each with its downloadable resources."""
    fetcher = fetcher or _fetcher()
    api = ckan_api_base()
    raw = fetcher.get(f"{api}/package_search?" + urlencode({"q": query, "rows": rows}), use_cache=False)
    if raw:
        try:
            res = json.loads(raw)["result"]["results"]
            return [{
                "id": d.get("name") or d.get("id"),
                "title": d.get("title", ""),
                "organization": (d.get("organization") or {}).get("title", ""),
                "resources": [{"name": r.get("name") or "", "format": (r.get("format") or "").lower(),
                               "url": r.get("url")} for r in d.get("resources", []) if r.get("url")],
            } for d in res]
        except (ValueError, KeyError, TypeError):
            log.info("CKAN API not available at %s, falling back to link scan", api)
    return _scan_links(settings.odl_base_url, query, fetcher)


def _scan_links(base: str, query: str, fetcher) -> list[dict]:
    from bs4 import BeautifulSoup

    pages = [base, urljoin(base + "/", "dataset?" + urlencode({"q": query})), urljoin(base + "/", "data")]
    out, seen = [], set()
    for page in pages:
        html = fetcher.get(page, use_cache=False)
        if not html:
            continue
        for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
            url = urljoin(page, a["href"])
            if url in seen or not DATA_EXT_RE.search(url):
                continue
            title = clean(a.get_text(" ")) or Path(urlsplit(url).path).name
            if query and query.lower() not in (title + url).lower():
                continue
            seen.add(url)
            out.append({"id": url, "title": title, "organization": "",
                        "resources": [{"name": title, "format": DATA_EXT_RE.search(url).group(1).lower(),
                                       "url": url}]})
    return out


def load_source(src: str, fetcher=None) -> tuple[bytes, str]:
    """Read a dataset file from a local path or URL."""
    if re.match(r"https?://", src):
        data = (fetcher or _fetcher()).get_bytes(src)
        if data is None:
            raise RuntimeError(f"could not download {src}")
        return data, urlsplit(src).path
    p = Path(src)
    return p.read_bytes(), p.name


def import_table(src: str, kind: str = "auto", lookup: dict[str, str] | None = None,
                 columns: dict[str, str] | None = None, fetcher=None) -> Dataset:
    data, name = load_source(src, fetcher)
    rows = read_table(data, name)
    return import_rows(rows, kind=kind, source=f"opendatalab:{Path(name).name or src}",
                       lookup=lookup, columns=columns)


DEFAULT_QUERIES = ["эцсийн өмчлөгч", "хувьцаа эзэмшигч", "beneficial ownership", "shareholder",
                   "хуулийн этгээд", "удирдлага", "компани"]


def auto_import(queries: list[str] | None = None, lookup: dict[str, str] | None = None,
                fetcher=None) -> tuple[Dataset, list[dict]]:
    """Search Open Data Lab and import every table that looks like ownership/officer data.

    A resource is imported only if its columns map to a company (name, reg no.
    or ticker) AND to holders or officers; anything else is skipped and
    reported, so unrelated datasets never end up in the graph.
    """
    fetcher = fetcher or _fetcher()
    lookup = dict(lookup or {})
    seen: set[str] = set()
    report: list[dict] = []
    out = Dataset()
    for q in queries or DEFAULT_QUERIES:
        for d in search(q, fetcher=fetcher):
            for r in d["resources"]:
                url = r["url"]
                if url in seen or (r["format"] and r["format"] not in ("csv", "xlsx", "xls", "json")):
                    continue
                seen.add(url)
                entry = {"dataset": d["title"], "url": url, "status": "", "rows": 0}
                report.append(entry)
                data = fetcher.get_bytes(url)
                if data is None:
                    entry["status"] = "download failed"
                    continue
                try:
                    rows = read_table(data, urlsplit(url).path)
                except Exception as exc:  # unreadable / unsupported file
                    entry["status"] = f"unreadable: {exc.__class__.__name__}"
                    continue
                if not rows:
                    entry["status"] = "empty"
                    continue
                cols = map_columns(list(rows[0]))
                kind = detect_kind(cols)
                has_company = any(k in cols for k in ("company_name", "company_reg", "ticker"))
                if kind == "companies" or not has_company:
                    entry["status"] = f"skipped ({kind}, columns: {sorted(cols)})"
                    continue
                ds = import_rows(rows, kind=kind, source=f"opendatalab:{d['title'] or url}", lookup=lookup)
                for c in ds.companies:            # later files resolve to the same unlisted nodes
                    lookup.setdefault(entity_key(c.name), c.ticker)
                    if c.reg_no:
                        lookup.setdefault(f"reg:{c.reg_no}", c.ticker)
                out.extend(ds)
                entry["status"], entry["rows"] = f"imported {kind}", len(rows)
    return out, report
