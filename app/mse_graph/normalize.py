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
from dataclasses import dataclass

# Legal-form suffixes / words that mark an organisation rather than a person.
_ORG_MARKERS = [
    "ххк", "хк", "төхк", "төк", "өхк", "ххн", "хзх", "ббсб", "ттб", "ҮЦК", "ҮЦ",
    "компани", "корпораци", "групп", "холдинг", "банк", "сан", "нөхөрлөл", "хоршоо",
    "засгийн газар", "төрийн", "өмч", "өмчийн", "яам", "яамны", "холбоо", "хурал", "агентлаг", "газар", "хороо",
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
_CYR_WORD_RE = re.compile(r"^[а-яёөү][а-яёөү-]*$")
# genitive endings of a patronymic: "Батын" / "Доржийн" / "Батаагийн" are the fathers Бат, Дорж, Батаа
_GENITIVE_RE = re.compile(r"(?:гийн|ийн|ын)$")


@dataclass(frozen=True)
class PersonName:
    """A person's name taken apart (issue #36).

    `form` is "full" for the registry's "Patronymic Given" (optionally with the clan
    name, ургийн овог, first: "Боржигон Батын Батболд"), "initial" for MSE's
    "Б.Батболд" / "Батболд Б.", and "other" for anything else (one word, Latin script,
    more words). `key` is the person's merge key; `match_key` is the initial form
    every spelling of the same name shares ("person:б батболд"), which is how an
    initial is matched to a full name.
    """
    form: str
    key: str
    match_key: str
    given: str = ""            # as written
    initial: str = ""          # the patronymic's initial, as written ("Б", "Бя")
    patronymic: str = ""
    clan: str = ""
    full_name: str = ""        # "Батын Батболд", clan included when given

    def parts(self) -> dict:
        """The non-empty name properties a Person node stores."""
        props = {"name_form": self.form, "given_name": self.given, "patronymic": self.patronymic,
                 "clan_name": self.clan, "full_name": self.full_name}
        return {k: v for k, v in props.items() if v}

    def could_be(self, other: "PersonName") -> bool:
        """An initial form that a full form may stand for: same given name, initial fits."""
        return (self.form == "initial" and other.form == "full"
                and self.given.casefold() == other.given.casefold()
                and other.patronymic.casefold().startswith(self.initial.casefold()))


def _tokens(name: str) -> list[str]:
    return [t.strip("-") for t in re.split(r"[\s.,]+", _QUOTES_RE.sub("", clean(name))) if t.strip("-")]


def patronymic_stem(patronymic: str) -> str:
    """The father's name in a patronymic, case-folded: "Батын" -> "бат", "Доржийн" -> "дорж"."""
    low = (patronymic or "").casefold()
    stem = _GENITIVE_RE.sub("", low)
    return stem if len(stem) >= 2 else low


def person_name(name: str) -> PersonName:
    """Take a person's name apart (see PersonName)."""
    tokens = _tokens(name)
    low = [t.casefold() for t in tokens]
    other = "person:" + " ".join(sorted(low))
    if not low or not all(_CYR_WORD_RE.match(t) for t in low):
        return PersonName("other", other, other)
    short = [len(t) <= 2 for t in low]
    if len(low) == 2 and sum(short) == 1:
        i = short.index(True)
        initial, given = tokens[i], tokens[1 - i]
        key = "person:" + " ".join(sorted([low[i], low[1 - i]]))
        return PersonName("initial", key, "person:" + " ".join(sorted([low[i][0], low[1 - i]])),
                          given=given, initial=initial)
    if len(low) in (2, 3) and not any(short):
        clan, patronymic, given = ([""] + tokens if len(tokens) == 2 else tokens)
        stem = patronymic_stem(patronymic)
        return PersonName("full", f"person:{stem} {given.casefold()}",
                          "person:" + " ".join(sorted([patronymic[0].casefold(), given.casefold()])),
                          given=given, initial=patronymic[0], patronymic=patronymic, clan=clan,
                          full_name=" ".join(tokens))
    return PersonName("other", other, other)


def entity_key(name: str, kind: str = "") -> str:
    """Stable merge key for a person or organisation name.

    * case-folded, quotes and punctuation removed
    * people (person_name): an initial form's tokens are sorted, so "Б.Батболд" ==
      "Батболд Б."; a full registry name keeps its patronymic, so "Батын Батболд"
      (== "Бат Батболд") and "Баярын Батболд" stay two people (issue #36). Whether
      "Б.Батболд" is one of them is decided by the loader, which knows the company
      (Graph._people_resolver)
    * `kind="organization"` forces an organisation key for names without a
      legal-form marker (e.g. a registry row typed "Хуулийн этгээд"); `kind="person"`
      forces the person reading (the key a name had before a marker made it an organisation)
    """
    n = _QUOTES_RE.sub("", clean(name)).casefold()
    if kind.startswith("person"):
        return person_name(n).key
    if kind.startswith("org") or is_organization(n):
        n = _SPACE_RE.sub(" ", re.sub(r"[^\w\s]", " ", n)).strip()
        # "Багануур ХК" (MSE) == "Багануур" (registry): ignore a trailing legal form
        n = _LEGAL_FORM_RE.sub("", n).strip() or n
        return "org:" + n
    return person_name(n).key


# Changes when the person-identity rules change (issue #36): manual datasets reload once.
IDENTITY_VERSION = "persons-v1"


def match_key(name: str) -> str:
    """The initial-form key any spelling of a person's name shares (the key before #36)."""
    return person_name(name).match_key


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
# Names of state and municipal bodies and of state-owned companies, matched as whole words.
# Checked against the live graph on 2026-11-24 (governance check validation, issue #10): a place
# word alone ("аймгийн", "дүүргийн") marked herders' and pasture users' associations as the state,
# so local bodies are named by what they are (council, governor's office, property office); the
# genitive forms ("засгийн газрын", "яамны") and local property ("орон нутгийн өмч", the holder
# of BTG, BDL and TTL on mse.mn) were missed.
STATE_MARKERS = [
    "төрийн", "засгийн газар", "засгийн газрын", "яам", "яамны", "нийслэлийн", "төк", "төхк",
    "орон нутгийн өмч", "орон нутгийн өмчийн", "орон нутгийн өмчит",
    "иргэдийн төлөөлөгчдийн хурал", "засаг даргын", "тамгын газар", "өмчийн харилцааны газар",
    "хотын захиргаа", "эрдэнэс монгол", "монгол банк", "монгол улсын хөгжлийн банк",
    "нийгмийн даатгалын сан",
    "government", "ministry", "state property", "state-owned", "state owned", "municipal",
    "erdenes mongol", "bank of mongolia", "development bank of mongolia",
]
_STATE_RE = re.compile(r"(?<![\w])(" + "|".join(re.escape(m) for m in STATE_MARKERS) + r")(?![\w])",
                       re.IGNORECASE)
# "Төрийн бус байгууллага" is a non-governmental organisation.
_NOT_STATE_RE = re.compile(r"(?<![\w])төрийн\s+бус(?![\w])", re.IGNORECASE)
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
    (_STATE_RE.pattern + _NOT_STATE_RE.pattern + _FUND_RE.pattern + _FOREIGN_RE.pattern).encode()).hexdigest()[:12]


