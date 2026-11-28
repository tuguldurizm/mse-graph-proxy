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
   `scanned` (almost no text layer: a scan, read by step 5), and `sections`, the pages
   where the shareholders, subsidiaries, associates, board, related parties and the auditor's report
   are named.
3. **Findings.** On those pages, a line naming an **organisation** (a legal form: ХХК, ХК, LLC, ...)
   with a percent is a finding: `subsidiary`, `associate` or `shareholder`, with the page and the line,
   stored as JSON on the Report (`findings`). They are **unchecked**: nothing is written as a stake,
   and no people are read (as with FRC, names in reports are mostly initial-form). The UI and the
   dossier show them as "from the annual report, not checked".
4. **Confirmed findings become stakes** (owner, 2026-11-27: "make OWNS links"). Once someone has checked
   a subsidiary or associate finding against the report, `cli reports --confirm URL --finding N` (`confirm`
   below) writes it into the hand-curated dataset `data/manual/annual-reports/` (CONFIRMED_DIR): an
   `OWNS` stake from the publishing company, source `report:<the report's URL>`, percent and year as the
   report gives them. The manual loader does the rest (records, history; deleting the row ends the stake).
   The subsidiary is the graph's company of the same name when there is one (or `--ticker`), else a new
   unlisted company in the dataset's companies.csv. Shareholder findings are not confirmed this way: a listed
   company's owners are the ones mse.mn reports (owner, 2026-11-25).

5. **OCR of scans** (2026-11-28). Most documents read so far are scans (7 of the first 8: no text layer),
   so `read_scans` renders the pages of documents marked `scanned` with pypdfium2 (300 dpi) and reads them
   with Tesseract (REPORTS_OCR_LANG, `mon+eng`), REPORTS_OCR_BUDGET (6) documents and REPORTS_OCR_MAX_PAGES
   (40) pages each, the newest first. The text goes through the same sections and findings as step 3,
   after `fix_ocr_percent` puts back the percent signs Tesseract reads as "96". The findings replace the
   scan's empty ones and carry `"ocr": true` (shown as "OCR"); they are as unchecked as the others, and
   OCR misreads names and numbers, so check them against the PDF before `--confirm`. It needs Tesseract,
   so the daily job runs it (`cli reports --ocr`), not Render's 0.15-CPU service.

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

# 2: titles from the tables' columns, kinds from the upload folder, dividend decisions (first live run, 2026-11-24).
# A new version re-reads every company's tabs before the rotation goes on, and the PDFs read before it.
STEP_VERSION = 2

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
    "dividend": "Ногдол ашгийн шийдвэр",
    "other": "Бусад",
}
# kinds whose PDFs are read for findings, in this order
READ_KINDS = ("annual_report", "financial_statement", "audit")

# the upload folder on issuers.mse.mn says what a file is (seen 2026-11-24: activityreport, finance_reports,
# stockm_dec_files, dividend_files); checked before the words
_PATH_KINDS = [
    ("dividend", re.compile(r"/dividend", re.I)),
    ("audit", re.compile(r"/audit", re.I)),
    ("annual_report", re.compile(r"/activity_?report", re.I)),
    ("financial_statement", re.compile(r"/financ\w*_?report|/finance", re.I)),
    ("esg", re.compile(r"/esg|/sustainab", re.I)),
    ("meeting", re.compile(r"/stockm|/meeting", re.I)),
]
# title words -> kind, checked in this order (an activity report often says "санхүү" too)
_KIND_RULES = [
    ("dividend", re.compile(r"ногдол ашиг|dividend", re.I)),
    ("audit", re.compile(r"аудит|audit", re.I)),
    ("esg", re.compile(r"тогтвортой|байгаль орчин|\besg\b|бонз|sustainab", re.I)),
    ("governance", re.compile(r"засаглал|governance|дүрэм|журам|charter|bylaw", re.I)),
    ("meeting", re.compile(r"хурал|хурлын|meeting|agenda|хэлэлцэх асуудал", re.I)),
    ("annual_report", re.compile(r"үйл ажиллагаа|жилийн тайлан|annual report|operations report", re.I)),
    ("financial_statement", re.compile(r"санхүү|financial|баланс|balance sheet", re.I)),
]

