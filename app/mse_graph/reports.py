"""Annual reports and other company documents (issue #10, "More sources": annual-report PDFs).

Source: each listed company's profile on open.mse.mn has four document tabs the company scrape
never read (their slugs from the profile page, 2026-11-24):

    tab/operations   "Үйл ажиллагааны тайлан"   activity reports (annual, half-year)
    tab/financials   "Санхүүгийн мэдээлэл"      financial statements, audit reports
    tab/meetings     "Хурлын мэдээлэл"          shareholder meetings
    tab/otherinfo    "Бусад мэдээлэл"           everything else

Step 1 (this module) does three things, all bounded and all logged:

1. **Index.** It reads the four tabs of REPORTS_BATCH (25) listed companies a run, the ones read
   longest ago first, so every company is read again about once a week (4 requests a company, through
   the scraper's Fetcher: the same egress, crawl delay and page cache as the company scrape). Every
   link to a document (a .pdf / .doc / .xls / ... path, or a storage / upload / download path) becomes
   a `(:Report)` node, `(:Company)-[:PUBLISHED]->(:Report)`, keyed by its URL, with its title (the
   link's text, else its row's), year, period (annual / half / quarter), kind (`KINDS`), the tab, and
   first_seen / last_seen. A document a company no longer lists keeps its node with `current = false`.
   The tabs' layout was never seen by a session (cloud sessions can't reach mse.mn), so the parser
   reads any link and REPORTS_LAYOUT_LOG (4) tabs a run are described in `REPORTS layout` lines.
2. **Read.** Up to REPORTS_PDF_BUDGET (4) PDFs a run, each company's newest unread annual report or
   financial statement first, are read with pdfplumber: the first REPORTS_PDF_MAX_PAGES (80) pages, at
   most REPORTS_PDF_MAX_MB (25) MB, not kept on disk. Stored on the Report: `pages`, `text_pages` and
   `scanned` (almost no text layer: a scan, which this step does not OCR), and `sections`, the pages
   where the shareholders, subsidiaries, associates, board, related parties and the auditor's report
   are named.
3. **Findings.** On those pages, a line naming an **organisation** (a legal form: ХХК, ХК, LLC, ...)
   with a percent is a finding: `subsidiary`, `associate` or `shareholder`, with the page and the line,
   stored as JSON on the Report (`findings`). They are **unchecked**: nothing is written as a stake,
   and no people are read (as with FRC, names in reports are mostly initial-form). The UI and the
   dossier show them as "from the annual report, not checked"; a later step can turn confirmed ones
   into stakes once real reports have been looked at.

The pipeline runs it (`pipeline.reports_step`) at most every REPORTS_MIN_INTERVAL_HOURS (20);
REPORTS_ENABLE=0 turns it off. `cli reports` runs or summarises it by hand.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
from collections import Counter
from datetime import date, datetime, timezone
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .normalize import clean, parse_date

log = logging.getLogger(__name__)

STEP_VERSION = 1

# tab slug -> the kind a document there has when its title says nothing
TABS = {"operations": "annual_report", "financials": "financial_statement",
        "meetings": "meeting", "otherinfo": "other"}

KINDS = {
    "annual_report": "Үйл ажиллагааны тайлан",
    "financial_statement": "Санхүүгийн тайлан",
    "audit": "Аудитын тайлан",
    "meeting": "Хурлын материал",
    "governance": "Засаглалын баримт бичиг",
    "esg": "Тогтвортой байдлын тайлан",
    "other": "Бусад",
}
# kinds whose PDFs are read for findings, in this order
READ_KINDS = ("annual_report", "financial_statement", "audit")

# title words -> kind, checked in this order (an activity report often says "санхүү" too)
_KIND_RULES = [
    ("audit", re.compile(r"аудит|audit", re.I)),
    ("esg", re.compile(r"тогтвортой|байгаль орчин|\besg\b|sustainab", re.I)),
    ("governance", re.compile(r"засаглал|governance|дүрэм|журам|charter|bylaw", re.I)),
    ("meeting", re.compile(r"хурал|хурлын|meeting|agenda|хэлэлцэх асуудал", re.I)),
    ("annual_report", re.compile(r"үйл ажиллагаа|жилийн тайлан|annual report|operations report", re.I)),
    ("financial_statement", re.compile(r"санхүү|financial|баланс|balance sheet", re.I)),
]

DOC_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar")
_DOC_PATH = re.compile(r"/(storage|uploads?|files?|documents?|attachments?|media|download)s?/|download|getfile", re.I)
# link texts that say nothing about the document
_GENERIC = re.compile(r"^(татах|үзэх|харах|дэлгэрэнгүй|файл|download|view|open|pdf|more|татаж авах|энд дарна уу)\W*$",
                      re.I)
_GENERIC_WORDS = re.compile(r"\b(татах|үзэх|харах|дэлгэрэнгүй|download|view)\b", re.I)
_YEAR = re.compile(r"(?<!\d)(19[89]\d|20[0-4]\d)(?!\d)")
_DATE = re.compile(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}")
_HALF = re.compile(r"хагас|half|semi|6 сар", re.I)
_QUARTER = re.compile(r"улира?л|quarter|\bq[1-4]\b", re.I)
_ANNUAL = re.compile(r"жилийн|annual|\bжил\b|year", re.I)


def batch_size() -> int:
    return int(os.getenv("REPORTS_BATCH", "25"))


def pdf_budget() -> int:
    return int(os.getenv("REPORTS_PDF_BUDGET", "4"))


def max_pages() -> int:
    return int(os.getenv("REPORTS_PDF_MAX_PAGES", "80"))


def max_bytes() -> int:
    return int(float(os.getenv("REPORTS_PDF_MAX_MB", "25")) * 1024 * 1024)


# ---- parsing a tab ---------------------------------------------------------------------------

def _is_document(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False
    path = parts.path.lower()
    if "/securities/" in path or "/news/" in path:
        return False
    return path.endswith(DOC_EXT) or bool(_DOC_PATH.search(path + "?" + parts.query))


def _container_text(a) -> str:
    """The text of the row a link sits in (a table row, a list item, a card), if it is short."""
    for parent in a.parents:
        if parent.name in ("tr", "li") or any(c in " ".join(parent.get("class") or [])
                                               for c in ("card", "item", "row", "list-group")):
            text = clean(parent.get_text(" "))
            return text if len(text) <= 400 else ""
        if parent.name in ("table", "ul", "body"):
            break
    return ""


def period_of(text: str) -> str:
    if _QUARTER.search(text):
        return "quarter"
    if _HALF.search(text):
        return "half"
    if _ANNUAL.search(text):
        return "annual"
    return ""


def kind_of(text: str, tab: str) -> str:
    for kind, rule in _KIND_RULES:
        if rule.search(text):
            return kind
    return TABS.get(tab, "other")


def parse_tab(html: str, url: str, tab: str) -> list[dict]:
    """The documents a tab links to: [{url, title, year, period, kind, tab, ext, date}], each URL once."""
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"].strip()).split("#")[0]
        if href in seen or not _is_document(href):
            continue
        seen.add(href)
        own = clean(a.get_text(" ") or a.get("title") or "")
        row = _container_text(a)
        title = own if own and not _GENERIC.match(own) else clean(_GENERIC_WORDS.sub(" ", row)) or own
        if not title:
            title = urlsplit(href).path.rsplit("/", 1)[-1]
        title = title[:300]
        context = f"{title} {row}"
        years = _YEAR.findall(title) or _YEAR.findall(row) or _YEAR.findall(href)
        day = _DATE.search(row)
        path = urlsplit(href).path.lower()
        ext = next((e.lstrip(".") for e in DOC_EXT if path.endswith(e)), "")
        out.append({"url": href, "title": title, "year": int(years[0]) if years else None,
                    "period": period_of(context), "kind": kind_of(title or context, tab), "tab": tab,
                    "ext": ext, "date": parse_date(day.group(0)) if day else None,
                    "host": urlsplit(href).hostname or ""})
    return out


def describe(html: str, url: str) -> dict:
    """The shape of a tab, for the `REPORTS layout` log lines (the layout is unverified)."""
    soup = BeautifulSoup(html, "lxml")
    links = soup.find_all("a", href=True)
    docs = parse_tab(html, url, "")
    hosts = Counter(urlsplit(urljoin(url, a["href"])).hostname or "" for a in links)
    tables = soup.find_all("table")
    return {"url": url, "bytes": len(html), "tables": len(tables),
            "headers": [[clean(th.get_text(" "))[:30] for th in t.find_all("th")][:8] for t in tables[:3]],
            "links": len(links), "documents": len(docs), "hosts": dict(hosts.most_common(5)),
            "exts": dict(Counter(d["ext"] or "?" for d in docs)),
            "sample": [{"title": d["title"][:80], "year": d["year"], "url": d["url"][-80:]} for d in docs[:4]],
            "text": clean(soup.get_text(" "))[:200] if not docs else ""}


# ---- reading a PDF ---------------------------------------------------------------------------

SECTIONS = {
    "subsidiaries": re.compile(r"охин\s+(компани|байгууллага|үйлдвэр)|subsidiar", re.I),
    "associates": re.compile(r"хараат\s+(компани|байгууллага)|хамтарсан\s+(компани|үйлдвэр)|associate|joint venture",
                             re.I),
    "shareholders": re.compile(r"хувьцаа\s+эзэмшигч|хувь\s+нийлүүлэгч|shareholder|stockholder", re.I),
    "board": re.compile(r"төлөөлөн\s+удирдах\s+зөвлөл|\bТУЗ\b|board of directors", re.I),
    "related_parties": re.compile(r"холбоотой\s+тал|related\s+part", re.I),
    "auditor": re.compile(r"аудитор|auditor|аудитын\s+дүгнэлт", re.I),
}
# the sections whose lines are read for findings, and the finding's kind
_FINDING_KIND = {"subsidiaries": "subsidiary", "associates": "associate", "shareholders": "shareholder"}

_LEGAL = r"(?:ХХК|ХК|ТӨХК|ТӨҮГ|ОНӨҮГ|ББСБ|ҮЦК|ХЗХ|ХХМТК|LLC|JSC|PLC|Ltd\.?|LTD|Inc\.?|Corp\.?|Co\.,?\s*Ltd\.?|GmbH|AG|S\.A\.|B\.V\.)"
_ORG = re.compile(r"([\"«“„']?[A-Za-zА-Яа-яӨөҮүЁё0-9][\w\-.&\s\"«»“”„']{1,70}?\s*[\"»”']?\s*" + _LEGAL + r")(?![\w])")
_PCT = re.compile(r"(?<![\d.,])(\d{1,3}(?:[.,]\d{1,4})?)\s*%")
# a section's lines are read until this many lines pass without a finding
SECTION_SPAN = 60
MAX_FINDINGS = 40


def _org_name(line: str) -> str | None:
    m = None
    for m in _ORG.finditer(line):
        pass                      # the last organisation before the percent is usually the holder
    if not m:
        return None
    name = clean(re.sub(r"^\W*\d+[.)]?\s*", "", m.group(1)))
    name = re.sub(r"^(?:нь|болон|ба|and|of|the)\s+", "", name, flags=re.I)
    return name if len(name) >= 4 else None


def analyse_text(pages: list[str], total_pages: int | None = None) -> dict:
    """Sections and findings in a report's page texts (no PDF needed; tests feed text)."""
    sections: dict[str, list[int]] = {}
    findings, seen = [], set()
    current, quiet = None, 0
    text_pages = 0
    for n, text in enumerate(pages, start=1):
        if len(clean(text)) >= 50:
            text_pages += 1
        for name, rule in SECTIONS.items():
            if rule.search(text) and n not in sections.setdefault(name, []):
                sections[name].append(n)
        for line in text.splitlines():
            line = clean(line)
            if not line:
                continue
            for name in _FINDING_KIND:
                if SECTIONS[name].search(line):
                    current, quiet = name, 0
                    break
            if current is None:
                continue
            quiet += 1
            if quiet > SECTION_SPAN:
                current = None
                continue
            pct = _PCT.search(line)
            if not pct:
                continue
            value = float(pct.group(1).replace(",", "."))
            name = _org_name(line[:pct.start()]) or _org_name(line)
            if not name or not 0 < value <= 100:
                continue
            k = (_FINDING_KIND[current], name.lower(), value)
            if k in seen:
                continue
            seen.add(k)
            quiet = 0
            if len(findings) < MAX_FINDINGS:
                findings.append({"kind": _FINDING_KIND[current], "name": name, "percent": value, "page": n,
                                 "line": line[:200]})
    read = len(pages)
    return {"pages": total_pages if total_pages is not None else read, "pages_read": read,
            "text_pages": text_pages, "scanned": read > 0 and text_pages / read < 0.3,
            "sections": {k: v[:10] for k, v in sections.items() if v}, "findings": findings}