def is_state(name: str | None) -> bool:
    """Whether a name marks a state or municipal body or a state-owned company."""
    return bool(_STATE_RE.search(_NOT_STATE_RE.sub(" ", clean(name).lower())))


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


# ---- standard sector from registry activity codes (issue #38) -------------------
#
# The registry lists a company's activities as "<code> - <description>", e.g.
# "4610 - Гадаад худалдаа" or "46100 - Төлбөр эсвэл ...". The codes are Mongolia's
# MNSIC, built on ISIC Rev. 4: the first two digits are the ISIC division, and the
# division gives the section (A-U). A company's standard sector (`sector_std`) is
# the section of its main activity (main_activity): the first one the registry lists
# that is not a catch-all registration (GENERIC_ACTIVITIES).

SECTIONS_STD = {
    # section: (divisions, Mongolian name, English name)
    "A": ((1, 3), "Хөдөө аж ахуй, ойн аж ахуй, загас агнуур", "Agriculture, forestry and fishing"),
    "B": ((5, 9), "Уул уурхай, олборлох үйлдвэр", "Mining and quarrying"),
    "C": ((10, 33), "Боловсруулах үйлдвэр", "Manufacturing"),
    "D": ((35, 35), "Цахилгаан, хий, уур, агааржуулалт", "Electricity, gas, steam and air conditioning"),
    "E": ((36, 39), "Ус хангамж, бохир ус, хог хаягдал", "Water supply, sewerage and waste"),
    "F": ((41, 43), "Барилга", "Construction"),
    "G": ((45, 47), "Бөөний болон жижиглэн худалдаа", "Wholesale and retail trade"),
    "H": ((49, 53), "Тээвэр, агуулах", "Transportation and storage"),
    "I": ((55, 56), "Зочид буудал, нийтийн хоол", "Accommodation and food service"),
    "J": ((58, 63), "Мэдээлэл, холбоо", "Information and communication"),
    "K": ((64, 66), "Санхүү, даатгал", "Financial and insurance activities"),
    "L": ((68, 68), "Үл хөдлөх хөрөнгө", "Real estate"),
    "M": ((69, 75), "Мэргэжлийн, шинжлэх ухаан, техникийн үйл ажиллагаа",
          "Professional, scientific and technical activities"),
    "N": ((77, 82), "Удирдлага, дэмжлэг үзүүлэх үйлчилгээ", "Administrative and support services"),
    "O": ((84, 84), "Төрийн удирдлага, батлан хамгаалах", "Public administration and defence"),
    "P": ((85, 85), "Боловсрол", "Education"),
    "Q": ((86, 88), "Эрүүл мэнд, нийгмийн халамж", "Human health and social work"),
    "R": ((90, 93), "Урлаг, үзвэр, амралт", "Arts, entertainment and recreation"),
    "S": ((94, 96), "Бусад үйлчилгээ", "Other service activities"),
    "T": ((97, 98), "Өрхийн үйл ажиллагаа", "Activities of households as employers"),
    "U": ((99, 99), "Олон улсын байгууллага", "Extraterritorial organisations"),
}
_ACTIVITY_RE = re.compile(r"^\s*(\d{3,6})\b\s*(?:[-–—:.]\s*)?(.*)$")


