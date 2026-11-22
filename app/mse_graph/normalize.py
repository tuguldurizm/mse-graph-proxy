"""Name normalisation and entity-type detection for Mongolian/English names.

The same person or company often appears in slightly different forms across
MSE pages and annual reports, e.g. "Б.Батболд", "Батболд Б.", "БАТБОЛД Б".
`entity_key` collapses these into one stable key so they merge into a single
graph node.
"""
from __future__ import annotations

import hashlib
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


# ---- positions: standard role vocabulary (issue #34) ------------------------

ROLES_STD = ("chair", "vice_chair", "board_member", "independent_director", "ceo", "deputy_ceo",
             "cfo", "other_executive", "secretary", "supervisory", "unknown")

# Checked in order; the first match wins, so the narrow roles come before the broad ones
# ("ТУЗ-ийн нарийн бичгийн дарга" is the secretary, not the chair; "Гүйцэтгэх захирлын
# орлогч" is a deputy, not the CEO; "Санхүү эрхэлсэн дэд захирал" runs finance, so the CFO).
_BOARD = r"(туз|төлөөлөн удирдах|удирдах зөвлөл|board)"
_ROLE_RULES = [
    ("secretary", r"нарийн бичг|secretary"),
    ("supervisory", r"хяналтын зөвлөл|хянан шалгах|ревиз|supervisory"),
    ("vice_chair", r"(дэд|орлогч) дарга|vice[ -]?chair|deputy chair"),
    ("chair", _BOARD + r".*дарга|chair"),
    ("cfo", r"санхүү\S*\s+(\S+\s+)?захир|санхүү эрхэлсэн|cfo|chief financial|financ\S* director"),
    ("deputy_ceo", r"(дэд|орлогч)\s+(\S+\s+)?захир|захирлын (орлогч|туслах)|"
                   r"deputy|vice[ -]?president|дэд ерөнхийлөгч"),
    # "захир" also covers the genitive: "Гүйцэтгэх захирлын үүрэг гүйцэтгэгч" (acting CEO)
    ("ceo", r"гүйцэтгэх захир|ерөнхий захир|^захирал$|ceo|chief executive|managing director|"
            r"general director"),
    ("independent_director", r"хараат бус|independent"),
    ("board_member", _BOARD + r"|гишүүн|member|director"),
    ("other_executive", r"захир|менежер|manager|officer|executive|нягтлан|accountant|"
                        r"мэргэжилтэн|specialist|ерөнхийлөгч|president|дарга|head|зөвлөх|advis"),
]
_ROLE_RES = [(role, re.compile(rx, re.IGNORECASE)) for role, rx in _ROLE_RULES]
# Changes whenever the rules (or post_key) change, so graph.migrate_positions recomputes every edge.
ROLES_VERSION = hashlib.sha1((repr(_ROLE_RULES) + "|post-v1").encode()).hexdigest()[:12]
_TITLE_KEY_RE = re.compile(r"[^\w]+")


def role_std(title: str | None, role: str | None = None) -> str:
    """The standard role for a free-text position title (one of ROLES_STD).

    `role` is the source's coarse "board" / "executive": a bare "Дарга" in a
    board table is the chair. Titles that match no pattern are "unknown", for the
    data-quality report to list.
    """
    t = clean(title).casefold()
    if not t:
        return "unknown"
    if role == "board" and re.fullmatch(r"дарга|chair(man|woman|person)?", t):
        return "chair"
    for std, rx in _ROLE_RES:
        if rx.search(t):
            return std
    return "unknown"


def post_key(title: str | None, role: str | None = None) -> str:
    """The seat a position occupies: open positions merge on (person, company, post_key).

    Two spellings of one seat ("Гүйцэтгэх захирал" / "Гүйцэтгэх захирал (CEO)") give the
    same key, so a respelt title refreshes the stint instead of closing it. An
    independent director sits on the board, so both share "board_member". Titles too
    vague to identify a seat (other_executive, unknown) keep their own wording, so
    "Маркетингийн захирал" and "Хуулийн хэлтсийн дарга" stay two posts.
    """
    std = role_std(title, role)
    if std == "independent_director":
        return "board_member"
    if std in ("other_executive", "unknown"):
        return std + ":" + _TITLE_KEY_RE.sub(" ", clean(title).casefold()).strip()
    return std


# ---- registration numbers and organisation types (issue #35) ----------------------

_REGISTER_RE = re.compile(r"\d{6,8}")


def split_reg_numbers(*numbers: str | None) -> tuple[str, str]:
    """(register_no, state_reg_no) from whatever numbers a source gave.

    `register_no` is always the registry's 6-8 digit register number (Регистрийн
    дугаар, e.g. APU 2702673), the one Open Data Lab pages use. Anything else, such
    as the 10-digit state registration number mse.mn gives (APU 9010001005), is the
    `state_reg_no`. The first number of each kind wins.
    """
    register = state = ""
    for number in numbers:
        n = re.sub(r"\s", "", clean(number))
        if not n:
            continue
        if _REGISTER_RE.fullmatch(n):
            register = register or n
        else:
            state = state or n
    return register, state


ORG_TYPES = ("state", "foreign", "fund", "other")

# Name markers of state or municipal owners (Mongolian / English).
STATE_MARKERS = [
    "төрийн", "засгийн газар", "яам", "аймгийн", "нийслэлийн", "дүүргийн", "төк", "төхк",
    "эрдэнэс монгол", "монгол банк", "нийгмийн даатгалын сан",
    "government", "ministry", "state property", "state-owned", "state owned", "municipal",
    "erdenes mongol", "bank of mongolia",
]
_STATE_RE = re.compile(r"(?<![\w])(" + "|".join(re.escape(m) for m in STATE_MARKERS) + r")(?![\w])",
                       re.IGNORECASE)
# Investment and pension funds.
_FUND_RE = re.compile(r"(?<![\w])(сан|фонд|fund|funds|trust|etf)(?![\w])", re.IGNORECASE)
# Foreign legal forms. Only names written wholly in Latin letters count: Mongolian
# companies use "LLC" / "JSC" in English, which say nothing about where they are from.
_FOREIGN_RE = re.compile(
    r"(?<![\w])(ltd|limited|inc|incorporated|gmbh|pte|plc|ag|s\.a\.|sa|b\.v\.|bv|n\.v\.|nv|"
    r"k\.k\.|co\., ltd|pty|srl|s\.r\.l\.|oy|ab|corp|corporation|kabushiki kaisha)(?![\w])",
    re.IGNORECASE)
_CYRILLIC_RE = re.compile(r"[а-яёөүА-ЯЁӨҮ]")
# Changes whenever the name rules change, so graph.consolidate_companies re-types organisations.
ORG_TYPES_VERSION = hashlib.sha1(
    (_STATE_RE.pattern + _FUND_RE.pattern + _FOREIGN_RE.pattern).encode()).hexdigest()[:12]


def is_state(name: str | None) -> bool:
    """Whether a name marks a state or municipal body or a state-owned company."""
    return bool(_STATE_RE.search(clean(name).lower()))


def org_type_from_name(name: str | None) -> str:
    """The organisation type a name suggests (one of ORG_TYPES), for sources that don't say.

    State markers win ("Нийгмийн даатгалын сан" is the state's), then funds, then foreign
    legal forms of Latin-script names. Everything else is "other".
    """
    n = clean(name)
    if is_state(n):
        return "state"
    if _FUND_RE.search(n):
        return "fund"
    if not _CYRILLIC_RE.search(n) and _FOREIGN_RE.search(n):
        return "foreign"
    return "other"
