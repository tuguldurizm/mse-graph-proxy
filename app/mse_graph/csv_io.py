"""CSV import/export.

CSV is the hand-off format between the scraper (or manual research from
annual reports / FRC filings) and the Neo4j loader. A data directory holds:

    companies.csv      ticker,name,name_en,mse_id,sector,register_no,address,website,listed,url,other_names
                       (+ state_reg_no; a `reg_no` column of either kind of number still works)
    shareholders.csv   company_ticker,holder_name,percent,shares,holder_type,as_of,source,holder_ticker
                       (+ org_type: state | foreign | fund | other, optional)
    executives.csv     company_ticker,person_name,title,role,since,until,source
    manifest.json      {"observed_at": "YYYY-MM-DD", "snapshots": {...}, "supplementary": true}   (optional)

Files may also be gzipped (shareholders.csv.gz, ...), as in the archive.

The archive (`archive()`) keeps one gzipped directory per scrape date,
`data/archive/YYYY-MM-DD/`, so the full history in Neo4j can be rebuilt from
the raw observations (`cli rebuild-history`).

Open Data Lab enrichment adds two optional files:

    beneficial_owners.csv  company_ticker,owner_name,owner_type,since,source,owner_reg_no,org_type
    company_facts.csv      ticker,reg_no,legal_form,founded,address,registry_name,registry_updated,registry_url,register_no,
                           activities  ("; "-separated "<code> - <description>", main first; issue #38)
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import re
from dataclasses import fields
from pathlib import Path
from typing import IO

from .models import BeneficialOwner, Company, CompanyFacts, Dataset, Position, Shareholding
from .normalize import clean, parse_int, parse_percent

FILES = {
    "companies": ("companies.csv", Company),
    "shareholdings": ("shareholders.csv", Shareholding),
    "positions": ("executives.csv", Position),
    "beneficial_owners": ("beneficial_owners.csv", BeneficialOwner),
    "facts": ("company_facts.csv", CompanyFacts),
}
OPTIONAL = {"beneficial_owners", "facts"}   # only written when non-empty


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


MANIFEST = "manifest.json"


def _open(fpath: Path, mode: str) -> IO[str]:
    if fpath.suffix == ".gz":
        return gzip.open(fpath, mode + "t", encoding="utf-8-sig" if mode == "r" else "utf-8", newline="")
    return fpath.open(mode, encoding="utf-8-sig" if mode == "r" else "utf-8", newline="")


def read_dir(path: str | Path) -> Dataset:
    path = Path(path)
    ds = Dataset()
    for attr, (fname, cls) in FILES.items():
        fpath = path / fname
        if not fpath.exists():
            fpath = path / (fname + ".gz")
            if not fpath.exists():
                continue
        with _open(fpath, "r") as fh:
            items = [_coerce(cls, row) for row in csv.DictReader(fh)]
        getattr(ds, attr).extend(items)
    mpath = path / MANIFEST
    if mpath.exists():
        meta = json.loads(mpath.read_text(encoding="utf-8"))
        ds.observed_at = meta.get("observed_at", "")
        ds.snapshots = {k: dict(v) for k, v in (meta.get("snapshots") or {}).items()}
        ds.supplementary = bool(meta.get("supplementary"))
    return ds


def write_dir(ds: Dataset, path: str | Path, compress: bool = False) -> None:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    for attr, (fname, cls) in FILES.items():
        target = path / (fname + ".gz" if compress else fname)
        stale = path / (fname if compress else fname + ".gz")
        stale.unlink(missing_ok=True)
        if attr in OPTIONAL and not getattr(ds, attr):
            target.unlink(missing_ok=True)      # don't leave an older run's file behind
            continue
        cols = [f.name for f in fields(cls)]
        with _open(target, "w") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for item in getattr(ds, attr):
                w.writerow({c: getattr(item, c) for c in cols})
    mpath = path / MANIFEST
    if ds.observed_at or ds.snapshots or ds.supplementary:
        meta = {"observed_at": ds.observed_at, "snapshots": ds.snapshots}
        if ds.supplementary:
            meta["supplementary"] = True
        mpath.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    else:
        mpath.unlink(missing_ok=True)


_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def archive(ds: Dataset, root: str | Path = "data/archive", part: str = "") -> Path:
    """Store a scrape as data/archive/<observed_at>/[<part>/]*.csv.gz (a few hundred KB).

    The main scrape goes in the date directory itself; extra sources loaded the
    same day (e.g. part="opendatalab") go in a subdirectory and are replayed
    after it. A second scrape on the same day replaces that day's files.
    """
    if not ds.observed_at:
        raise ValueError("archive() needs ds.observed_at")
    target = Path(root) / ds.observed_at
    if part:
        target = target / part
    write_dir(ds, target, compress=True)
    return target


def archived_snapshots(root: str | Path = "data/archive") -> list[Path]:
    """Archived data directories in the order they must be loaded: by date,
    each day's main scrape before its extra sources."""
    root = Path(root)
    if not root.is_dir():
        return []
    out = []
    for day in sorted(p for p in root.iterdir() if p.is_dir() and _DATE_DIR.match(p.name)):
        if any(day.glob("*.csv*")):
            out.append(day)
        out += sorted(p for p in day.iterdir() if p.is_dir())
    return out


MANUAL_ROOT = "data/manual"


def manual_dirs(root: str | Path = MANUAL_ROOT) -> list[Path]:
    """Hand-curated datasets kept in the repository, one sub-directory each
    (e.g. data/manual/shunkhlai), in name order."""
    root = Path(root)
    if not root.is_dir():
        return []
    return [d for d in sorted(root.iterdir())
            if d.is_dir() and any((d / fname).exists() for fname, _ in FILES.values())]


def dir_digest(path: str | Path) -> str:
    """Fingerprint of a data directory's files, to tell whether it changed since it was loaded."""
    h = hashlib.sha1()
    for f in sorted(Path(path).iterdir()):
        if f.is_file() and (f.name in {fname for fname, _ in FILES.values()} | {MANIFEST}):
            h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def is_demo(ds: Dataset) -> bool:
    from .graph import DEMO_SOURCE
    sources = {x.source for x in ds.shareholdings + ds.positions}
    return bool(sources) and sources <= {DEMO_SOURCE}
