"""Source records: the rows behind the graph's relationships (issue #37).

Each row a source reports becomes one ``(:Record)``, between the raw data and the
resolved entities:

    (:Record {record_id, kind, source, source_url, raw_company, raw_name, raw_*,
              name_key, observed_at, first_seen, last_seen})
    (:Record)-[:REPORTED_FOR]->(:Company)          the company the row was reported for
    (:Record)-[:REFERS_TO {method, confidence, decided_by, decided_at}]->(:Person|:Organization|:Company)

``kind`` is ``shareholder``, ``officer``, ``beneficial_owner`` or ``company``.
``record_id`` is a hash of the kind, the source, the company as the source named it
and the row's content, so a row seen again on a later day is the same record: a
load only moves its ``last_seen``. A row whose content changes (a new percentage, a
respelt title) is a new record.

Relationships (``OWNS``, ``HOLDS_POSITION``, ``BENEFICIAL_OWNER``) list the records
behind them in ``records``. Edges loaded before #37 have none until their source
reports them again.

``REFERS_TO.method`` says how the loader decided who the row names:

=================  =========================================================
``exact_key``      the row's own name key (normalize.entity_key)
``initial_match``  an initial form ("Б.Батболд") linked to the one full-name person
                   at the company that fits it (issue #36)
``alias``          a shareholder named like a company in the graph
``holder_ticker``  a company reporting its own stake in another company
``register_no``    a beneficial owner named by its registration number
``manual_override`` a row of the identity overrides file (below)
=================  =========================================================

Identity overrides
------------------
``data/overrides/identity.csv`` (or ``$IDENTITY_OVERRIDES``) holds decisions made by
hand: who the rows that spell ``name`` at ``company`` are.

    company,name,target,source,kind,decided_by,decided_at,note

* ``company``: a ticker or register number, or ``*`` for every company;
* ``name``: the name as a source spells it. Any spelling with the same name key
  matches ("Б.Батболд" also matches "Батболд Б.");
* ``target``: the entity key the rows belong to: ``person:…``, ``org:…`` or
  ``company:<ticker or register no>``. A person key may carry a ``#`` suffix to
  tell namesakes apart (``person:б батболд#2``). The loader never re-keys such a node;
* ``source`` (optional): only rows whose source starts with it;
* ``kind`` (optional): ``shareholder``, ``officer`` or ``beneficial_owner``.

Lines starting with ``#`` are comments. The loader applies a matching row as
``manual_override`` when it resolves a row, and Graph.apply_overrides re-points
records already in the graph, then rebuilds only the relationships they are behind.
Because the decisions live in this file, not only in the graph, they survive
``rebuild-history``.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .normalize import clean, entity_key

OVERRIDES_FILE = "data/overrides/identity.csv"
# Part of the manual datasets' digest (manual.load_manual): when records change, each reloads once.
RECORDS_VERSION = "records-v1"

KINDS = ("shareholder", "officer", "beneficial_owner", "company")
METHODS = ("exact_key", "initial_match", "alias", "holder_ticker", "register_no", "manual_override")
CONFIDENCE = {"exact_key": 1.0, "holder_ticker": 1.0, "register_no": 1.0, "manual_override": 1.0,
              "treasury": 1.0, "alias": 0.9, "initial_match": 0.8}

# the dataset fields that are the company a row was reported for, per kind
_COMPANY_FIELD = {"shareholder": "company_ticker", "officer": "company_ticker",
                  "beneficial_owner": "company_ticker", "company": "ticker"}
# raw fields stored on the record, per kind: (record property, row attribute)
_RAW = {
    "shareholder": (("raw_name", "holder_name"), ("raw_percent", "percent"), ("raw_shares", "shares"),
                    ("raw_as_of", "as_of"), ("raw_type", "holder_type"), ("raw_holder_ticker", "holder_ticker"),
                    ("raw_org_type", "org_type"), ("raw_holding_type", "holding_type"),
                    ("raw_share_class", "share_class"), ("raw_basis", "basis")),
    "officer": (("raw_name", "person_name"), ("raw_title", "title"), ("raw_role", "role"),
                ("raw_since", "since"), ("raw_until", "until")),
    "beneficial_owner": (("raw_name", "owner_name"), ("raw_type", "owner_type"), ("raw_since", "since"),
                         ("raw_reg_no", "owner_reg_no"), ("raw_org_type", "org_type")),
    "company": (("raw_name", "name"), ("raw_name_en", "name_en"), ("raw_register_no", "register_no"),
                ("raw_state_reg_no", "state_reg_no"), ("raw_reg_no", "reg_no")),
}
_PARTS = {"shareholder": "shareholdings", "officer": "positions", "beneficial_owner": "beneficial_owners",
          "company": "companies"}


def record_id(kind: str, row) -> str:
    """The stable id of a source row: a hash of its kind, source, company and content."""
    content = {k: v for k, v in asdict(row).items() if v not in ("", None)}
    raw = json.dumps([kind, content], ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def stamp(ds) -> None:
    """Give every row of `ds` its record id and the company as the source named it.

    Runs before Graph.canonicalize rewrites tickers, so a row's id doesn't depend on
    how the graph happens to key its company today. Idempotent."""
    for kind, part in _PARTS.items():
        for row in getattr(ds, part):
            if getattr(row, "_rid", None):
                continue
            row._rid = record_id(kind, row)
            row._raw_company = getattr(row, _COMPANY_FIELD[kind])


def name_key(name: str, kind_hint: str = "") -> str:
    """The key overrides and records are matched on (normalize.entity_key of the spelling)."""
    return entity_key(clean(name), kind_hint or "")


def record_row(kind: str, row, ticker: str, key: str, method: str, override: "Override | None" = None) -> dict:
    """The parameters Graph._write_records takes for one resolved row. `override` is the
    overrides-file row that decided it (method manual_override), else the rules did."""
    source = getattr(row, "source", "") or None             # company rows have no source column
    props = {"kind": kind, "source": source,
             "raw_company": getattr(row, "_raw_company", None) or getattr(row, _COMPANY_FIELD[kind])}
    src = source or (getattr(row, "url", "") if kind == "company" else "")
    props["source_url"] = src if (src or "").startswith(("http://", "https://")) else None
    for prop, attr in _RAW[kind]:
        v = getattr(row, attr, None)
        props[prop] = v if v not in ("", None) else None
    hint = {"shareholder": getattr(row, "holder_type", ""),
            "beneficial_owner": getattr(row, "owner_type", ""), "company": "organization"}.get(kind, "")
    props["name_key"] = name_key(props["raw_name"] or "", hint) if props["raw_name"] else None
    return {"id": row._rid, "ticker": ticker.upper(), "key": key, "method": method,
            "confidence": CONFIDENCE[method], "props": props,
            "by": (override.decided_by if override else "") or ("overrides file" if override else "rule"),
            "at": (override.decided_at if override else "") or None}


# ---- identity overrides --------------------------------------------------------------

@dataclass
class Override:
    company: str                # a ticker as the graph keeps it, or "*"
    name: str
    target: str                 # entity key
    source: str = ""            # source prefix, optional
    kind: str = ""              # record kind, optional
    decided_by: str = ""
    decided_at: str = ""
    note: str = ""

    @property
    def keys(self) -> set[str]:
        """The name keys this row matches: the spelling read by today's rules, as a person and as an
        organisation (records written before a name rule changed keep the key they had then)."""
        return {name_key(self.name), name_key(self.name, "organization"), name_key(self.name, "person")}

    @property
    def label(self) -> str:
        return label_of(self.target)

    def kinds(self) -> tuple[str, ...]:
        """The record kinds this row may apply to: only people hold positions."""
        allowed = KINDS[:3] if self.label == "Person" else ("shareholder", "beneficial_owner")
        return tuple(k for k in allowed if not self.kind or k == self.kind)

    def matches(self, kind: str, ticker: str, key: str, source: str | None) -> bool:
        return (kind in self.kinds() and self.company in ("*", ticker.upper()) and key in self.keys
                and (not self.source or (source or "").startswith(self.source)))


def label_of(key: str) -> str:
    return {"person": "Person", "org": "Organization", "company": "Company"}.get(key.split(":", 1)[0].lower(), "")


def overrides_path(path: str | os.PathLike | None = None) -> Path:
    return Path(path or os.getenv("IDENTITY_OVERRIDES") or OVERRIDES_FILE)


def read_overrides(path: str | os.PathLike | None = None) -> list[Override]:
    """The rows of the identity overrides file; a missing file has none."""
    p = overrides_path(path)
    if not p.is_file():
        return []
    lines = [ln for ln in p.read_text(encoding="utf-8-sig").splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    out = []
    for row in csv.DictReader(lines):
        company, name, target = (clean(row.get(k)) for k in ("company", "name", "target"))
        if not (company and name and target and label_of(target)):
            continue
        prefix, rest = target.split(":", 1)
        target = prefix.lower() + ":" + (rest.strip() if prefix.lower() == "company" else rest.strip().casefold())
        out.append(Override(company=company if company == "*" else company.upper(), name=name, target=target,
                            source=clean(row.get("source")), kind=clean(row.get("kind")).lower(),
                            decided_by=clean(row.get("decided_by")) or "overrides file",
                            decided_at=clean(row.get("decided_at")), note=clean(row.get("note"))))
    return out
