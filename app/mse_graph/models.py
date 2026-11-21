from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Company:
    ticker: str                      # MSE symbol, e.g. "APU" - primary key
    name: str
    name_en: str = ""
    mse_id: str = ""                 # numeric id used in mse.mn URLs
    sector: str = ""
    reg_no: str = ""                 # state registration number
    address: str = ""
    website: str = ""
    listed: bool = True
    url: str = ""


@dataclass
class Shareholding:
    company_ticker: str
    holder_name: str
    percent: float | None = None
    shares: int | None = None
    holder_type: str = ""            # "person" | "organization" | "" (auto-detect)
    as_of: str = ""
    source: str = ""


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


@dataclass
class CompanyFacts:
    """Registry facts for an existing company. Loading only fills empty properties,
    so it never overwrites what mse.mn says."""
    ticker: str
    reg_no: str = ""
    legal_form: str = ""             # e.g. "Хувьцаат компани"
    founded: str = ""
    address: str = ""
    registry_name: str = ""          # name as registered (often without ХК / ХХК)
    registry_updated: str = ""
    registry_url: str = ""


@dataclass
class Dataset:
    companies: list[Company] = field(default_factory=list)
    shareholdings: list[Shareholding] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    beneficial_owners: list[BeneficialOwner] = field(default_factory=list)
    facts: list[CompanyFacts] = field(default_factory=list)
    # Date (YYYY-MM-DD) the data was observed, e.g. the scrape date. The loader
    # stamps first_seen / last_seen / until with it; empty means "today".
    observed_at: str = ""
    # Which parts of this dataset are a *complete* picture of a company right
    # now, as {"shareholdings" | "positions": {ticker: source_prefix}}. For those,
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
        return out


def to_dict(obj) -> dict:
    return asdict(obj)
