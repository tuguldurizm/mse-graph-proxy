from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Company:
    ticker: str                      # MSE symbol, e.g. "APU" - primary key
    name: str
    name_en: str = ""
    mse_id: str = ""                 # numeric id used in mse.mn URLs
    sector: str = ""
    # A number as the source gave it (either kind). The loader sorts it into
    # register_no / state_reg_no (normalize.split_reg_numbers), so the graph only
    # stores those two.
    reg_no: str = ""
    address: str = ""
    website: str = ""
    listed: bool = True
    url: str = ""
    # Other names the company is recorded under, "|"-separated (e.g. a former name
    # still used by older registry records). Shareholder rows under any of them
    # link to this company.
    other_names: str = ""
    # The registry's 7-digit register number (Регистрийн дугаар, APU: 2702673): one
    # company node per register number (issue #35).
    register_no: str = ""
    # mse.mn's 10-digit state registration number (APU: 9010001005).
    state_reg_no: str = ""


@dataclass
class Shareholding:
    company_ticker: str
    holder_name: str
    percent: float | None = None
    shares: int | None = None
    holder_type: str = ""            # "person" | "organization" | "" (auto-detect)
    as_of: str = ""
    source: str = ""
    # Set when the holder is a company reporting its own stake in `company_ticker`
    # (a subsidiary or associate, from its "stakes in other companies" table).
    holder_ticker: str = ""
    # For an organisation: "state" | "foreign" | "fund" | "other" when the source says
    # so; otherwise the loader guesses from the name (normalize.org_type_from_name).
    org_type: str = ""
    # What kind of holding it is (issue #39, holdings.py): "direct" | "nominee" (held for
    # clients: custodian, broker, trust) | "treasury" (the company's own shares) | "unknown".
    # Empty: the graph decides from the holder (data/reference/nominees.csv, name rules).
    holding_type: str = ""
    share_class: str = ""            # "common" | "preferred" | another class, when the source says
    basis: str = ""                  # the percent is of "outstanding" or of "voting" shares


@dataclass
class Position:
    company_ticker: str
    person_name: str
    title: str
    role: str = "executive"          # "executive" | "board"
    since: str = ""
    until: str = ""
    source: str = ""


@dataclass
class BeneficialOwner:
    """Ultimate (beneficial) owner as declared to the state registry - no percentage."""
    company_ticker: str
    owner_name: str
    owner_type: str = ""             # "person" | "organization" | ""
    since: str = ""
    source: str = ""
    # The owner's own registration number, when the registry gives one (legal
    # entities in the ownership chain). Discovery follows it (discovery.py).
    owner_reg_no: str = ""
    org_type: str = ""               # as Shareholding.org_type


@dataclass
class Affiliation:
    """A related party a company discloses for one of its shareholders (issue #10): open.mse.mn's
    "Түүнтэй нэгдмэл сонирхолтой этгээд" table in the holder's row. `relation` is the page's word
    ("эхнэр", "охин компани", "Хувьцаа эзэмшигч"); affiliations.relation_std() classifies it.
    The party's shares and percent are its own stake in the company (mostly 0)."""
    company_ticker: str
    holder_name: str
    party_name: str
    relation: str = ""
    party_shares: int | None = None
    party_percent: float | None = None
    holder_type: str = ""            # "person" | "organization" | "" (auto-detect)
    party_type: str = ""
    source: str = ""


@dataclass
class CompanyFacts:
    """Registry facts for an existing company. Loading only fills empty properties,
    so it never overwrites what mse.mn says. Every fact is also kept as a dated
    (:Fact) of the company (graph.py, issue #38)."""
    ticker: str
    reg_no: str = ""                 # either kind of number, as Company.reg_no
    legal_form: str = ""             # e.g. "Хувьцаат компани"
    founded: str = ""
    address: str = ""
    registry_name: str = ""          # name as registered (often without ХК / ХХК)
    registry_updated: str = ""
    registry_url: str = ""
    # The 7-digit register number (Регистрийн дугаар) that Open Data Lab pages use.
    # Unlike a company row's, it replaces the one the graph has: the registry page
    # (or discovery) is the authority on it.
    register_no: str = ""
    # The registry's activities ("Үйл ажиллагаа"), "; "-separated "<code> - <description>",
    # main activity first, e.g. "4610 - Гадаад худалдаа; 5920 - Контентийн үйлчилгээ".
    # They replace the company's `activities` / `activity_codes` and give `sector_std`
    # (issue #38). Empty leaves them as they are.
    activities: str = ""


