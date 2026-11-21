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


_LEGAL_FORM_RE = re.compile(r"(?:\s+(?:ххк|хк|төхк|төк|өхк|ххн|llc|jsc))+$")
_CYR_WORD_RE = re.compile(r"^[а-яёөү][а-яёөү-]+$")


def entity_key(name: str, kind: str = "") -> str:
    """Stable merge key for a person or organisation name.

    * case-folded, quotes and punctuation removed
    * for people, tokens are sorted so "Б.Батболд" == "Батболд Б."
    * a Mongolian "Patronymic Given" name is reduced to the initial form MSE
      uses, so "Батын Болд" / "Батын.Болд" == "Б.Болд" (the registry and
      Open Data Lab write the patronymic in full)
    * `kind="organization"` forces an organisation key for names without a
      legal-form marker (e.g. a registry row typed "Хуулийн этгээд")
    """
    n = _QUOTES_RE.sub("", clean(name)).casefold()
    if kind.startswith("org") or is_organization(n):
        n = _SPACE_RE.sub(" ", re.sub(r"[^\w\s]", " ", n)).strip()
        # "Багануур ХК" (MSE) == "Багануур" (registry): ignore a trailing legal form
        n = _LEGAL_FORM_RE.sub("", n).strip() or n
        return "org:" + n
    tokens = [t.strip("-") for t in re.split(r"[\s.,]+", n) if t.strip("-")]
    if len(tokens) == 2 and all(_CYR_WORD_RE.match(t) for t in tokens):
        tokens = [tokens[0][0], tokens[1]]
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


_YMD_RE = re.compile(r"(?<!\d)(\d{4})(?:[.\-/](\d{1,2})(?:[.\-/](\d{1,2}))?)?(?!\d)")
_DMY_RE = re.compile(r"(?<!\d)(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})(?!\d)")


def parse_date(value: str | None) -> str:
    """Normalise a source date to ISO form ("2021-04-29", "2021-04" or "2021").

    Accepts "2021.04.29", "2021/4/29", "29.04.2021", "2021 он" etc. ISO strings
    of different precision still sort correctly against each other. Text with
    no recognisable date is returned cleaned but otherwise unchanged, so no
    information is lost; an empty value gives "".
    """
    text = clean(value)
    if not text:
        return ""
    m = _DMY_RE.search(text)
    if m and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(1)) <= 31:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = _YMD_RE.search(text)
    if m and 1900 <= int(m.group(1)) <= 2100:
        y, mo, d = m.group(1), m.group(2), m.group(3)
        if mo and 1 <= int(mo) <= 12:
            if d and 1 <= int(d) <= 31:
                return f"{y}-{int(mo):02d}-{int(d):02d}"
            return f"{y}-{int(mo):02d}"
        return y
    return text
