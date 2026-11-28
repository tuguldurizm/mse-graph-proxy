"""Open Data Lab Mongolia (www.opendatalab.mn) as a source on top of mse.mn.

Open Data Lab republishes the state legal-entity registry (УБЕГ): for every
registered company it has a page at /search/<registration number> with the
registered shareholders, officers, ultimate (beneficial) owners, licences and
tenders. This module:

* `fetch_registry_page()` / `parse_registry_page()` - download and parse one
                       registry page (layout verified against the live site);
* `enrich_companies()` - for each MSE-listed company with a registration
                       number, turn its registry page into graph records:
                       registry shareholders (no %), officers, beneficial
                       owners and company facts;
* `import_table()`   - import a downloaded CSV/XLSX/JSON table, mapping its
                       columns to companies / shareholders / officers by
                       header keywords (Mongolian or English).

Registry data supplements mse.mn and never overwrites it: the loader keeps
MSE's ownership percentages and only fills company properties that are empty.
Registry entries can be years out of date (a listed JSC often still shows its
founders), so every record carries its registry URL as `source`.

Table rows are matched to MSE-listed companies by registration number or name.
Companies that aren't listed on MSE become *unlisted* :Company nodes
(listed=false, ticker "~<reg no>").
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

from .config import settings
from .models import BeneficialOwner, Company, CompanyFacts, Dataset, Position, Shareholding
from .normalize import clean, entity_key, is_aggregate, is_organization, parse_activity, parse_int, parse_percent

log = logging.getLogger(__name__)

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


# ---- dataset files ----------------------------------------------------------

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


def _fetcher():
    from .scraper import Fetcher
    return Fetcher()


# ---- company registry pages (www.opendatalab.mn/search/<reg no>) -------------
#
# Each registered legal entity has a page at /search/<registration number> made
# of Bootstrap cards, one per section, each titled by an <h5>:
#
#   Үндсэн мэдээлэл               label/value rows: Нэр, Хэлбэр, Регистер, Үүсгэн байгуулагдсан, Хаяг, ...
#   Хувь эзэмшил                  shareholders: Д/д | Төрөл (Иргэн / Хуулийн этгээд) | Улс | Нэр | Огноо | flag
#   Удирдах албан тушаалтан       officers:     Д/д | Албан тушаал | Улс | Нэр | Огноо | flag
#   Үйл ажиллагаа                 activity codes (a <table>)
#   Эцсийн өмчлөгч                ultimate owners: Дугаар | Ангилал (Иргэн or a reg. no.) | Нэр | Бүртгэсэн
#   Тусгай зөвшөөрөл              special licences: Дугаар | Код | Нэр | Хүчинтэй (from / to) | Олгосон
#   Оролцсон тендер               public tenders: Дугаар | Нэр (a link) | Төлөв (Шалгарсан = won,
#                                 Хасагдсан = rejected, Илгээгдсэн = submitted, ...)
#   Хамаарал бүхий нийтийн ...    public officials whose asset and income declarations (ХОМ) mention
#                                 the company: Дугаар | Албан тушаал, нэр ("<agency> <title> Ц.Нэр",
#                                 one cell) | ХОМ огноо (the year, a link to the declaration)
#
# The last three are read into RegistryLists (officials.py, issue #9). A tender's "Хасагдсан"
# status is a rejected bid, not a removed row.
#
# Rows flagged "(хасагдсан)" were removed from the registry; they are kept in
# the parsed page but not imported. The registry has no ownership percentages.
# robots.txt asks for a 10 s crawl delay and disallows /search?query=... and
# ?page= listings. The owner has an agreement with Open Data Lab that this project may
# ignore it: the client reads these per-company pages and, for the holder search
# (holder_search.py), name searches and record pages, one request every 11 s.

SECTIONS = {
    "info": ["үндсэн мэдээлэл"],
    "shareholders": ["хувь эзэмшил", "хувь ззэмшил"],     # the site spells it "ззэмшил"
    "officers": ["удирдах албан тушаалтан"],
    "activities": ["үйл ажиллагаа"],
    "beneficial_owners": ["эцсийн өмчлөгч"],
    "licenses": ["тусгай зөвшөөрөл"],
    "tenders": ["оролцсон тендер"],
    "officials": ["хамаарал бүхий нийтийн албан тушаалтан"],
}
INFO_FIELDS = {
    "нэр": "name", "хэлбэр": "legal_form", "төрөл": "kind", "регистер": "reg_no", "регистр": "reg_no",
    "үүсгэн байгуулагдсан": "founded", "хаяг": "address", "шинэчлэгдсэн огноо": "updated",
}
REMOVED = "хасагдсан"
_REG_RE = re.compile(r"^\d{6,8}$")
REGISTER_NO_RE = r"\d{6,8}"     # the same rule, for Cypher's =~ (always a full match)


def is_register_no(number: str | None) -> bool:
    """Whether `number` can be a register number, the 7-digit kind Open Data Lab pages
    use. mse.mn's 10-digit state registration numbers (APU: 9010001005) never are."""
    return bool(_REG_RE.match(re.sub(r"\s", "", number or "")))