DOC_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar")
_DOC_PATH = re.compile(r"/(storage|uploads?|files?|documents?|attachments?|media|download)s?/|download|getfile", re.I)
# link texts that say nothing about the document (open.mse.mn's are "PDF" / "XLS")
_GENERIC = re.compile(r"^(татах|үзэх|харах|дэлгэрэнгүй|файл|download|view|open|pdf|xlsx?|docx?|more|татаж авах|"
                      r"энд дарна уу)\W*$", re.I)
_GENERIC_WORDS = re.compile(r"(?<!\w)(татах|үзэх|харах|дэлгэрэнгүй|download|view|pdf|xlsx?|docx?)(?!\w)", re.I)
_YEAR = re.compile(r"(?<!\d)(19[89]\d|20[0-4]\d)(?!\d)")
_DATE = re.compile(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}")
_HALF = re.compile(r"хагас|half|semi|6 сар", re.I)
_QUARTER = re.compile(r"улира?л|quarter|\bq[1-4]\b", re.I)
_ANNUAL = re.compile(r"жилийн|annual|\bжил\b|year", re.I)
# table columns (lower case): what they hold
_COL_SKIP = re.compile(r"^(№|#|файл|file|татах|\s*)$", re.I)
_COL_YEAR = re.compile(r"^(он|year|тайлант он)$", re.I)
_COL_QUARTER = re.compile(r"^(улирал|quarter)$", re.I)
_COL_DATE = re.compile(r"огноо|date", re.I)


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