def parse_activity(text: str | None) -> tuple[str, str]:
    """("4610", "Гадаад худалдаа") from "4610 - Гадаад худалдаа"; ("", "") when there is
    no code. A 3-digit code lost its leading zero ("510" is division 05)."""
    m = _ACTIVITY_RE.match(clean(text))
    if not m:
        return "", ""
    code = m.group(1)
    if len(code) == 3:
        code = "0" + code
    return code, clean(m.group(2))


def isic_section(code: str | None) -> str:
    """The ISIC Rev. 4 section letter of an MNSIC / ISIC activity code, else ""."""
    digits = re.sub(r"\D", "", code or "")
    if len(digits) < 2:
        return ""
    division = int(digits[:2])
    for section, ((lo, hi), _mn, _en) in SECTIONS_STD.items():
        if lo <= division <= hi:
            return section
    return ""


# Registrations so many companies carry that they say nothing of the business: 4610
# "Гадаад худалдаа" (foreign trade; 46100 "wholesale for a fee") comes first on hotels,
# mines and airports alike, and 8299 is "other business support". A company's main
# activity is one of them only when the registry lists nothing else.
GENERIC_ACTIVITIES = {"4610", "8299"}


def main_activity(codes: list[str]) -> str:
    """The main activity code of a registry list in its order (see GENERIC_ACTIVITIES)."""
    codes = [c for c in codes if c]
    specific = [c for c in codes if c[:4] not in GENERIC_ACTIVITIES]
    return (specific or codes or [""])[0]


def sector_std_name(section: str | None, lang: str = "mn") -> str:
    entry = SECTIONS_STD.get(section or "")
    return "" if entry is None else entry[1 if lang == "mn" else 2]


# ---- mse.mn's sector texts as ISIC divisions (governance check validation, issue #10) ----
#
# mse.mn names a listed company's sector with the Mongolian title of an MNSIC / ISIC Rev. 4
# division ("Хүнсний бүтээгдэхүүний үйлдвэрлэл" is division 10), sometimes in a shorter
# spelling of its own ("Хүнсний үйлдвэрлэл"). Every text the live graph held on 2026-11-24 is
# here, so two spellings of one division compare as one sector. "Санхүүгийн бусад үйл
# ажиллагаа" is mse.mn's catch-all for banks, brokers, the exchange and lenders alike: only
# its section (K) is known. A text not listed is compared only with the same text.