_EMPTY_DATE = {"0001-01-01", "0000-00-00"}
# Registry spellings of the state as a shareholder ("Өмч Төр" is typed Иргэн in the source).
_STATE_NAMES = {"өмч төр", "төр өмч", "төр", "төрийн өмч"}


def registry_url(reg_no: str) -> str:
    return settings.odl_base_url.rstrip("/") + "/search/" + re.sub(r"\D", "", reg_no)


def _section_key(title: str) -> str | None:
    t = clean(title).lower()
    for key, names in SECTIONS.items():
        if any(t == n or t.startswith(n) for n in names):
            return key
    return None


def _grid_rows(body) -> list[dict]:
    """Rows of a card: either a <table> or Bootstrap div.row/div.col-* grid."""
    from bs4 import Tag

    table = body.find("table")
    if table is not None:
        header = [clean(th.get_text(" ")).lower() for th in table.find_all("th")]
        out = []
        for tr in table.find_all("tr"):
            cells = [clean(td.get_text(" ")) for td in tr.find_all("td")]
            if cells:
                out.append({**dict(zip(header, cells)), "_removed": False, "_links": _links(tr)})
        return out
    header: list[str] = []
    out = []
    for row in body.find_all("div", class_="row"):
        cols = [c for c in row.children if isinstance(c, Tag) and
                any(cl.startswith("col") for cl in c.get("class", []))]
        if not cols:
            continue
        texts = [clean(c.get_text(" ")) for c in cols]
        if not header:
            if any("font-weight-bold" in c.get("class", []) for c in cols):
                header = [t.lower() or f"_col{i}" for i, t in enumerate(texts)]
            continue
        rec = dict(zip(header, texts))
        rec["_removed"] = any(REMOVED in t.lower() for t in texts)
        rec["_links"] = _links(row)
        out.append(rec)
    return out


def _links(tag) -> list[str]:
    """The row's links, made absolute (an official's declaration, a tender's page)."""
    base = settings.odl_base_url.rstrip("/")
    return [a["href"] if a["href"].startswith("http") else base + "/" + a["href"].lstrip("/")
            for a in tag.find_all("a", href=True)]


def parse_registry_page(html: str) -> dict:
    """Parse one /search/<reg no> page into {"info": {...}, "<section>": [rows], ...}."""
    from bs4 import BeautifulSoup, Tag

    soup = BeautifulSoup(html, "lxml")
    page: dict = {"info": {}}
    for h in soup.find_all("h5"):
        key = _section_key(h.get_text(" "))
        if not key:
            continue
        body = h.find_parent("div", class_="card-body") or h.parent
        if key == "info":
            for row in body.find_all("div", class_="row"):
                cols = [c for c in row.children if isinstance(c, Tag)]
                if len(cols) == 2:
                    label = clean(cols[0].get_text(" ")).lower()
                    if label in INFO_FIELDS:
                        page["info"][INFO_FIELDS[label]] = clean(cols[1].get_text(" "))
        else:
            page[key] = _grid_rows(body)
    return page


def _get(rec: dict, *names: str) -> str:
    for n in names:
        for k, v in rec.items():
            if k == n or k.startswith(n):
                return v
    return ""


def _org_type(text: str) -> str:
    """The organisation type a registry "төрөл" cell states, if any (normalize.ORG_TYPES)."""
    t = text.lower()
    if "гадаад" in t:
        return "foreign"
    if re.search(r"(?<!\w)төр(ийн)?(?!\w)", t) and "төрийн бус" not in t:   # not an NGO
        return "state"
    return ""