def _heading(table) -> str:
    """The title a table sits under (open.mse.mn: a heading before each table, e.g. "БОНЗ, Тогтвортой
    байдлын тайлан"), else ""."""
    if table.caption:
        return clean(table.caption.get_text(" "))[:80]
    prev = table.find_previous(["h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "legend", "caption"])
    if prev is not None and prev.find_parent("table") is None:
        text = clean(prev.get_text(" "))
        if 2 <= len(text) <= 80:
            return text
    return ""


def _table_row(a) -> dict | None:
    """{heading, cells: [(header, text)], link_header} of the table row a link sits in, else None."""
    tr = a.find_parent("tr")
    table = tr.find_parent("table") if tr is not None else None
    if table is None:
        return None
    head = table.find("thead")
    head_row = head.find("tr") if head is not None else next(
        (r for r in table.find_all("tr") if r.find("th") and not r.find("td")), None)
    headers = [clean(c.get_text(" ")) for c in head_row.find_all(["th", "td"])] if head_row is not None else []
    cells = tr.find_all(["td", "th"], recursive=False)
    pairs, link_header = [], ""
    for i, cell in enumerate(cells):
        h = headers[i] if i < len(headers) else ""
        if a in cell.find_all("a"):
            link_header = h
        pairs.append((h, clean(cell.get_text(" "))))
    return {"heading": _heading(table), "cells": pairs, "link_header": link_header}


def _title_from_row(row: dict, own: str) -> tuple[str, int | None, int | None, str | None]:
    """(title, year, quarter, date) from a table row: the heading, the year and quarter columns, the
    describing columns, and the header of the link's own column when it isn't just "Файл"."""
    parts, year, quarter, day = [], None, None, None
    for h, text in row["cells"]:
        hl = h.lower()
        text = clean(_GENERIC_WORDS.sub(" ", text))
        if not text or _COL_SKIP.match(hl) or _GENERIC.match(text):
            continue
        if _COL_YEAR.match(hl) and _YEAR.search(text):
            year = int(_YEAR.search(text).group(1))
        elif _COL_QUARTER.match(hl) and re.fullmatch(r"[1-4]", text):
            quarter = int(text)
        elif _COL_DATE.search(hl):
            day = day or (parse_date(_DATE.search(text).group(0)) if _DATE.search(text) else None)
        elif not re.fullmatch(r"[\d.,%\s-]+", text):
            parts.append(text)
    lead = row["heading"]
    if year:
        lead = f"{lead} {year} он".strip()
    if quarter:
        lead = f"{lead}, {quarter}-р улирал"
    link = clean(re.sub(r"/?\(?\bфайл\b\)?/?", " ", row["link_header"], flags=re.I))   # "Хурлын шийдвэр /файл/"
    extra = [link] if link and not _COL_SKIP.match(link.lower()) else []
    if own and not _GENERIC.match(own):
        extra.append(own)
    title = clean(" · ".join(x for x in [lead, *parts[:2], *extra] if x))
    return title, year, quarter, day


def kind_of(text: str, tab: str, url: str = "") -> str:
    path = urlsplit(url).path if url else ""
    for kind, rule in _PATH_KINDS:
        if path and rule.search(path):
            return kind
    for kind, rule in _KIND_RULES:
        if rule.search(text):
            return kind
    return TABS.get(tab, "other")


def period_of(text: str, quarter: int | None = None) -> str:
    """annual / half / quarter. A quarter column's 4 is the year's report, 2 the half-year one."""
    if quarter:
        return {4: "annual", 2: "half"}.get(quarter, "quarter")
    if _QUARTER.search(text):
        return "quarter"
    if _HALF.search(text):
        return "half"
    if _ANNUAL.search(text):
        return "annual"
    return ""


def parse_tab(html: str, url: str, tab: str) -> list[dict]:
    """The documents a tab links to: [{url, title, year, quarter, period, kind, tab, ext, date, host}],
    each URL once. In a table, the title is built from its heading and columns (open.mse.mn's link texts
    are just "PDF" / "XLS"); elsewhere from the link's text, else its row's."""
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"].strip()).split("#")[0]
        if href in seen or not _is_document(href):
            continue
        seen.add(href)
        own = clean(a.get_text(" ") or a.get("title") or "")
        row_text = _container_text(a)
        row = _table_row(a)
        year = quarter = day = None
        if row is not None:
            title, year, quarter, day = _title_from_row(row, own)
        else:
            title = own if own and not _GENERIC.match(own) else clean(_GENERIC_WORDS.sub(" ", row_text)) or own
        if not title:
            title = urlsplit(href).path.rsplit("/", 1)[-1]
        title = title[:300]
        context = f"{title} {row_text}"
        if year is None:
            years = _YEAR.findall(title) or _YEAR.findall(row_text) or _YEAR.findall(href)
            year = int(years[0]) if years else None
        if day is None:
            m = _DATE.search(row_text)
            day = parse_date(m.group(0)) if m else None
        path = urlsplit(href).path.lower()
        ext = next((e.lstrip(".") for e in DOC_EXT if path.endswith(e)), "")
        out.append({"url": href, "title": title, "year": year, "quarter": quarter,
                    "period": period_of(context, quarter), "kind": kind_of(context, tab, href), "tab": tab,
                    "ext": ext, "date": day, "host": urlsplit(href).hostname or ""})
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
# lines with a percent in a section that gave no finding: logged (not stored), to tune the parser
UNMATCHED_LINES = 8


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
    unmatched: list[str] = []
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
                if len(unmatched) < UNMATCHED_LINES:
                    unmatched.append(f"{n}: {line[:120]}")
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
            "sections": {k: v[:10] for k, v in sections.items() if v}, "findings": findings,
            "unmatched": unmatched}


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


# ---- OCR of scanned PDFs (module docstring, step 5) ----------------------------------------------

OCR_DPI = 300                       # 200 dpi read "100%" as "10096" more often (2026-11-28 trials)
OCR_PAGE_TIMEOUT = 120              # seconds for one page; a page that takes longer is left out
# Tesseract's Mongolian model reads "%" as "96": "34%" -> "3496", "51.5 %" -> "51.5 96" (2026-11-28 trials;
# at 300 dpi it sometimes gives "90", which also ends amounts like "1,290", so that is not mended). Only on a
# line that names an organisation (a legal form), only where the number before it is a percent (<= 100), not
# a four-digit year ("1996", "2096") and not an amount or a count (a unit word after it).
_OCR_PCT = re.compile(r"(?<![\d.,])(\d{1,3}(?:[.,]\d{1,4})?)(\s?)(96)(?![\d%])"
                      r"(?!\s*(?:сая|мянга|мянган|тэрбум|төгрөг|₮|ширхэг|ш\.|ш\b|хувьцаа|млн|тэрб|mln|bn|mnt|usd|\$))",
                      re.I)


def ocr_lang() -> str:
    return os.getenv("REPORTS_OCR_LANG") or "mon+eng"


def ocr_budget() -> int:
    return int(os.getenv("REPORTS_OCR_BUDGET") or 6)


def ocr_max_pages() -> int:
    return int(os.getenv("REPORTS_OCR_MAX_PAGES") or 40)


def ocr_available() -> str:
    """"" when Tesseract and its language data (ocr_lang) are installed, else why not."""
    import shutil
    import subprocess

    if not shutil.which("tesseract"):
        return "tesseract is not installed (apt-get install tesseract-ocr tesseract-ocr-mon)"
    try:
        out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"tesseract: {exc}"
    have = set((out.stdout or "").split())
    missing = [lang for lang in ocr_lang().split("+") if lang not in have]
    return f"tesseract has no language data for {', '.join(missing)}" if missing else ""


def fix_ocr_percent(text: str) -> str:
    """Put back the percent signs Tesseract read as "96" / "90", on lines that name an organisation."""
    out = []
    for line in (text or "").splitlines():
        if _ORG.search(line):
            def repl(m):
                num, digits = m.group(1), m.group(1) + m.group(3)
                if not m.group(2) and re.fullmatch(r"(?:19|20)\d\d", digits):
                    return m.group(0)                      # a year
                try:
                    value = float(num.replace(",", "."))
                except ValueError:
                    return m.group(0)
                return f"{num}%" if 0 < value <= 100 else m.group(0)
            line = _OCR_PCT.sub(repl, line)
        out.append(line)
    return "\n".join(out)


def ocr_pdf(data: bytes, page_limit: int | None = None) -> tuple[list[str], int]:
    """(the text of each of the first pages, total pages): each page rendered at OCR_DPI with pypdfium2
    (installed with pdfplumber) and read by Tesseract (ocr_lang)."""
    import subprocess

    import pypdfium2 as pdfium

    page_limit = page_limit or ocr_max_pages()
    texts = []
    pdf = pdfium.PdfDocument(data)
    try:
        total = len(pdf)
        for i in range(min(total, page_limit)):
            page = pdf[i]
            try:
                img = page.render(scale=OCR_DPI / 72).to_pil().convert("L")
                buf = io.BytesIO()
                img.save(buf, "PNG")
                out = subprocess.run(["tesseract", "stdin", "stdout", "-l", ocr_lang(), "--psm", "6"],
                                     input=buf.getvalue(), capture_output=True, timeout=OCR_PAGE_TIMEOUT)
                texts.append(fix_ocr_percent(out.stdout.decode("utf-8", "replace")))
            except Exception as exc:       # one bad page must not lose the rest
                log.debug("ocr page %d: %s", i + 1, exc)
                texts.append("")
            finally:
                page.close()
    finally:
        pdf.close()
    return texts, total


def scans_due(g, budget: int) -> list[dict]:
    """Scanned documents not read by OCR yet: the newest first, a year's annual report before its
    financial statement, audit report and half-year report (as pdfs_due)."""
    rows = g.run(
        """
        MATCH (c:Company)-[:PUBLISHED]->(r:Report)
        WHERE r.current AND r.scanned AND r.pdf_error IS NULL AND r.ocr_at IS NULL AND r.kind IN $kinds
        RETURN c.ticker AS ticker, r.url AS url, r.kind AS kind, coalesce(r.year, 0) AS year,
               coalesce(r.period, '') AS period
        """, kinds=list(READ_KINDS))
    rank = {k: i for i, k in enumerate(READ_KINDS)}
    rank_period = {"annual": 0, "": 1, "half": 2, "quarter": 3}
    return sorted(rows, key=lambda r: (-r["year"], rank[r["kind"]], rank_period.get(r["period"], 1),
                                       r["url"]))[:budget]


def write_ocr(g, url: str, res: dict, when: str) -> None:
    """OCR results on the Report: `ocr_at`, `ocr_text_pages`, `ocr_error`; its sections and findings
    replace pdfplumber's (empty for a scan). Each finding carries "ocr": true."""
    findings = [{**f, "ocr": True} for f in res.get("findings") or []]
    g.run(
        """
        MATCH (r:Report {url: $url})
        SET r.ocr_at = $when, r.ocr_version = $v, r.ocr_error = $error, r.ocr_pages_read = $pages_read,
            r.ocr_text_pages = $text_pages
        FOREACH (_ IN CASE WHEN $error IS NULL THEN [1] ELSE [] END |
            SET r.sections = $sections, r.findings = $findings, r.findings_count = $nf)
        """, url=url, when=when, v=STEP_VERSION, error=res.get("error"), pages_read=res.get("pages_read"),
        text_pages=res.get("text_pages"),
        sections=[f"{k}:{','.join(map(str, v))}" for k, v in (res.get("sections") or {}).items()],
        findings=json.dumps(findings, ensure_ascii=False), nf=len(findings))


def read_scans(g, fetcher=None, budget: int | None = None, minutes: float | None = None,
               when: str | None = None, clock=None) -> dict:
    """OCR up to `budget` (REPORTS_OCR_BUDGET, 6) scanned documents, within `minutes` (default 20).
    Needs Tesseract (ocr_available), so it runs in the daily job, not on Render's 0.15 CPU."""
    import time

    from .scraper import Fetcher

    report = {"read": 0, "failed": 0, "findings": 0, "pages": 0, "errors": [], "skipped": ocr_available()}
    if report["skipped"]:
        return report
    report.pop("skipped")
    clock = clock or time.monotonic
    deadline = clock() + 60 * (20 if minutes is None else minutes)
    fetcher = fetcher or Fetcher()
    when = when or date.today().isoformat()
    for r in scans_due(g, ocr_budget() if budget is None else budget):
        if clock() >= deadline:
            report["stopped"] = "time budget used"
            break
        data = fetcher.get_bytes(r["url"], use_cache=False, store=False)
        if data is None or not data.startswith(b"%PDF") or len(data) > max_bytes():
            # a failed download is tried again next time; a page that isn't a PDF is not
            error = ("not a PDF" if data is not None and not data.startswith(b"%PDF") else
                     "too large" if data is not None else f"fetch: {fetcher.last_error or fetcher.last_status}")
            report["failed"] += 1
            if len(report["errors"]) < 5:
                report["errors"].append(f"{r['ticker']}: {error}")
            if data is not None:
                write_ocr(g, r["url"], {"error": error}, when)
            continue
        try:
            texts, total = ocr_pdf(data)
            res = analyse_text(texts, total)
        except Exception as exc:
            res = {"error": f"ocr: {type(exc).__name__}: {exc}"[:200]}
        write_ocr(g, r["url"], res, when)
        if res.get("error"):
            report["failed"] += 1
            if len(report["errors"]) < 5:
                report["errors"].append(f"{r['ticker']}: {res['error']}")
            continue
        report["read"] += 1
        report["pages"] += res.get("pages_read") or 0
        report["findings"] += len(res.get("findings") or [])
        log.info("REPORTS ocr %s %s: %s", r["ticker"], r["url"][-80:], json.dumps(
            {k: res.get(k) for k in ("pages", "pages_read", "text_pages", "sections", "unmatched")} |
            {"findings": (res.get("findings") or [])[:5]}, ensure_ascii=False))
    return report


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
        SET r.title = d.title, r.year = d.year, r.quarter = d.quarter, r.period = d.period, r.kind = d.kind,
            r.tab = d.tab,
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
            {k: res.get(k) for k in ("pages", "pages_read", "text_pages", "scanned", "sections", "unmatched")} |
            {"findings": (res.get("findings") or [])[:5]}, ensure_ascii=False))
    return report


