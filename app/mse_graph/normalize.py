"""Name normalisation and entity-type detection for Mongolian/English names.

The same person or company often appears in slightly different forms across
MSE pages and annual reports, e.g. "Б.Батболд", "Батболд Б.", "БАТБОЛД Б".
`entity_key` collapses these into one stable key so they merge into a single
graph node.
"""
from __future__ import annotations

import re
import unicodedata

# Legal-form suffixes / words that mark an organisation rather than a person.
_ORG_MARKERS = [
    "ххк", "хк", "төхк", "төк", "өхк", "ххн", "хзх", "ббсб", "ттб", "ҮЦК", "ҮЦ",
    "компани", "корпораци", "групп", "холдинг", "банк", "сан", "нөхөрлөл", "хоршоо",
    "засгийн газар", "төрийн", "өмчийн", "яам", "агентлаг", "газар", "хороо",
    "llc", "jsc", "ltd", "inc", "plc", "corp", "corporation", "company", "co.",
    "group", "holding", "holdings", "bank", "fund", "capital", "partners", "trust",
    "limited", "gmbh", "ag", "s.a.", "b.v.", "pte", "government", "ministry",
]
_ORG_RE = re.compile(
    r"(?<![\w])(" + "|".join(re.escape(m.lower()) for m in _ORG_MARKERS) + r")(?![\w])",
    re.IGNORECASE,
)

# Phrases that describe a group of holders, not one entity.
_AGGREGATE_RE = re.compile(
    r"(бусад|жижиг хувьцаа|олон нийт|нийт дүн|бүгд|бусад хувьцаа эзэмшигч|"
    r"others?|public|free ?float|minority|total)",
    re.IGNORECASE,
)

_QUOTES_RE = re.compile(r"[\"'«»“”„‘’`]")
_SPACE_RE = re.compile(r"\s+")


def clean(text: str | None) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\xa0", " ")
    return _SPACE_RE.sub(" ", text).strip()


def is_organization(name: str) -> bool:
    return bool(_ORG_RE.search(clean(name).lower()))


def is_aggregate(name: str) -> bool:
    """True for rows such as "Бусад" (others) or "Нийт" (total) in shareholder tables."""
    n = clean(name).lower()
    return bool(_AGGREGATE_RE.match(n)) and len(n) < 40


def entity_key(name: str) -> str:
    """Stable merge key for a person or organisation name.

    * case-folded, quotes and punctuation removed
    * for people, tokens are sorted so "Б.Батболд" == "Батболд Б."
    """
    n = _QUOTES_RE.sub("", clean(name)).casefold()
    if is_organization(n):
        n = re.sub(r"[^\w\s]", " ", n)
        return "org:" + _SPACE_RE.sub(" ", n).strip()
    tokens = [t for t in re.split(r"[\s.,\-]+", n) if t]
    return "person:" + " ".join(sorted(tokens))


def parse_percent(value: str | float | int | None) -> float | None:
    """Parse "34.5%", "34,5", "0.345" style values into a percentage (0-100)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = clean(str(value)).replace("%", "").replace(" ", "")
    if s.count(",") == 1 and "." not in s:
        s = s.replace(",", ".")
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_int(value: str | int | None) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    digits = re.sub(r"[^\d]", "", str(value))
    return int(digits) if digits else None
