"""CSV import/export.

CSV is the hand-off format between the scraper (or manual research from
annual reports / FRC filings) and the Neo4j loader. A data directory holds:

    companies.csv      ticker,name,name_en,mse_id,sector,reg_no,address,website,listed,url
    shareholders.csv   company_ticker,holder_name,percent,shares,holder_type,as_of,source
    executives.csv     company_ticker,person_name,title,role,since,until,source
"""
from __future__ import annotations

import csv
from dataclasses import fields
from pathlib import Path

from .models import Company, Dataset, Position, Shareholding
from .normalize import clean, parse_int, parse_percent

FILES = {
    "companies": ("companies.csv", Company),
    "shareholdings": ("shareholders.csv", Shareholding),
    "positions": ("executives.csv", Position),
}


def _coerce(cls, row: dict):
    kwargs = {}
    for f in fields(cls):
        raw = row.get(f.name)
        if raw is None:
            continue
        raw = clean(raw)
        if f.name == "percent":
            kwargs[f.name] = parse_percent(raw)
        elif f.name == "shares":
            kwargs[f.name] = parse_int(raw)
        elif f.name == "listed":
            kwargs[f.name] = raw.lower() not in ("0", "false", "no", "")
        else:
            kwargs[f.name] = raw
    return cls(**kwargs)


def read_dir(path: str | Path) -> Dataset:
    path = Path(path)
    ds = Dataset()
    for attr, (fname, cls) in FILES.items():
        fpath = path / fname
        if not fpath.exists():
            continue
        with fpath.open(encoding="utf-8-sig", newline="") as fh:
            items = [_coerce(cls, row) for row in csv.DictReader(fh)]
        getattr(ds, attr).extend(items)
    return ds


def write_dir(ds: Dataset, path: str | Path) -> None:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    for attr, (fname, cls) in FILES.items():
        cols = [f.name for f in fields(cls)]
        with (path / fname).open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for item in getattr(ds, attr):
                w.writerow({c: getattr(item, c) for c in cols})