def analyse_pdf(data: bytes, page_limit: int | None = None) -> dict:
    """Read a PDF's first pages with pdfplumber and analyse their text."""
    import pdfplumber

    page_limit = page_limit or max_pages()
    texts = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        total = len(pdf.pages)
        for page in pdf.pages[:page_limit]:
            try:
                texts.append(page.extract_text() or "")
            except Exception as exc:  # a broken page must not lose the rest
                log.debug("pdf page: %s", exc)
                texts.append("")
            close = getattr(page, "close", None)
            if close:
                close()
    return analyse_text(texts, total)


# ---- the graph -----------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def companies_due(g, limit: int) -> list[dict]:
    """Listed companies with a profile page, the ones whose tabs were read longest ago first."""
    return g.run(
        """
        MATCH (c:Company) WHERE coalesce(c.listed, true) AND NOT c.ticker STARTS WITH '~'
              AND (c.url STARTS WITH 'http' OR c.mse_id IS NOT NULL)
        RETURN c.key AS key, c.ticker AS ticker, c.url AS url, c.mse_id AS mse_id
        ORDER BY coalesce(c.reports_read_at, '') , c.ticker LIMIT $n
        """, n=limit)


def write_company(g, key: str, docs: list[dict], when: str) -> dict:
    """Store one company's documents; those it no longer lists stay, with current = false."""
    got = g.run(
        """
        MATCH (c:Company {key: $key})
        OPTIONAL MATCH (c)-[:PUBLISHED]->(old:Report) WHERE old.current AND NOT old.url IN $urls
        SET old.current = false, old.gone_at = $when
        WITH DISTINCT c
        SET c.reports_read_at = $when
        WITH c
        UNWIND $docs AS d
        MERGE (r:Report {url: d.url})
        ON CREATE SET r.first_seen = $when, r.new = true
        ON MATCH SET r.new = false
        SET r.title = d.title, r.year = d.year, r.period = d.period, r.kind = d.kind, r.tab = d.tab,
            r.ext = d.ext, r.date = d.date, r.host = d.host, r.last_seen = $when, r.current = true,
            r.source = 'mse.mn', r.gone_at = null
        MERGE (c)-[:PUBLISHED]->(r)
        RETURN count(r) AS n, sum(CASE WHEN r.new THEN 1 ELSE 0 END) AS added
        """, key=key, docs=docs, urls=[d["url"] for d in docs], when=when)
    n = got[0] if got else {"n": 0, "added": 0}
    return {"documents": n["n"] or 0, "added": n["added"] or 0}