def _holder_type(text: str) -> str:
    t = text.lower()
    # "Х/Этгээд" is how newer registry rows abbreviate "Хуулийн этгээд" (a legal entity)
    if "хуулийн этгээд" in t or "аж ахуй" in t or re.search(r"х\s*[/.]\s*этгээд", t):
        return "organization"
    if "иргэн" in t or "хувь хүн" in t:
        return "person"
    return ""


def _is_group_row(name: str) -> bool:
    return is_aggregate(name) or bool(re.search(r"(?<!\w)бусад(?!\w)", name.lower()))


def registry_activities(page: dict) -> list[str]:
    """The page's current activities as "<code> - <description>", in the registry's order
    (its "Дугаар"; the first is the main activity). Rows without a code are left out."""
    rows = [r for r in page.get("activities", []) if not r.get("_removed")]
    rows.sort(key=lambda r: int(_get(r, "дугаар")) if _get(r, "дугаар").isdigit() else 10 ** 6)
    out = []
    for r in rows:
        code, label = parse_activity(_get(r, "үйл ажиллагааны код", "код"))
        label = re.sub(r"\s*/.*", "", label)       # as scripts/mcs/build_mcs.py
        if code and (item := f"{code} - {label}" if label else code) not in out:
            out.append(item)
    return out


def registry_dataset(page: dict, ticker: str, url: str) -> Dataset:
    """Turn a parsed registry page into graph records for company `ticker`."""
    info = page.get("info", {})
    own_reg = re.sub(r"\D", "", info.get("reg_no", ""))
    ds = Dataset(facts=[CompanyFacts(
        ticker=ticker, reg_no=own_reg, legal_form=info.get("legal_form", ""),
        founded=info.get("founded", ""), address=info.get("address", ""),
        registry_name=info.get("name", ""), registry_updated=info.get("updated", ""), registry_url=url,
        activities="; ".join(registry_activities(page)))])

    for r in page.get("shareholders", []):
        name = _get(r, "нэр")
        if r["_removed"] or not name or _is_group_row(name):
            continue
        kind = _get(r, "төрөл")
        htype, otype = _holder_type(kind), _org_type(kind)
        if name.lower() in _STATE_NAMES:
            name, htype, otype = "Төрийн өмч", "organization", "state"
        ds.shareholdings.append(Shareholding(ticker, name, None, None, htype,
                                             as_of=_get(r, "огноо"), source=url,
                                             org_type=otype if htype == "organization" else ""))

    for r in page.get("officers", []):
        name, title = _get(r, "нэр"), _get(r, "албан тушаал")
        if r["_removed"] or not name:
            continue
        board = re.search(r"туз|төлөөлөн удирдах", title.lower()) and "гүйцэтгэх" not in title.lower()
        ds.positions.append(Position(ticker, name, title, "board" if board else "executive",
                                     since=_get(r, "огноо"), source=url))

    for r in page.get("beneficial_owners", []):
        cat, name = _get(r, "ангилал"), _get(r, "нэр")
        since = _get(r, "бүртгэсэн")
        since = "" if since in _EMPTY_DATE else since
        if not name or r["_removed"]:
            continue
        owner_reg = ""
        if _REG_RE.match(cat):          # a legal entity, listed by its own reg. no.
            trail = re.search(r"\s+(\d{6,8})$", name)
            if cat == own_reg or (trail and trail.group(1) == own_reg):
                continue                # the company itself, or an entity tagged with its number
            name = re.sub(r"\s+\d{6,8}$", "", name)
            otype, owner_reg = "organization", cat
        else:
            otype = _holder_type(cat) or ("organization" if is_organization(name) else "person")
        ds.beneficial_owners.append(BeneficialOwner(ticker, name, otype, since, url, owner_reg_no=owner_reg,
                                                    org_type=_org_type(cat) if otype == "organization" else ""))
    from .officials import registry_lists
    ds.registry_lists.append(registry_lists(page, ticker, url))
    # The page is the registry's complete record of this company: a holder, officer
    # or owner it no longer lists closes the registry's edge (never mse.mn's).
    ds.supplementary = True
    for kind in ("shareholdings", "positions", "beneficial_owners"):
        ds.mark_snapshot(kind, ticker, url)
    return ds