def hours_since_step(g) -> float | None:
    rows = g.run("MATCH (m:Meta {key: 'reports'}) RETURN m.at AS at")
    if not rows or not rows[0]["at"]:
        return None
    return (datetime.now(timezone.utc) - datetime.fromisoformat(rows[0]["at"])).total_seconds() / 3600


def upgrade(g) -> dict | None:
    """After a parser change (STEP_VERSION): every company's tabs are due again, and the PDFs read by an
    older version are read again. None when nothing changed."""
    rows = g.run("MATCH (m:Meta {key: 'reports'}) RETURN m.version AS v")
    if not rows or (rows[0]["v"] or 0) >= STEP_VERSION:
        return None
    companies = g.run("MATCH (c:Company) WHERE c.reports_read_at IS NOT NULL REMOVE c.reports_read_at "
                      "RETURN count(c) AS n")[0]["n"]
    pdfs = g.run("""MATCH (r:Report) WHERE r.pdf_read_at IS NOT NULL AND coalesce(r.pdf_version, 0) < $v
                    REMOVE r.pdf_read_at, r.pdf_error, r.ocr_at, r.ocr_error RETURN count(r) AS n""",
                 v=STEP_VERSION)[0]["n"]
    return {"from": rows[0]["v"], "to": STEP_VERSION, "companies": companies, "pdfs": pdfs}