def index(g, fetcher=None, limit: int | None = None, when: str | None = None,
          companies: list[dict] | None = None, layout: int | None = None) -> dict:
    """Read the document tabs of the companies due and store what they link to."""
    from .scraper import OPEN_BASE, Fetcher

    fetcher = fetcher or Fetcher()
    when = when or date.today().isoformat()
    layout = int(os.getenv("REPORTS_LAYOUT_LOG", "4")) if layout is None else layout
    companies = companies if companies is not None else companies_due(g, limit or batch_size())
    report = {"companies": 0, "tabs_read": 0, "tabs_failed": 0, "documents": 0, "added": 0,
              "kinds": Counter(), "errors": []}
    described = 0
    for c in companies:
        base = c.get("url") if (c.get("url") or "").startswith("http") else \
            (f"{OPEN_BASE}/securities/{c['mse_id']}" if c.get("mse_id") else "")
        if not base:
            continue
        docs, read_any = [], False
        for tab in TABS:
            url = f"{base.rstrip('/')}/tab/{tab}"
            html = fetcher.get(url)
            if html is None:
                report["tabs_failed"] += 1
                if len(report["errors"]) < 10:
                    report["errors"].append(f"{c['ticker']} {tab}: {fetcher.last_error or fetcher.last_status}")
                continue
            read_any = True
            report["tabs_read"] += 1
            if described < layout:
                described += 1
                log.info("REPORTS layout %s %s: %s", c["ticker"], tab,
                         json.dumps(describe(html, url), ensure_ascii=False))
            known = {d["url"] for d in docs}
            docs += [d for d in parse_tab(html, url, tab) if d["url"] not in known]
        if not read_any:
            continue
        cap = int(os.getenv("REPORTS_MAX_PER_COMPANY", "120"))
        docs.sort(key=lambda d: -(d["year"] or 0))
        res = write_company(g, c["key"], docs[:cap], when)
        report["companies"] += 1
        report["documents"] += res["documents"]
        report["added"] += res["added"]
        report["kinds"].update(d["kind"] for d in docs[:cap])
    report["kinds"] = dict(report["kinds"])
    return report