MSE_SECTOR_DIVISIONS = {
    "агуулахын болон тээврийн туслах үйл ажиллагаа": "52",
    "аж ахуйн нэгжийн удирдах дээд байгууллагын үйл ажиллагаа, менежментийн зөвлөгөө өгөх үйл ажиллагаа": "70",
    "албан байгууллагын удирдлагын болон туслах үйл ажиллагаа, бизнесийн бусад туслах үйл ажиллагаа": "82",
    "албан журмын нийгмийн даатгалаас бусад даатгал, давхар даатгал, тэтгэврийн санхүүжилтын үйл ажиллагаа": "65",
    "архитектур болон инженерийн үйл ажиллагаа; техникийн туршилт, шинжилгээ": "71",
    "арьс, арьсан бүтээгдэхүүн үйлдвэрлэл": "15",
    "аялал, жуулчлалын агентлаг, аяллын оператор болон захиалгат бусад үйлчилгээний үйл ажиллагаа": "79",
    "барилга угсралт": "41",
    "барилгын тусгай үйл ажиллагаа": "43",
    "бохир ус цуглуулах, цэвэршүүлэх, ариутгах үйл ажиллагаа": "37",
    "бусад ашигт малтмал олборлолт": "08",
    "бусад боловсруулах үйлдвэрлэл": "32",
    "газар тариалан, мал аж ахуйн үйлдвэрлэл, ан агнуур болон түүнд холбогдох туслах үйл ажиллагаа": "01",
    "гишүүнчлэл бүхий байгууллагын үйл ажиллагаа": "94",
    "даатгалын болон тэтгэврийн санхүүжилтийн үйл ажиллагаанаас бусад санхүүгийн үйлчилгээний үйл ажиллагаа": "64",
    "зочид буудал, байр, сууцаар үйлчлэх үйл ажиллагаа": "55",
    "иргэний зориулалттай инженерийн байгууламжийн угсралтын үйл ажиллагаа": "42",
    "компьютер програмчлал, зөвлөгөө болон түүнд холбогдох үйл ажиллагаа": "62",
    "машин, мотоциклээс бусад барааны бөөний худалдаа": "46",
    "машин, мотоциклээс бусад барааны жижиглэн худалдаа": "47",
    "менежментийн зөвлөх үйлчилгээ": "70",
    "металл үйлдвэрлэл": "24",
    "металлын хүдэр олборлолт": "07",
    "нийтийн хоолны үйлчилгээ": "56",
    "нэхмэлийн үйлдвэрлэл": "13",
    "санхүүгийн бусад үйл ажиллагаа": "K",
    "түрээс, лизингийн үйл ажиллагаа": "77",
    "төмөрлөг бус эрдэс бодисоор хийсэн эдлэл үйлдвэрлэл": "23",
    "ус цуглуулах, ариутгах, ус хангамж": "36",
    "уул уурхайн олборлолтын туслах үйл ажиллагаа": "09",
    "хог хаягдлыг цуглуулах, ариутгах, боловсруулах үйл ажиллагаа": "38",
    "хуурай замын болон хий дамжуулах хоолойн тээвэрлэлт": "49",
    "хүнсний бүтээгдэхүүний үйлдвэрлэл": "10",
    "хүнсний үйлдвэрлэл": "10",
    "цахилгаан холбоо": "61",
    "цахилгаан, хий, уур, агааржуулалт": "35",
    "чулуун, хүрэн нүүрс олборлолт": "05",
    "шингэн хүнсний үйлдвэрлэл": "11",
    "шуудан, зарлага, элчийн үйл ажиллагаа": "53",
    "үл хөдлөх хөрөнгийн үйл ажиллагаа": "68",
}


def mse_sector_code(text: str | None) -> str:
    """The ISIC division ("10") or, for a catch-all text, the section ("K") of an mse.mn sector
    text; "" for a text not in MSE_SECTOR_DIVISIONS."""
    return MSE_SECTOR_DIVISIONS.get(clean(text).casefold(), "")


def comparable_sector(sector_mse: str | None, activity_main: str | None = None,
                      sector_std: str | None = None) -> tuple[str, str, str]:
    """(key, division, section) on which two companies count as the same sector.

    The ISIC division where one is known: mse.mn's sector text first (how the exchange
    classifies a listed company; the registry's first activity is often a side line, such
    as APU's trade), else the registry's main activity code. Then the section alone (an
    mse.mn catch-all text, or `sector_std`), then the text itself. Keys of different kinds
    never match: a company known only as section K is not compared with banks (64) or
    insurers (65). ("", "", "") when nothing is known."""
    code = mse_sector_code(sector_mse)
    if code.isdigit():
        return "div:" + code, code, isic_section(code)
    digits = re.sub(r"\D", "", activity_main or "")
    if len(digits) >= 2 and isic_section(digits):
        return "div:" + digits[:2], digits[:2], isic_section(digits)
    section = code or (sector_std or "")
    if section:
        return "sec:" + section, "", section
    text = clean(sector_mse).casefold()
    return ("text:" + text, "", "") if text else ("", "", "")