def _entity_page(html: str, reg_no: str) -> dict | None:
    """The parsed page if it is the registry page of `reg_no`, else None
    (unknown number, error page, a block or challenge page, ...)."""
    page = parse_registry_page(html)
    got = re.sub(r"\D", "", page["info"].get("reg_no", ""))
    return page if got == re.sub(r"\D", "", reg_no) else None


def fetch_registry_page(reg_no: str, fetcher=None) -> tuple[dict | None, str]:
    """Download and parse the registry page for `reg_no`. Returns (page or None, url)."""
    url = registry_url(reg_no)
    html = (fetcher or _fetcher()).get(url)
    return (_entity_page(html, reg_no) if html else None), url


def fetch_entity(reg_no: str, fetcher) -> tuple[str, dict | None, str]:
    """(status, parsed page or None, url) for one registry page. The status is "ok",
    "not found" (no such entity: a 404, or an answer that isn't this number's page)
    or "fetch failed" (no answer through any route; `fetcher.last_error` says why)."""
    url = registry_url(reg_no)
    html = fetcher.get(url)
    if html is None and getattr(fetcher, "last_status", None) not in (404, 410):
        return "fetch failed", None, url
    page = _entity_page(html, reg_no) if html else None
    return ("ok" if page else "not found"), page, url


REGISTRY_LEASE = "registry"
REGISTRY_LEASE_MINUTES = 30       # renewed while pages are read, so it lapses only if a run dies


def lease_owner(what: str) -> str:
    """Who holds the registry lease: the job, the machine and the process."""
    import os
    import socket
    return f"{what} on {os.getenv('RENDER_SERVICE_NAME') or socket.gethostname()} (pid {os.getpid()})"


class LeaseLost(RuntimeError):
    """The registry lease expired and another run took it: this run must stop reading opendatalab.mn."""


class RegistryLease:
    """opendatalab.mn is never crawled by two runs at once (the hosted pipeline and the daily
    job both read it). A run `acquire()`s the lease in Neo4j first, calls `keep()` after each
    page (it renews the lease every `renew_every` seconds) and `release()`s it at the end.
    If a renewal finds the lease taken by another run (this one stalled past its expiry),
    `keep()` raises LeaseLost, so the two never go on reading together."""

    def __init__(self, g, what: str, minutes: float = REGISTRY_LEASE_MINUTES, renew_every: float = 300):
        self.g, self.owner, self.minutes, self.renew_every = g, lease_owner(what), minutes, renew_every
        self.holder: dict = {}
        self._renewed = float("-inf")

    def acquire(self) -> bool:
        import time
        self.holder = self.g.claim(REGISTRY_LEASE, self.owner, self.minutes)
        self._renewed = time.monotonic()
        return bool(self.holder.get("ok"))

    def keep(self) -> None:
        import time
        if time.monotonic() - self._renewed >= self.renew_every:
            renewed = self.g.claim(REGISTRY_LEASE, self.owner, self.minutes)
            if not renewed.get("ok"):
                self.holder = renewed
                raise LeaseLost(f"the registry lease expired and {renewed.get('owner')} took it "
                                f"(until {renewed.get('until')}); stopping so two runs never read opendatalab.mn at once")
            self._renewed = time.monotonic()

    def release(self) -> None:
        self.g.release(REGISTRY_LEASE, self.owner)

    def busy_message(self) -> str:
        return (f"another run is reading the registry now ({self.holder.get('owner')}, "
                f"until {self.holder.get('until')})")


# Registry pages in a row that could not be downloaded before a run gives up on the
# remaining companies: the way out is down, and ~200 companies x 11 s x retries of
# nothing would only hold up the pipeline.
MAX_CONSECUTIVE_FAILURES = 5