def pdfs_due(g, budget: int) -> list[dict]:
    """Unread documents to read, each company's newest annual report (else financial statement,
    else audit report; a year's annual one before its half-year one) first, then the next of each company."""
    rows = g.run(
        """
        MATCH (c:Company)-[:PUBLISHED]->(r:Report)
        WHERE r.current AND r.pdf_read_at IS NULL AND r.kind IN $kinds AND r.ext IN ['pdf', '']
        RETURN c.key AS key, c.ticker AS ticker, r.url AS url, r.kind AS kind, coalesce(r.year, 0) AS year,
               coalesce(r.period, '') AS period
        """, kinds=list(READ_KINDS))
    rank_kind = {k: i for i, k in enumerate(READ_KINDS)}
    # a year's annual report before its half-year and quarterly ones
    rank_period = {"annual": 0, "": 1, "half": 2, "quarter": 3}
    by_company: dict[str, list[dict]] = {}
    for r in sorted(rows, key=lambda r: (-r["year"], rank_kind[r["kind"]], rank_period.get(r["period"], 1), r["url"])):
        by_company.setdefault(r["key"], []).append(r)
    order = []
    depth = 0
    while len(order) < budget and any(len(v) > depth for v in by_company.values()):
        for key in sorted(by_company, key=lambda k: -by_company[k][0]["year"]):
            if len(by_company[key]) > depth:
                order.append(by_company[key][depth])
        depth += 1
    return order[:budget]