@dataclass
class DeclaredInterest:
    """A public official whose asset and income declaration (ХОМ) mentions a company, one row
    per declaration year, from the company's Open Data Lab registry page (issue #9). A disclosed
    interest, not a finding."""
    official_name: str               # as the page writes it, an initial form: "Ц.Алтанцэцэг"
    position: str                    # agency and title in one text, as the page gives them
    year: str = ""                   # the declaration's year
    report_url: str = ""             # the declaration on Open Data Lab (…/search/<id>?model=XReport)
    iaac: bool = False               # files with the IAAC (АТГ-т мэдүүлгээ бүртгүүлдэг: senior officials)


@dataclass
class RegistryLists:
    """The lists on a company's registry page that are not owners or officers (issue #9): the
    officials whose declarations name it, its special licences and the public tenders it took
    part in. A page read is the complete picture of these lists for that company."""
    ticker: str
    source: str                      # the registry page
    declarations: list[DeclaredInterest] = field(default_factory=list)
    licenses: list[str] = field(default_factory=list)    # "<code> <name> | <issuer> | <valid from / to>"
    tenders: list[str] = field(default_factory=list)     # "<status>: <tender name>"


@dataclass
class Dataset:
    companies: list[Company] = field(default_factory=list)
    shareholdings: list[Shareholding] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    beneficial_owners: list[BeneficialOwner] = field(default_factory=list)
    facts: list[CompanyFacts] = field(default_factory=list)
    # officials' declared interests, licences, tenders (registry pages, issue #9)
    registry_lists: list[RegistryLists] = field(default_factory=list)
    # related parties disclosed per shareholder (open.mse.mn, affiliations.py)
    affiliations: list[Affiliation] = field(default_factory=list)
    # Date (YYYY-MM-DD) the data was observed, e.g. the scrape date. The loader
    # stamps first_seen / last_seen / until with it; empty means "today".
    observed_at: str = ""
    # Which parts of this dataset are a *complete* picture of a company right
    # now, as {"shareholdings" | "positions" | "holdings": {ticker: source_prefix}}.
    # "holdings" is keyed by the *holder*: the company's own stakes in other
    # companies (rows with holder_ticker set). For those,
    # the loader closes open edges (sets `until`) whose source starts with the
    # prefix but that are missing from this dataset - i.e. people who left and
    # holders who sold. An Open Data Lab registry page marks its company too,
    # which closes only registry-sourced edges. Manual CSVs leave this empty, so
    # they only ever add or refresh records. "beneficial_owners" works the same.
    snapshots: dict[str, dict[str, str]] = field(default_factory=dict)

    # A supplementary source (the Open Data Lab registry) adds to edges other
    # sources own but never takes them over: its snapshots close only its own
    # edges, and its values never replace another source's.
    supplementary: bool = False

    def mark_snapshot(self, kind: str, ticker: str, source_prefix: str) -> None:
        self.snapshots.setdefault(kind, {})[ticker.upper()] = source_prefix

    def extend(self, other: "Dataset") -> None:
        self.companies += other.companies
        self.shareholdings += other.shareholdings
        self.positions += other.positions
        self.beneficial_owners += other.beneficial_owners
        self.facts += other.facts
        self.registry_lists += other.registry_lists
        self.affiliations += other.affiliations
        self.observed_at = self.observed_at or other.observed_at
        self.supplementary = self.supplementary or other.supplementary
        for kind, scopes in other.snapshots.items():
            self.snapshots.setdefault(kind, {}).update(scopes)

    def summary(self) -> dict:
        out = {
            "companies": len(self.companies),
            "shareholdings": len(self.shareholdings),
            "positions": len(self.positions),
        }
        if self.beneficial_owners:
            out["beneficial_owners"] = len(self.beneficial_owners)
        if self.facts:
            out["company_facts"] = len(self.facts)
        if self.affiliations:
            out["affiliations"] = len(self.affiliations)
        if any(r.declarations for r in self.registry_lists):
            out["declared_interests"] = sum(len(r.declarations) for r in self.registry_lists)
        return out


def to_dict(obj) -> dict:
    return asdict(obj)
