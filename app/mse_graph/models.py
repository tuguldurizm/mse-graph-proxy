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
class Dataset:
    companies: list[Company] = field(default_factory=list)
    shareholdings: list[Shareholding] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)

    def extend(self, other: "Dataset") -> None:
        self.companies += other.companies
        self.shareholdings += other.shareholdings
        self.positions += other.positions

    def summary(self) -> dict:
        return {
            "companies": len(self.companies),
            "shareholdings": len(self.shareholdings),
            "positions": len(self.positions),
        }


def to_dict(obj) -> dict:
    return asdict(obj)