def read_pdf(fetcher, url: str) -> dict:
    """Fetch one document and analyse it; {'error': ...} when that fails."""
    data = fetcher.get_bytes(url, use_cache=False, store=False)
    if data is None:
        return {"error": f"fetch: {fetcher.last_error or fetcher.last_status}"}
    if not data.startswith(b"%PDF"):
        return {"error": "not a PDF", "bytes": len(data)}
    if len(data) > max_bytes():
        return {"error": f"too large ({len(data) // (1024 * 1024)} MB)", "bytes": len(data)}
    try:
        res = analyse_pdf(data)
    except Exception as exc:
        return {"error": f"pdf: {type(exc).__name__}: {exc}"[:200], "bytes": len(data)}
    return {**res, "bytes": len(data)}


def write_pdf(g, url: str, res: dict, when: str) -> None:
    g.run(
        """
        MATCH (r:Report {url: $url})
        SET r.pdf_read_at = $when, r.pdf_version = $v, r.pdf_error = $error, r.bytes = $bytes,
            r.pages = $pages, r.pages_read = $pages_read, r.text_pages = $text_pages, r.scanned = $scanned,
            r.sections = $sections, r.findings = $findings, r.findings_count = $nf
        """, url=url, when=when, v=STEP_VERSION, error=res.get("error"), bytes=res.get("bytes"),
        pages=res.get("pages"), pages_read=res.get("pages_read"), text_pages=res.get("text_pages"),
        scanned=res.get("scanned"),
        sections=[f"{k}:{','.join(map(str, v))}" for k, v in (res.get("sections") or {}).items()],
        findings=json.dumps(res.get("findings") or [], ensure_ascii=False), nf=len(res.get("findings") or []))