def step(g, fetcher=None, when: str | None = None) -> dict:
    """The pipeline's reports step: index a batch of companies, then read a few PDFs."""
    from .scraper import Fetcher

    fetcher = fetcher or Fetcher()
    when = when or date.today().isoformat()
    from .pipeline import mse_enabled

    res = {"upgraded": upgrade(g)}
    # the tabs are on open.mse.mn (paused: pipeline.mse_enabled); the PDFs are on issuers.mse.mn
    res["index"] = index(g, fetcher, when=when) if mse_enabled() else {"skipped": "MSE_ENABLE=0"}
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
             r.scanned AS scanned, r.ocr_at AS ocr_at, r.sections AS sections, r.findings AS findings"""


def company_reports(g, key: str, limit: int = 200) -> dict:
    """A company's documents, newest first, and the findings read from them."""
    rows = [_row(r) for r in g.run(
        f"""MATCH (:Company {{key: $key}})-[:PUBLISHED]->(r:Report)
            RETURN {_FIELDS}
            ORDER BY r.current DESC, coalesce(r.year, 0) DESC, r.date DESC, r.title LIMIT $limit""",
        key=key, limit=limit)]
    findings = [{**f, "n": n, "report": r["url"], "report_title": r["title"], "year": r["year"]}
                for r in rows for n, f in enumerate(r["findings"])]
    read = g.run("MATCH (c:Company {key: $key}) RETURN c.reports_read_at AS at", key=key)
    return {"items": rows, "findings": findings, "read_at": read[0]["at"] if read else None,
            "note": "Findings are lines of the company's own reports that name an organisation with a percent; "
                    "they are not checked and are not used as stakes until someone confirms one "
                    "(cli reports --confirm URL --finding N)."}