def enrich_companies(companies: list[tuple[str, str]], fetcher=None, limit: int | None = None,
                     max_failures: int = MAX_CONSECUTIVE_FAILURES, progress=None,
                     retry_failed: bool = True) -> tuple[Dataset, list[dict]]:
    """Fetch the registry page of each (ticker, reg_no) and collect its records.

    Returns the combined Dataset and a per-company report whose status is "ok",
    "not found" (no such entity), "fetch failed" (no answer through any route) or
    "skipped" (not tried: the `max_failures` downloads before it all failed).
    A company without a page gets no records, so nothing of its history is closed.
    Numbers that can't be register numbers (mse.mn's 10-digit state registration
    numbers) are left out: the registry has no page for them. `progress()`, if
    given, is called after each page (e.g. RegistryLease.keep). With `retry_failed`, the
    pages that failed are tried once more at the end, unless the run gave up on an outage.
    """
    fetcher = fetcher or _fetcher()
    out, report = Dataset(), []
    todo = [(t, re.sub(r"\D", "", r or "")) for t, r in companies]
    unusable = [t for t, r in todo if r and not is_register_no(r)]
    if unusable:
        log.info("ODL: %d companies have no register number, only a state registration number the registry "
                 "doesn't use (discovery can find theirs): %s", len(unusable), " ".join(unusable[:20]))
    todo = [(t, r) for t, r in todo if is_register_no(r)]
    todo = todo[:limit] if limit else todo
    failures = 0
    for i, (ticker, reg) in enumerate(todo):
        entry = {"ticker": ticker, "reg_no": reg, "url": registry_url(reg)}
        report.append(entry)
        if failures >= max_failures:
            entry["status"] = "skipped"
            continue
        status, page, url = fetch_entity(reg, fetcher)
        if progress:
            progress()
        entry["status"] = status
        if status == "fetch failed":
            failures += 1
            if getattr(fetcher, "last_error", ""):
                entry["error"] = fetcher.last_error
            if failures == max_failures and i + 1 < len(todo):
                log.error("ODL: %d registry pages in a row could not be downloaded; skipping the other %d "
                          "companies this run. Check the route: python -m mse_graph.cli egress-check",
                          failures, len(todo) - i - 1)
        else:
            failures = 0
            if page is not None:
                ds = registry_dataset(page, ticker, url)
                out.extend(ds)
                entry.update(shareholders=len(ds.shareholdings), officers=len(ds.positions),
                             beneficial_owners=len(ds.beneficial_owners))
        log.info("ODL [%d/%d] %s %s: %s", i + 1, len(todo), ticker, reg, entry["status"])
    # Once more for the pages that failed (a reset connection, a proxy hiccup), at the end
    # so the route has had time to recover; the crawl delay still applies.
    # Not after an outage (companies skipped): then the route is down and tomorrow is soon enough.
    again = [e for e in report if e["status"] == "fetch failed"] if retry_failed else []
    if again and not any(e["status"] == "skipped" for e in report):
        log.info("ODL: retrying %d companies that failed", len(again))
        failures = 0
        for i, entry in enumerate(again):
            if failures >= max_failures:
                break
            status, page, url = fetch_entity(entry["reg_no"], fetcher)
            if progress:
                progress()
            entry["status"], entry["retried"] = status, True
            entry.pop("error", None)
            if status == "fetch failed":
                failures += 1
                if getattr(fetcher, "last_error", ""):
                    entry["error"] = fetcher.last_error
            else:
                failures = 0
                if page is not None:
                    ds = registry_dataset(page, entry["ticker"], url)
                    out.extend(ds)
                    entry.update(shareholders=len(ds.shareholdings), officers=len(ds.positions),
                                 beneficial_owners=len(ds.beneficial_owners))
            log.info("ODL retry [%d/%d] %s %s: %s", i + 1, len(again), entry["ticker"], entry["reg_no"], status)
    return out, report


def check_egress(reg_no: str, fetcher=None) -> list[dict]:
    """Fetch the registry page of `reg_no` through each opendatalab.mn route on its
    own (no cache, no retries, no failover; crawl delay kept) and report per route
    whether the real page came back."""
    fetcher = fetcher or _fetcher()
    url = registry_url(reg_no)
    results = []
    for route in fetcher.router.routes_for(url):
        a = fetcher.try_route(route, url)
        res = {"route": route.label, "status": a.status, "seconds": a.seconds, "error": a.error}
        res["ok"] = a.step == "ok" and _entity_page(a.response.text, reg_no) is not None
        if a.step == "ok" and not res["ok"]:
            res["error"] = "answered, but not with the registry page (unknown number, or a block page?)"
        results.append(res)
    return results