def read_pdfs(g, fetcher=None, budget: int | None = None, when: str | None = None) -> dict:
    from .scraper import Fetcher

    fetcher = fetcher or Fetcher()
    when = when or date.today().isoformat()
    report = {"read": 0, "failed": 0, "scanned": 0, "findings": 0, "errors": []}
    for r in pdfs_due(g, pdf_budget() if budget is None else budget):
        res = read_pdf(fetcher, r["url"])
        write_pdf(g, r["url"], res, when)
        if res.get("error"):
            report["failed"] += 1
            if len(report["errors"]) < 5:
                report["errors"].append(f"{r['ticker']}: {res['error']}")
            continue
        report["read"] += 1
        report["scanned"] += bool(res.get("scanned"))
        report["findings"] += len(res.get("findings") or [])
        log.info("REPORTS pdf %s %s: %s", r["ticker"], r["url"][-80:], json.dumps(
            {k: res.get(k) for k in ("pages", "pages_read", "text_pages", "scanned", "sections")} |
            {"findings": (res.get("findings") or [])[:5]}, ensure_ascii=False))
    return report


def hours_since_step(g) -> float | None:
    rows = g.run("MATCH (m:Meta {key: 'reports'}) RETURN m.at AS at")
    if not rows or not rows[0]["at"]:
        return None
    return (datetime.now(timezone.utc) - datetime.fromisoformat(rows[0]["at"])).total_seconds() / 3600


def step(g, fetcher=None, when: str | None = None) -> dict:
    """The pipeline's reports step: index a batch of companies, then read a few PDFs."""
    from .scraper import Fetcher

    fetcher = fetcher or Fetcher()
    when = when or date.today().isoformat()
    res = {"index": index(g, fetcher, when=when)}
    res["pdf"] = read_pdfs(g, fetcher, when=when) if pdf_budget() > 0 else {"read": 0}
    g.run("MERGE (m:Meta {key: 'reports'}) SET m.at = $at, m.version = $v", at=_utc_now(), v=STEP_VERSION)
    res["summary"] = summary(g)
    return res


# ---- reading it back ---------------------------------------------------------------------------

def _row(r: dict) -> dict:
    out = dict(r)
    try:
        out["findings"] = json.loads(r.get("findings") or "[]")
    except (TypeError, ValueError):
        out["findings"] = []
    out["kind_name"] = KINDS.get(r.get("kind") or "", r.get("kind"))
    return out