def split_activities(text: str | None) -> list[str]:
    """The "; "-separated activities of CompanyFacts.activities, cleaned, in order."""
    return [a for a in (clean(x) for x in (text or "").split(";")) if a]


# ---- holding type and share class (issue #39) ------------------------------------
#
# A stake (`OWNS`) is `direct` unless something says otherwise: `nominee` when the
# holder keeps the shares for clients (a custodian bank, a brokerage's client account,
# a depository, a trust), `treasury` for a company's own shares, `unknown` when a
# source says it can't tell. Nominee holders come from data/reference/nominees.csv
# (holdings.py) and from the name rules below; a source's own `holding_type` wins.

HOLDING_TYPES = ("direct", "nominee", "treasury", "unknown")

# Names of holders that keep shares for others. Stems, matched from a word start.
_NOMINEE_RULES = (
    ("custodian", r"кастоди|custod|depositor|depositar|хадгаламжийн төв"),
    ("nominee", r"nominee|номини|omnibus|client\w*\s+account|for the account of|a/c(?![\w])|"
                r"харилцагч\w*\s+(?:\w+\s+)?данс"),
    ("trust", r"итгэмжл(?:эл|эн|ийн)"),     # held under a trust deed; not "итгэмжлэгдсэн" (accredited)
)
_NOMINEE_RES = [(kind, re.compile(r"(?<![\w])(?:" + pat + ")", re.IGNORECASE)) for kind, pat in _NOMINEE_RULES]
# A company's own (treasury, bought-back) shares as a shareholder-table row.
_TREASURY_RE = re.compile(
    r"(?<![\w])(?:өөрийн хувьцаа|буцаан (?:худалдан|худалдаж) ав|эргүүлэн (?:худалдан|худалдаж) ав|"
    r"treasury|own shares|bought[- ]back)", re.IGNORECASE)

_HOLDING_WORDS = {      # checked in this order
    "unknown": ("unknown", "тодорхойгүй", "?"),
    "direct": ("direct", "шууд", "beneficial", "own account"),
    "nominee": ("nominee", "custodian", "custody", "broker", "trust", "omnibus", "client",
                "номини", "кастодиан", "брокер", "итгэмжлэл", "харилцагч"),
    "treasury": ("treasury", "own shares", "өөрийн", "буцаан"),
}
_SHARE_CLASSES = {
    "common": ("common", "ordinary", "энгийн", "жирийн"),
    "preferred": ("preferred", "preference", "давуу"),
}
_BASES = {
    "outstanding": ("outstanding", "issued", "total", "нийт", "гаргасан"),
    "voting": ("voting", "санал"),
}


def _vocab(raw: str | None, table: dict) -> str:
    v = clean(raw).lower()
    if not v:
        return ""
    if v in table:
        return v
    return next((k for k, words in table.items() if any(w in v for w in words)), "")


def holding_type_value(raw: str | None) -> str:
    """A source's holding type in the standard vocabulary (HOLDING_TYPES), "" if it gives none."""
    return _vocab(raw, _HOLDING_WORDS)


def share_class_value(raw: str | None) -> str:
    """"common" / "preferred", another class as the source spells it (lower case), or ""."""
    return _vocab(raw, _SHARE_CLASSES) or clean(raw).lower()


def basis_value(raw: str | None) -> str:
    """What a percentage is of: "outstanding" or "voting" shares, or "" when not said."""
    return _vocab(raw, _BASES)


def nominee_kind(name: str | None) -> str:
    """"custodian" / "nominee" / "trust" when a holder's name says it keeps shares for others."""
    n = clean(name)
    return next((kind for kind, rx in _NOMINEE_RES if rx.search(n)), "")


def is_treasury_name(name: str | None) -> bool:
    """A shareholder-table row for the company's own shares ("Өөрийн хувьцаа", "Treasury shares")."""
    return bool(_TREASURY_RE.search(clean(name)))