def findings_report(g, kind: str = "", limit: int = 500) -> dict:
    """Findings across the market (subsidiaries, associates, shareholder organisations), newest first."""
    rows = g.run(
        """MATCH (c:Company)-[:PUBLISHED]->(r:Report) WHERE r.current AND coalesce(r.findings_count, 0) > 0
           RETURN c.ticker AS ticker, c.name AS company, r.url AS url, r.title AS title, r.year AS year,
                  r.findings AS findings
           ORDER BY coalesce(r.year, 0) DESC, c.ticker""")
    out = []
    for r in rows:
        for n, f in enumerate(json.loads(r["findings"] or "[]")):
            if kind and f.get("kind") != kind:
                continue
            out.append({**f, "n": n, "ticker": r["ticker"], "company": r["company"], "report": r["url"],
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
               size([r IN rs WHERE r.ocr_at IS NOT NULL AND r.ocr_error IS NULL]) AS ocr_read,
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


# ---- confirmed findings -> stakes (module docstring, step 4) ---------------------------------------

CONFIRMED_DIR = "data/manual/annual-reports"
SOURCE_PREFIX = "report:"              # provenance.REPORT_PREFIX
CONFIRMABLE = ("subsidiary", "associate")


def _slug(name: str) -> str:
    from .normalize import entity_key

    words = entity_key(name, "organization").split(":", 1)[-1]
    return "~AR-" + (re.sub(r"[^0-9A-Za-zА-Яа-яӨөҮүЁё]+", "-", words).strip("-").upper() or "X")


def _company_by_name(g, name: str) -> str | None:
    """The ticker of the one company whose name (or alias) has the same organisation key, else None."""
    from .normalize import entity_key

    want = entity_key(name, "organization")
    hits = set()
    for r in g.run("MATCH (c:Company) RETURN c.ticker AS ticker, c.name AS name, coalesce(c.aliases, []) AS aliases"):
        names = [r["name"] or ""] + [a for a in r["aliases"] if isinstance(a, str)]
        if any(entity_key(n, "organization") == want for n in names if n):
            hits.add(r["ticker"])
    return hits.pop() if len(hits) == 1 else None


def confirm(g, url: str, index: int, ticker: str | None = None, root: str | os.PathLike = CONFIRMED_DIR,
            percent: float | None = None) -> dict:
    """Write finding number `index` (0-based, in the report's `findings`) of the report at `url` into the
    confirmed dataset as a stake (module docstring, step 4). `ticker`: the subsidiary's ticker or register
    number when the name doesn't find it; `percent` corrects the parser's. Returns the row written."""
    from pathlib import Path

    from . import csv_io
    from .models import Company, Shareholding

    rows = g.run("""MATCH (c:Company)-[:PUBLISHED]->(r:Report {url: $url})
                    RETURN c.ticker AS ticker, c.name AS name, r.year AS year, r.date AS date, r.findings AS findings""",
                 url=url)
    if not rows:
        raise ValueError(f"no report with the URL {url}")
    rep = rows[0]
    findings = json.loads(rep["findings"] or "[]")
    if not 0 <= index < len(findings):
        raise ValueError(f"the report has {len(findings)} findings; there is no number {index}")
    f = findings[index]
    if f.get("kind") not in CONFIRMABLE:
        raise ValueError(f"finding {index} is a {f.get('kind')}: only {', '.join(CONFIRMABLE)} findings become stakes")
    root = Path(root)
    ds = csv_io.read_dir(root) if root.is_dir() else csv_io.Dataset()
    sub = g.resolve_ticker(ticker) if ticker else _company_by_name(g, f["name"])
    new_company = None
    if not sub:
        sub = _slug(f["name"])
        if not any(c.ticker == sub for c in ds.companies):
            new_company = Company(ticker=sub, name=f["name"], listed=False, url=url)
            ds.companies.append(new_company)
    source = SOURCE_PREFIX + url
    as_of = str(rep["year"] or (rep["date"] or "")[:4] or "")
    row = Shareholding(company_ticker=sub, holder_name=rep["name"], holder_ticker=rep["ticker"],
                       percent=percent if percent is not None else f.get("percent"), holder_type="organization",
                       as_of=as_of, source=source)
    ds.shareholdings = [h for h in ds.shareholdings
                        if not (h.company_ticker == sub and h.holder_ticker == rep["ticker"])] + [row]
    csv_io.write_dir(ds, root)
    return {"company": sub, "holder": rep["ticker"], "percent": row.percent, "as_of": as_of, "source": source,
            "new_company": new_company.name if new_company else None, "finding": f}