_FIELDS = """r.url AS url, r.title AS title, r.year AS year, r.period AS period, r.kind AS kind, r.tab AS tab,
             r.ext AS ext, r.date AS date, r.first_seen AS first_seen, r.last_seen AS last_seen,
             r.current AS current, r.pdf_read_at AS pdf_read_at, r.pdf_error AS pdf_error, r.pages AS pages,
             r.scanned AS scanned, r.sections AS sections, r.findings AS findings"""


def company_reports(g, key: str, limit: int = 200) -> dict:
    """A company's documents, newest first, and the findings read from them."""
    rows = [_row(r) for r in g.run(
        f"""MATCH (:Company {{key: $key}})-[:PUBLISHED]->(r:Report)
            RETURN {_FIELDS}
            ORDER BY r.current DESC, coalesce(r.year, 0) DESC, r.date DESC, r.title LIMIT $limit""",
        key=key, limit=limit)]
    findings = [{**f, "report": r["url"], "report_title": r["title"], "year": r["year"]}
                for r in rows for f in r["findings"]]
    read = g.run("MATCH (c:Company {key: $key}) RETURN c.reports_read_at AS at", key=key)
    return {"items": rows, "findings": findings, "read_at": read[0]["at"] if read else None,
            "note": "Findings are lines of the company's own reports that name an organisation with a percent; "
                    "they are not checked and are not used as stakes."}


def findings_report(g, kind: str = "", limit: int = 500) -> dict:
    """Findings across the market (subsidiaries, associates, shareholder organisations), newest first."""
    rows = g.run(
        """MATCH (c:Company)-[:PUBLISHED]->(r:Report) WHERE r.current AND coalesce(r.findings_count, 0) > 0
           RETURN c.ticker AS ticker, c.name AS company, r.url AS url, r.title AS title, r.year AS year,
                  r.findings AS findings
           ORDER BY coalesce(r.year, 0) DESC, c.ticker""")
    out = []
    for r in rows:
        for f in json.loads(r["findings"] or "[]"):
            if kind and f.get("kind") != kind:
                continue
            out.append({**f, "ticker": r["ticker"], "company": r["company"], "report": r["url"],
                        "report_title": r["title"], "year": r["year"]})
    return {"items": out[:limit], "summary": {**summary(g), "listed_findings": len(out)}}


def summary(g) -> dict:
    """Counts for `PIPELINE reports` and the daily job."""
    rows = g.run(
        """
        OPTIONAL MATCH (c:Company) WHERE coalesce(c.listed, true) AND NOT c.ticker STARTS WITH '~'
        WITH count(c) AS listed, sum(CASE WHEN c.reports_read_at IS NOT NULL THEN 1 ELSE 0 END) AS indexed
        OPTIONAL MATCH (:Company)-[:PUBLISHED]->(r:Report) WHERE r.current
        WITH listed, indexed, collect(DISTINCT r) AS rs
        RETURN listed, indexed, size(rs) AS documents,
               size([r IN rs WHERE r.pdf_read_at IS NOT NULL AND r.pdf_error IS NULL]) AS pdfs_read,
               size([r IN rs WHERE r.pdf_error IS NOT NULL]) AS pdfs_failed,
               size([r IN rs WHERE r.scanned]) AS scanned,
               reduce(n = 0, r IN rs | n + coalesce(r.findings_count, 0)) AS findings,
               [r IN rs | r.kind] AS kinds
        """)
    if not rows:
        return {}
    r = dict(rows[0])
    r["kinds"] = dict(Counter(k for k in r.pop("kinds") if k))
    r["with_documents"] = g.run("""MATCH (c:Company)-[:PUBLISHED]->(r:Report) WHERE r.current
                                    RETURN count(DISTINCT c) AS n""")[0]["n"]
    return r
