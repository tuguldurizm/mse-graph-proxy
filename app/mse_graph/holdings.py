"""Holding types (issue #39): which stakes are held for others.

A stake (`OWNS`) has a `holding_type` (normalize.HOLDING_TYPES):

    direct     the holder owns the shares (the default)
    nominee    the holder keeps them for clients: a custodian bank, a brokerage's client
               account, a depository, a trust
    treasury   the company's own shares (an `OWNS` edge from the company to itself)
    unknown    a source says it can't tell

A source's own value (a `holding_type` column in a CSV) wins and is kept
(`holding_type_by: 'source'`). Otherwise the graph decides (`holding_type_by: 'rule'`,
Graph.classify_holdings): `treasury` for a company's edge to itself, `nominee` when the
holder is a nominee, else `direct`. A holder is a nominee (`nominee: true`, `nominee_kind`,
`nominee_by`) when it is in ``data/reference/nominees.csv`` (or ``$NOMINEES_FILE``), by
name, alias or register number (`nominee_by: 'list'`), or when its name says so
(normalize.nominee_kind, `nominee_by: 'name'`).

`share_class` (common, preferred, ...) and `basis` (the percent is of outstanding or of
voting shares) are kept when the source gives them. A holder has one open stake per
company, so a source that lists two classes for one holder keeps the last.

The analyses count only `direct` and `unknown` stakes as ownership (REAL): control and
look-through never pass through a nominee or treasury stake, and nominees are listed
apart from common owners (Graph.nominee_holders, analysis.nominees).
"""
from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import clean, entity_key, nominee_kind

NOMINEES_FILE = "data/reference/nominees.csv"
NOMINEE_KINDS = ("custodian", "broker", "nominee", "trust")

# Cypher test for a stake that is ownership, for the relationship variable `r`.
REAL = "coalesce({r}.holding_type, 'direct') IN ['direct', 'unknown']"


def real(var: str = "r") -> str:
    return REAL.format(r=var)


@dataclass
class Nominee:
    name: str
    register_no: str = ""
    kind: str = "nominee"
    aliases: list[str] = field(default_factory=list)
    note: str = ""


def nominees_path(path: str | os.PathLike | None = None) -> Path:
    return Path(path or os.getenv("NOMINEES_FILE") or NOMINEES_FILE)


def read_nominees(path: str | os.PathLike | None = None) -> list[Nominee]:
    """The rows of the nominees list; a missing file has none. Lines starting with # are notes."""
    p = nominees_path(path)
    if not p.is_file():
        return []
    lines = [ln for ln in p.read_text(encoding="utf-8-sig").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    out = []
    for row in csv.DictReader(io.StringIO("\n".join(lines))):
        name = clean(row.get("name"))
        if not name:
            continue
        kind = clean(row.get("kind")).lower()
        out.append(Nominee(name, clean(row.get("register_no")), kind if kind in NOMINEE_KINDS else "nominee",
                           [a for a in (clean(x) for x in (row.get("aliases") or "").split("|")) if a],
                           clean(row.get("note"))))
    return out


class NomineeIndex:
    """Looks a holder up in the nominees list by key, alias key or register number."""

    def __init__(self, entries: list[Nominee]):
        self.by_key: dict[str, str] = {}
        self.by_reg: dict[str, str] = {}
        for n in entries:
            for name in [n.name, *n.aliases]:
                for k in (entity_key(name), entity_key(name, "organization")):
                    self.by_key.setdefault(k, n.kind)
            if n.register_no:
                self.by_reg.setdefault(n.register_no, n.kind)

    def lookup(self, key: str, name: str, aliases=(), register_no: str = "") -> tuple[str, str] | None:
        """(kind, by) for a holder that is a nominee, else None."""
        if register_no and register_no in self.by_reg:
            return self.by_reg[register_no], "list"
        for k in [key, *(aliases or ()), entity_key(name or ""), entity_key(name or "", "organization")]:
            if k in self.by_key:
                return self.by_key[k], "list"
        kind = nominee_kind(name)
        return (kind, "name") if kind else None
