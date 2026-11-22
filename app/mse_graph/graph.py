"""Neo4j schema, loader and analytical queries.

Graph model
-----------
(:Entity:Company[:Listed] {key, ticker, name, name_en, sector, sector_mse, sector_std, sector_std_name,
                           activities, activity_codes, activity_main, register_no, state_reg_no, address, website,
                           mse_id, url, listed, aliases, former_names, former_tickers, former_keys})
(:Entity:Person       {key, name, display_name, name_form, full_name, patronymic, given_name, clan_name,
                       name_variants, former_keys})
(:Entity:Organization {key, name, org_type, register_no?})  -- shareholders outside the company lists

A company is one legal entity (issue #35): its key is `company:<register_no>`
(the registry's 7-digit number) when known, else `company:<TICKER>`; unlisted
companies have internal `~...` tickers. `state_reg_no` is mse.mn's 10-digit
number. Nodes found to be the same entity are merged (consolidate_companies), and
their old tickers and keys still resolve (resolve_key, resolve_ticker). An
organisation's `org_type` is "state", "foreign", "fund" or "other": from its
source when it says (`org_type_by: 'source'`), else from its name.

(holder:Entity)-[:OWNS {percent, shares, as_of, source, sources, <history>}]->(:Company)
(:Person)-[:HOLDS_POSITION {post, role_std, title, titles, role, since, source, sources, <history>}]->(:Company)
(owner:Entity)-[:BENEFICIAL_OWNER {since, source, <history>}]->(:Company)  -- declared to the registry (Open Data Lab)

(:Record {record_id, kind, source, raw_name, ...})-[:REFERS_TO {method, ...}]->(:Entity)
(:Record)-[:REPORTED_FOR]->(:Company)           -- the source rows behind the edges (records.py, issue #37)
(:Company)-[:HAS_FACT]->(:Fact {name, value, norm, source, current, first_seen, last_seen, until, closed_at,
                                opened_run, closed_run, baseline})  -- dated company facts (issue #38)

    <history> = current, first_seen, last_seen, until, closed_at, opened_run, closed_run, baseline, records

`records` lists the record_ids of the source rows behind an edge. Merges and
splits re-point a record's REFERS_TO and move the edges with it; the identity
overrides file (records.py) pins decisions made by hand (apply_overrides).

`source` is the source that owns an edge: only that source, or a complete
snapshot of the company, can change or close it. `sources` lists every source
that reported it (mse.mn and/or the Open Data Lab registry). A source without
a percentage never clears one that another source gave.

A position's `post` (normalize.post_key) is the seat it fills, e.g. "ceo" or
"board_member"; `role_std` is its standard role (normalize.ROLES_STD). Open
positions merge on (person, company, post), so a respelt title refreshes the
stint: `title` is the owning source's latest spelling, `titles` every spelling
seen. `role` is the source's coarse "board" / "executive".

A person's key is `person:<patronymic stem> <given>` for a full registry name, else its initial form
(`person:б батболд`). An initial-form row is the full-name person at a company where exactly one
fitting full name appears (PeopleResolver, _adopt_initials; issue #36). `name_variants` lists every
spelling seen as "<spelling> | <source>".

`key` is the unique merge key (see normalize.entity_key). A shareholder whose
name matches a listed company's name / English name is linked to that
:Company node, which is how cross-holdings between listed companies appear.

Company facts (issue #38)
-------------------------
A company's name, English name, address, legal form, sector (the company list's
text) and registry activity codes are also kept as dated (:Fact) nodes, one chain
per (fact name, source: "mse.mn", "opendatalab.mn" for a registry page, "dataset:<host>"
for other company rows). A load that
reports the value an open fact has refreshes it (`last_seen`; compared without
case); another value closes it (`until`, `closed_at`, `closed_run`) and opens a
new one. An empty value changes nothing. The node properties stay the current
value, for fast queries. Closed `name` facts are the company's `former_names`;
their spellings are kept in `former_aliases` and added to `aliases`, so an old
name in a shareholder table still links to the company. The first fact a source
gives for a company is its `baseline`, not a change.

`sector_mse` is the sector text of the company list (mse.mn for listed companies);
`sector_std` is the ISIC Rev. 4 section (A-U) of the registry's main activity code
`activity_main` (normalize.main_activity, isic_section), `sector_std_name` its name.
`sector` is what the UI shows: `sector_mse`, else `sector_std_name`.

History
-------
Every edge is one *period*: a stint in a job, or a stake at a given size.
Loads never wipe; they compare the new data with what is in the graph:

* a row that matches an open edge refreshes it (`last_seen`);
* a new row opens an edge (`current: true`, `first_seen`);
* a stake whose percent / share count changed closes the old edge and opens
  a new one, so the graph grows with real changes, not with each scrape;
* for a company the dataset marks as a complete snapshot, an open edge that
  is missing from it is closed (`current: false`, `until` = observation date).
  The person left / the holder sold somewhere between `last_seen` and `until`.

`since` / `until` / `as_of` from the source are normalised to ISO dates. All
"current picture" queries filter on `current`; past edges feed `entity()`
history, `moves()`, `changes()` and history.py (change alerts, timelines).

Each load has a run id. Edges it opens get `opened_run`. Edges it closes get
`closed_run` and `closed_at` (the observation date). A position recorded
straight from a source as already ended has neither, because it is history,
not a change. A company's `first_seen` / `first_run` mark when it entered
the graph, so its first holders and officers count as one `company_added`.
Likewise the edges a source opens the first time it reports a company (e.g.
the first Open Data Lab import) get `baseline: true` and are not changes.
"""
from __future__ import annotations

import dataclasses
import logging
import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any

from neo4j import GraphDatabase

from . import records as rec
from .config import settings
from .models import Dataset
from .normalize import ORG_TYPES, ORG_TYPES_VERSION, ROLES_VERSION, PersonName, clean, entity_key, is_aggregate, \
    is_organization, isic_section, main_activity, org_type_from_name, parse_activity, parse_date, person_name, \
    post_key, role_std, sector_std_name, split_activities, split_reg_numbers
from .opendatalab import REGISTER_NO_RE

log = logging.getLogger(__name__)

SCHEMA = [
    "CREATE CONSTRAINT entity_key IF NOT EXISTS FOR (e:Entity) REQUIRE e.key IS UNIQUE",
    "CREATE CONSTRAINT company_ticker IF NOT EXISTS FOR (c:Company) REQUIRE c.ticker IS UNIQUE",
    "CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)",
    "CREATE FULLTEXT INDEX entity_names IF NOT EXISTS FOR (e:Entity) ON EACH [e.name, e.name_en, e.ticker]",
    "CREATE INDEX owns_current IF NOT EXISTS FOR ()-[r:OWNS]-() ON (r.current)",
    "CREATE INDEX position_current IF NOT EXISTS FOR ()-[r:HOLDS_POSITION]-() ON (r.current)",
    "CREATE INDEX owns_until IF NOT EXISTS FOR ()-[r:OWNS]-() ON (r.until)",
    "CREATE INDEX position_until IF NOT EXISTS FOR ()-[r:HOLDS_POSITION]-() ON (r.until)",
    "DROP INDEX company_reg_no IF EXISTS",       # `reg_no` was split into the two below (#35)
    "CREATE INDEX company_register_no IF NOT EXISTS FOR (c:Company) ON (c.register_no)",
    "CREATE INDEX company_state_reg_no IF NOT EXISTS FOR (c:Company) ON (c.state_reg_no)",
    # registry numbers discovery tried and should leave alone for a while (discovery.py)
    "CREATE CONSTRAINT registry_lead_reg IF NOT EXISTS FOR (l:RegistryLead) REQUIRE l.reg IS UNIQUE",
    # source records (issue #37, records.py)
    "CREATE CONSTRAINT record_id IF NOT EXISTS FOR (r:Record) REQUIRE r.record_id IS UNIQUE",
    "CREATE INDEX record_name_key IF NOT EXISTS FOR (r:Record) ON (r.name_key)",
    # decisions made on the review queue (issue #6, review.py); wipes keep them
    "CREATE CONSTRAINT decision_id IF NOT EXISTS FOR (d:Decision) REQUIRE d.id IS UNIQUE",
    # dated company facts (issue #38)
    "CREATE INDEX fact_name IF NOT EXISTS FOR (f:Fact) ON (f.name, f.source)",
]
# company properties kept as dated facts (issue #38): fact name -> company-row field
COMPANY_FACTS = {"name": "name", "name_en": "name_en", "address": "address", "sector": "sector_mse"}

# Graphs before #38: the company list's sector text was `sector`.
MIGRATE_SECTORS = """
MATCH (c:Company) WHERE c.sector_mse IS NULL AND c.sector_std IS NULL AND coalesce(c.sector, '') <> ''
SET c.sector_mse = c.sector
"""
# the relationships source records are behind
EDGE_TYPES = "OWNS|HOLDS_POSITION|BENEFICIAL_OWNER"

# Graphs loaded before history tracking: every edge was the current state.
MIGRATE = """
MATCH ()-[r:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER]->() WHERE r.current IS NULL
SET r.since = CASE WHEN r.since = '' THEN null ELSE r.since END,
    r.until = CASE WHEN r.until = '' THEN null ELSE r.until END,
    r.as_of = CASE WHEN r.as_of = '' THEN null ELSE r.as_of END
SET r.current = r.until IS NULL,
    r.first_seen = coalesce(r.first_seen, $today),
    r.last_seen = coalesce(r.last_seen, $today)
"""

# Graphs loaded before change tracking: edges the loader closed have an
# `until` on or after the day they were first seen (a source's own end date
# is normally earlier); companies entered the graph with their first edge.
MIGRATE_CHANGES = [
    """
    MATCH ()-[r:OWNS|HOLDS_POSITION]->()
    WHERE NOT r.current AND r.closed_at IS NULL AND r.until >= r.first_seen
    SET r.closed_at = r.until
    """,
    """
    MATCH (c:Company) WHERE c.first_seen IS NULL
    OPTIONAL MATCH (c)<-[r:OWNS|HOLDS_POSITION]-()
    WITH c, min(r.first_seen) AS first
    SET c.first_seen = coalesce(first, $today)
    """,
]

DEMO_SOURCE = "DEMO-fictional"
# a source on this host (the state registry mirror) gives people's full names
REGISTRY_MARK = "opendatalab.mn"
# as_of values that compare correctly as strings (see normalize.parse_date)
ISO_DATE = r"\d{4}(-\d{2}){0,2}"
# Two stakes differing by less than this many percentage points count as equal.
PERCENT_TOLERANCE = 0.001

# the record kind behind each relationship type (records.py)
_REL_KIND = {"OWNS": "shareholder", "HOLDS_POSITION": "officer", "BENEFICIAL_OWNER": "beneficial_owner"}

_TYPE = "[l IN labels({0}) WHERE l IN ['Company', 'Person', 'Organization']][0]"


def _opt(value):
    """Empty strings become nulls so Neo4j drops the property."""
    return value if value not in ("", None) else None


def _under(source: str | None, prefix: str) -> bool:
    """`source` is the snapshot page itself or one of its sub-pages (…/tab/officials)."""
    source = source or ""
    return source == prefix or source.startswith(prefix.rstrip("/") + "/")


def _today() -> str:
    return date.today().isoformat()


def _days_ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def new_run_id() -> str:
    """A sortable id for one load: UTC time to the microsecond."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def company_key(ticker: str, register_no: str = "") -> str:
    """A company's node key: its register number when known, else its ticker (issue #35)."""
    reg, _ = split_reg_numbers(register_no)
    return "company:" + (reg or ticker.upper())


class PeopleResolver:
    """Which person node a person row names (issue #36).

    A full name or any other form is its own key (normalize.person_name). An initial
    form ("Б.Батболд") at company C is the full-name person whose name it can stand
    for, when exactly one such person is linked to C (by any edge, past or current,
    or by a row the dataset is loading: `pending`). Otherwise it keeps its own
    initial key, and Graph.ambiguous_people lists it for review.

    `key()` also records the spellings it saw (`noted`), for Graph._note_names.

    `overrides` (records.Override, tickers resolved: Graph.identity_overrides) come
    first: a row they match is their target (`manual_override`, issue #37).
    """

    def __init__(self, people: list[dict], pending: dict[str, set] | None = None,
                 overrides: list | None = None):
        self.names: dict[str, str] = {}                 # full key -> its name
        self.parsed: dict[str, PersonName] = {}
        self.at: dict[str, set[str]] = {}               # ticker -> full keys linked to it
        self.by_given: dict[str, set[str]] = {}         # given name -> full keys
        for p in people:
            if p["pn"].form == "full":
                self._add(p["key"], p["pn"].full_name or p["name"], p["pn"], p["companies"])
        for ticker, rows in (pending or {}).items():
            for key, name in rows:
                self._add(key, self.names.get(key, name), person_name(name), [ticker])
        self.noted: dict[str, dict] = {}
        self.overrides = list(overrides or [])

    def _add(self, key: str, name: str, pn: PersonName, tickers) -> None:
        self.names.setdefault(key, name)
        self.parsed.setdefault(key, pn)
        self.by_given.setdefault(pn.given.casefold(), set()).add(key)
        for t in tickers:
            self.at.setdefault(t, set()).add(key)

    def candidates(self, pn: PersonName, ticker: str | None = None) -> list[str]:
        """The full-name people `pn` (an initial form) can stand for: at `ticker`, or anywhere."""
        if pn.form != "initial":
            return []
        keys = self.by_given.get(pn.given.casefold(), set())
        if ticker is not None:
            keys = keys & self.at.get(ticker, set())
        return sorted(k for k in keys if pn.could_be(self.parsed[k]))

    def match_override(self, name: str, ticker: str, source: str | None, kind: str, hint: str = ""):
        """The first override row for the row `name` at `ticker` (a records.Override), or None."""
        if not self.overrides:
            return None
        nk = rec.name_key(name, hint)
        for o in self.overrides:
            if o.matches(kind, ticker, nk, source):
                if o.label == "Person":
                    self._note(o.target, name, source, None)
                return o
        return None

    def resolve(self, name: str, ticker: str, source: str = "", kind: str = "officer") -> tuple[str, str]:
        """(key, method) of the person row `name` at company `ticker` (cleaned name)."""
        o = self.match_override(name, ticker, source, kind)
        if o is not None and o.label == "Person":
            return o.target, "manual_override"
        pn = person_name(name)
        key, method = pn.key, "exact_key"
        if pn.form == "initial":
            cands = self.candidates(pn, ticker.upper())
            if len(cands) == 1:
                key, method = cands[0], "initial_match"
        self._note(key, name, source, pn)
        return key, method

    def key(self, name: str, ticker: str, source: str = "", kind: str = "officer") -> str:
        """The key of the person row `name` at company `ticker` (cleaned name)."""
        return self.resolve(name, ticker, source, kind)[0]

    def _note(self, key: str, name: str, source: str | None, pn: PersonName | None) -> None:
        seen = self.noted.setdefault(key, {"parts": {}, "variants": []})
        if pn is not None and key == pn.key:
            seen["parts"] = {**seen["parts"], **pn.parts()}
        variant = f"{name} | {source}" if source else name
        if variant not in seen["variants"]:
            seen["variants"].append(variant)


class Graph:
    def __init__(self, uri=None, user=None, password=None, database=None):
        uri = uri or settings.neo4j_uri
        auth = (user or settings.neo4j_user, password or settings.neo4j_password)
        self.database = database or settings.neo4j_database or None   # None: server default
        if uri.startswith(("http://", "https://")):
            # Query API over HTTPS, for environments that can't open Bolt (port 7687)
            from .http_driver import HttpDriver
            self.driver = HttpDriver(uri, auth, database=self.database)
            self.http = True
        else:
            self.driver = GraphDatabase.driver(uri, auth=auth, notifications_min_severity="OFF")
            self.http = False

    def close(self):
        self.driver.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def session(self):
        with self.driver.session(database=self.database) as s:
            yield s

    def run(self, cypher: str, **params) -> list[dict[str, Any]]:
        if self.http:
            return self.driver.run(cypher, params)
        with self.session() as s:
            return [r.data() for r in s.run(cypher, **params)]

    # ---- schema / admin -----------------------------------------------------

    def init_schema(self, overrides: list | None = None):
        for stmt in SCHEMA:
            self.run(stmt)
        self.run(MIGRATE, today=_today())
        for stmt in MIGRATE_CHANGES:
            self.run(stmt, today=_today())
        self.run(MIGRATE_SECTORS)
        self.migrate_positions()
        self.rekeyed_people = self.rekey_entities()
        # what the last start or load merged and re-keyed (the pipeline logs it)
        self.consolidated = self.consolidate_companies()
        self.adopted = self._adopt_initials()
        # decisions from the identity overrides file (issue #37); `overrides` replaces the file
        self.overridden = self.apply_overrides(overrides)
        self._derive_sectors()

    def migrate_positions(self) -> dict:
        """Give positions their `post` and `role_std`, and undo splits caused by respelt titles.

        Positions used to merge on the raw title, so "Гүйцэтгэх захирал" becoming
        "Гүйцэтгэх захирал (CEO)" closed one stint and opened another (issue #34).
        This sets `post` / `role_std` / `titles` on edges that lack them (on all
        edges when the role rules changed, see normalize.ROLES_VERSION), merges open
        edges that now share a seat, and re-joins a stint closed on the day another
        stint of the same seat opened. Safe to run on every start.
        """
        done = self.run("MATCH (m:Meta {key: 'roles'}) RETURN m.version AS v")
        redo = not done or done[0]["v"] != ROLES_VERSION
        pairs = self.run(
            "MATCH ()-[h:HOLDS_POSITION]->() WHERE $redo OR h.post IS NULL "
            "RETURN DISTINCT coalesce(h.title, '') AS title, coalesce(h.role, '') AS role", redo=redo)
        table = {r["role"] + "|" + r["title"]: {"post": post_key(r["title"], r["role"] or None),
                                                "role_std": role_std(r["title"], r["role"] or None)}
                 for r in pairs}
        tagged = 0
        if table:
            tagged = self.run(
                """
                MATCH ()-[h:HOLDS_POSITION]->() WHERE $redo OR h.post IS NULL
                WITH h, $table[coalesce(h.role, '') + '|' + coalesce(h.title, '')] AS m
                SET h.post = m.post, h.role_std = m.role_std,
                    h.titles = coalesce(h.titles, [x IN [h.title] WHERE x IS NOT NULL])
                RETURN count(h) AS n
                """, redo=redo, table=table)[0]["n"]
        merged = self._merge_open_posts()
        rejoined = self._rejoin_respelt_stints()
        self.run("MERGE (m:Meta {key: 'roles'}) SET m.version = $v", v=ROLES_VERSION)
        return {"tagged": tagged, "merged": merged, "rejoined": rejoined}

    def _set_edge(self, eid: str, props: dict) -> None:
        self.run("MATCH ()-[h]->() WHERE elementId(h) = $id SET h += $p", id=eid, p=props)

    def _drop_edges(self, ids: list[str]) -> None:
        if ids:
            self.run("MATCH ()-[h]->() WHERE elementId(h) IN $ids DELETE h", ids=ids)

    @staticmethod
    def _union(*lists) -> list:
        out = []
        for xs in lists:
            for x in xs or []:
                if x is not None and x not in out:
                    out.append(x)
        return out

    def _merge_open_posts(self) -> int:
        """Open positions of one person, company and seat become one edge."""
        groups = self.run(
            """
            MATCH (p:Person)-[h:HOLDS_POSITION {current: true}]->(c:Company)
            WITH p, c, h.post AS post, collect({id: elementId(h), e: properties(h)}) AS hs
            WHERE size(hs) > 1
            RETURN hs
            """)
        dropped = 0
        for g in groups:
            hs = sorted(g["hs"], key=lambda x: (x["e"].get("first_seen") or "", x["id"]))
            keep, rest = hs[0], hs[1:]
            # the identity and start come from the oldest edge, the wording from the latest seen
            latest = max(hs, key=lambda x: x["e"].get("last_seen") or "")["e"]
            es = [x["e"] for x in hs]
            sinces = [e.get("since") for e in es if e.get("since")]
            self._set_edge(keep["id"], {
                "title": latest.get("title"), "role": latest.get("role"), "role_std": latest.get("role_std"),
                "source": latest.get("source"),
                "titles": self._union(*[e.get("titles") or [e.get("title")] for e in es]),
                "sources": self._union(*[e.get("sources") or [e.get("source")] for e in es]),
                "records": self._union(*[e.get("records") for e in es]) or None,
                "last_seen": max(e.get("last_seen") or "" for e in es) or None,
                "since": min(sinces) if sinces else None,
            })
            self._drop_edges([x["id"] for x in rest])
            dropped += len(rest)
        return dropped

    def _rejoin_respelt_stints(self) -> int:
        """A stint closed on the day another stint of the same seat opened was one stint."""
        total = 0
        for _ in range(50):  # each pass joins one link of a chain of respellings
            pairs = self.run(
                """
                MATCH (p:Person)-[o:HOLDS_POSITION {current: false}]->(c:Company)<-[n:HOLDS_POSITION]-(p)
                WHERE o.post = n.post AND o.closed_at IS NOT NULL AND n.first_seen = o.closed_at
                  AND (o.closed_run IS NULL OR n.opened_run IS NULL OR o.closed_run = n.opened_run)
                RETURN elementId(o) AS oid, properties(o) AS o, elementId(n) AS nid, properties(n) AS n
                ORDER BY o.closed_at
                """)
            used: set[str] = set()
            joined = 0
            for r in pairs:
                if r["oid"] in used or r["nid"] in used:
                    continue
                used.update((r["oid"], r["nid"]))
                o, n = r["o"], r["n"]
                self._set_edge(r["nid"], {
                    "first_seen": o.get("first_seen"), "opened_run": o.get("opened_run"),
                    "baseline": o.get("baseline"), "since": o.get("since") or n.get("since"),
                    "titles": self._union(o.get("titles") or [o.get("title")], n.get("titles") or [n.get("title")]),
                    "sources": self._union(o.get("sources") or [o.get("source")],
                                           n.get("sources") or [n.get("source")]),
                    "records": self._union(o.get("records"), n.get("records")) or None,
                })
                self._drop_edges([r["oid"]])
                joined += 1
            total += joined
            if not joined:
                break
        return total

    def rekey_entities(self) -> int:
        """Give people and organisations the key the current entity_key() makes.

        When the key rules change, the next load would otherwise create new nodes and
        report everyone as having left and rejoined. Nodes that end up with the same
        key are merged (_merge_node). Returns the number of nodes re-keyed or merged.

        Issue #36: a full registry name ("Батын Батболд") used to share the initial
        key of "Б.Батболд", so one node may hold the registry's person at one company
        and MSE's namesakes at others. Such a node is split: the companies where a
        registry source reported it go to the full-name person, the others stay on the
        initial key (the node is renamed to the initial form). Only the registry's
        edges show that the full name was given there; every other company is an
        initial form, which _adopt_initials links again where the rule allows. People
        also get their name parts (normalize.PersonName.parts) here.
        """
        rows = self.run("""MATCH (e:Entity) WHERE (e:Person OR e:Organization) AND NOT e:Company
                           RETURN elementId(e) AS id, e.key AS key, e.name AS name, e:Organization AS org,
                                  e.name_form IS NULL AS untagged""")
        moves, splits = [], []
        for r in rows:
            name = r["name"] or ""
            if "#" in (r["key"] or ""):
                continue        # an identity from the overrides file (records.py): never re-keyed
            if r["org"]:
                new = entity_key(name, "organization")
            else:
                pn = person_name(name)
                new = pn.key
                if pn.form == "full" and r["key"] == pn.match_key != new:
                    splits.append((r, pn))
                    continue
            if new and new != r["key"] and not r["key"].startswith("company:"):
                moves.append((r["id"], new))
        split = set()
        for r, pn in splits:
            if self._split_full_name(r["id"], pn):
                split.add(r["id"])      # it is the initial form now, with that form's parts
                continue
            moves.append((r["id"], pn.key))
        for old, new in moves:
            target = self.run("MATCH (n:Entity {key: $new}) RETURN elementId(n) AS id", new=new)
            if target:
                self._merge_node(old, target[0]["id"])
            else:
                self.run("MATCH (o) WHERE elementId(o) = $id SET o.key = $new", id=old, new=new)
        untagged = [{"id": r["id"], "parts": person_name(r["name"] or "").parts()}
                    for r in rows if not r["org"] and r["untagged"] and r["id"] not in split]
        self.run("""UNWIND $rows AS r MATCH (p) WHERE elementId(p) = r.id
                    SET p += r.parts, p.display_name = coalesce(p.full_name, p.name)""", rows=untagged)
        return len(moves) + len(split)

    def _split_full_name(self, node: str, pn: PersonName) -> bool:
        """Split a node keyed by the initial form of its full name (see rekey_entities).

        Returns False when there is nothing to split (all its companies, or none, have
        registry evidence): the caller then re-keys the whole node."""
        rows = self.run(
            """
            MATCH (p)-[r:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER]->(c:Company) WHERE elementId(p) = $id
            WITH c, collect(coalesce(r.sources, []) + [r.source]) AS srcs
            RETURN elementId(c) AS id,
                   any(s IN reduce(a = [], x IN srcs | a + x) WHERE s CONTAINS $reg) AS registry
            """, id=node, reg=REGISTRY_MARK)
        full = [r["id"] for r in rows if r["registry"]]
        if not full or len(full) == len(rows):
            return False
        target = self._person_node(pn.key, pn.full_name, pn)
        for c in full:
            self._move_edges(node, target, only=c)
        self.run("""MATCH (p) WHERE elementId(p) = $id
                    SET p.name = $name, p.display_name = $name, p.name_form = 'initial'
                    REMOVE p.full_name, p.patronymic, p.clan_name""",
                 id=node, name=f"{pn.initial}.{pn.given}")
        return True

    def _person_node(self, key: str, name: str, pn: PersonName) -> str:
        """The element id of the person `key`, created if missing."""
        return self.run("""MERGE (p:Entity {key: $key}) ON CREATE SET p.name = $name
                           SET p:Person, p += $parts, p.display_name = coalesce(p.full_name, p.name)
                           RETURN elementId(p) AS id""", key=key, name=name, parts=pn.parts())[0]["id"]

    # ---- person identity (issue #36) --------------------------------------------

    def _people(self) -> list[dict]:
        """Every person with the companies it is linked to (any edge, past or current)."""
        rows = self.run(
            """
            MATCH (p:Person) WHERE NOT p:Company
            OPTIONAL MATCH (p)-[:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER]->(c:Company)
            RETURN elementId(p) AS id, p.key AS key, p.name AS name, p.full_name AS full_name,
                   collect(DISTINCT [c.ticker, elementId(c)]) AS cs
            """)
        out = []
        for r in rows:
            name = r["full_name"] or r["name"] or ""
            if is_organization(name):
                continue
            out.append({"id": r["id"], "key": r["key"], "name": r["name"], "pn": person_name(name),
                        "companies": {t: c for t, c in r["cs"] if t}})
        return out

    def people_resolver(self, pending: dict[str, set] | None = None,
                        overrides: list | None = None) -> "PeopleResolver":
        """The resolver the loader uses for the graph as it is now (see PeopleResolver).
        `overrides`: resolved override rows (identity_overrides); default: the file's."""
        if overrides is None:
            overrides = self.identity_overrides()
        return PeopleResolver(self._people(), pending, overrides)

    def _pinned(self) -> set[tuple[str, str]]:
        """(entity key, ticker) pairs that an override decided (records.py): the rules leave them alone."""
        return {(r["key"], r["t"]) for r in self.run(
            """MATCH (r:Record)-[:REFERS_TO {method: 'manual_override'}]->(e:Entity),
                     (r)-[:REPORTED_FOR]->(c:Company)
               RETURN DISTINCT e.key AS key, c.ticker AS t""")}

    def _adopt_initials(self, pending: dict[str, set] | None = None) -> int:
        """Move an initial-form person's edges at a company to the full-name person there.

        "Б.Батболд" at company C is "Батын Батболд" when he is the only full-name
        person at C that the initial can stand for (normalize.PersonName.could_be).
        `pending` ({ticker: {(key, name)}}) adds the full-name rows a dataset is about to
        load, so their edges are moved before the load refreshes them and nothing
        looks like a change. A node left without edges is merged into the person
        that took the last of them. Returns the number of companies moved.
        """
        people = self._people()
        res = PeopleResolver(people, pending)
        pinned = self._pinned()
        moved = 0
        for n in people:
            if n["pn"].form != "initial":
                continue
            left = dict(n["companies"])
            last = None
            for ticker, company in n["companies"].items():
                if (n["key"], ticker) in pinned:
                    continue        # an override put this person's rows here (issue #37)
                cands = res.candidates(n["pn"], ticker)
                if len(cands) != 1:
                    continue
                key = cands[0]
                target = self._person_node(key, res.names[key], person_name(res.names[key]))
                got = self.run(
                    """MATCH (p)-[r:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER]->(c)
                       WHERE elementId(p) = $p AND elementId(c) = $c
                       RETURN r.source AS s, r.sources AS ss""", p=n["id"], c=company)
                srcs = {s for g in got for s in [g["s"], *(g["ss"] or [])]}
                self._move_edges(n["id"], target, only=company, method="initial_match")
                # the spellings those edges' sources used go with them
                self.run(
                    """MATCH (o) WHERE elementId(o) = $o MATCH (t) WHERE elementId(t) = $t
                       WITH o, t, [v IN coalesce(o.name_variants, [])
                                   WHERE any(s IN $srcs WHERE v ENDS WITH ' | ' + s)] AS go
                       SET o.name_variants = [v IN coalesce(o.name_variants, []) WHERE NOT v IN go],
                           t.name_variants = coalesce(t.name_variants, []) +
                                             [v IN go WHERE NOT v IN coalesce(t.name_variants, [])]""",
                    o=n["id"], t=target, srcs=[s for s in srcs if s])
                left.pop(ticker)
                last = target
                moved += 1
            if last and not left:
                self._absorb_node(n["id"], last)
        return moved

    def ambiguous_people(self) -> list[dict]:
        """Initial-form people a full-name person may be, which the rule didn't link (#6 queue).

        `reason` is "several_at_company" when two or more full names at one of its
        companies fit the initial, else "no_shared_company": a full-name person that
        fits exists, but at other companies only, so it may be a namesake."""
        people = self._people()
        res = PeopleResolver(people)
        pinned = self._pinned()
        by_key = {p["key"]: p for p in people}
        out = []
        for n in people:
            # companies where an override already decided who the rows are: no question left
            open_ = sorted(t for t in n["companies"] if (n["key"], t) not in pinned)
            if n["pn"].form != "initial" or not open_:
                continue
            cands = res.candidates(n["pn"])
            if not cands:
                continue
            several = any(len(res.candidates(n["pn"], t)) > 1 for t in open_)
            out.append({
                "key": n["key"], "name": n["name"], "companies": open_,
                "reason": "several_at_company" if several else "no_shared_company",
                "candidates": [{"key": k, "name": by_key[k]["name"],
                                "companies": sorted(by_key[k]["companies"]),
                                "shared": sorted(set(by_key[k]["companies"]) & set(n["companies"]))}
                               for k in cands],
            })
        return sorted(out, key=lambda x: (x["reason"] != "several_at_company", x["name"] or ""))

    def _dataset_people(self, ds: Dataset, alias_map: dict[str, str],
                        overrides: list | None = None) -> dict[str, set]:
        """{ticker: {(key, name)}} for the full-name person rows of `ds` (for _adopt_initials).
        Rows an override decides are left out: they are who the override says."""
        reg_map = {r["reg"] for r in self.run(
            "MATCH (c:Company) WHERE c.register_no =~ $re RETURN c.register_no AS reg", re=REGISTER_NO_RE)}
        names = [(p.company_ticker, p.person_name, p.source, "officer") for p in ds.positions]
        names += [(h.company_ticker, h.holder_name, h.source, "shareholder") for h in ds.shareholdings
                  if not h.holder_ticker and not (h.holder_type or "").startswith("org")]
        names += [(b.company_ticker, b.owner_name, b.source, "beneficial_owner") for b in ds.beneficial_owners
                  if not (b.owner_type or "").startswith("org")
                  and "".join(ch for ch in (b.owner_reg_no or "") if ch.isdigit()) not in reg_map]
        res = PeopleResolver([], None, overrides)
        out: dict[str, set] = {}
        for ticker, name, source, kind in names:
            name = clean(name)
            if not name or is_aggregate(name) or is_organization(name):
                continue
            if res.match_override(name, ticker, source, kind) is not None:
                continue
            pn = person_name(name)
            if pn.form == "full" and pn.key not in alias_map and entity_key(name) not in alias_map:
                out.setdefault(ticker.upper(), set()).add((pn.key, name))
        return out

    def _note_names(self, noted: dict[str, dict]) -> None:
        """Record the spellings the load saw (`name_variants`, "<spelling> | <source>") and the
        name parts of rows whose own key is the person's (an initial form resolved to a full
        name adds its spelling, not its parts)."""
        self.run(
            """
            UNWIND $rows AS r
            MATCH (p:Person {key: r.key}) WHERE NOT p:Company
            SET p += r.parts,
                p.name_variants = coalesce(p.name_variants, []) +
                                  [v IN r.variants WHERE NOT v IN coalesce(p.name_variants, [])]
            SET p.display_name = coalesce(p.full_name, p.name)
            """, rows=[{"key": k, **v} for k, v in noted.items()])

    # ---- source records (issue #37, records.py) ---------------------------------

    def _write_records(self, rows: list[dict], when: str) -> None:
        """Store source records (records.record_row) and who each one names.

        A record seen before only gets a later `last_seen`. Its REFERS_TO is the load's
        decision; a changed decision (another entity or method) replaces the old one.
        `key` "" means the company the record was reported for (company rows)."""
        if not rows:
            return
        self.run(
            """
            UNWIND $rows AS x
            MATCH (c:Company {ticker: x.ticker})
            OPTIONAL MATCH (k:Entity {key: x.key})
            WITH x, c, CASE WHEN x.key = '' THEN c ELSE k END AS e
            WHERE e IS NOT NULL
            MERGE (r:Record {record_id: x.id})
            ON CREATE SET r += x.props, r.observed_at = $when, r.first_seen = $when
            SET r.last_seen = CASE WHEN coalesce(r.last_seen, '') < $when THEN $when ELSE r.last_seen END,
                r.first_seen = CASE WHEN r.first_seen > $when THEN $when ELSE r.first_seen END
            MERGE (r)-[:REPORTED_FOR]->(c)
            WITH r, x, e
            CALL (r, x, e) {
                MATCH (r)-[old:REFERS_TO]->(o) WHERE o <> e OR old.method <> x.method
                DELETE old
            }
            MERGE (r)-[t:REFERS_TO]->(e)
            ON CREATE SET t.method = x.method, t.confidence = x.confidence, t.decided_by = x.by,
                          t.decided_at = coalesce(x.at, $when)
            """, rows=rows, when=when)

    def identity_overrides(self, overrides: list | None = None) -> list:
        """The identity overrides in force (the overrides file and the review decisions
        stored in the graph, review.overrides_in_force; or `overrides`) with their
        companies and company targets resolved to the graph's tickers and keys. A row
        whose company target isn't in the graph is left out (and logged)."""
        if overrides is None:
            from .review import overrides_in_force
            rows = overrides_in_force(self)
        else:
            rows = list(overrides)
        if not rows:
            return []
        aliases = None
        out = []
        for o in rows:
            o = dataclasses.replace(o)
            if o.company != "*":
                o.company = self.resolve_ticker(o.company)
            if o.label == "Organization":
                # an organisation that is a company's alias is that company (_merge_orgs_into_companies)
                if aliases is None:
                    aliases = {r["a"]: r["key"] for r in self.run(
                        "MATCH (c:Company) UNWIND c.aliases AS a RETURN a, c.key AS key")}
                o.target = aliases.get(o.target, o.target)
            if o.label == "Company":
                o.target = self.resolve_key(o.target)
                if not self.run("MATCH (c:Company {key: $k}) RETURN c.key AS k", k=o.target):
                    log.warning("identity override %s / %s: no company %s in the graph", o.company, o.name, o.target)
                    continue
            out.append(o)
        return out

    def apply_overrides(self, overrides: list | None = None) -> dict:
        """Apply the identity overrides to the records already in the graph.

        A record an override matches is re-pointed to the override's target
        (REFERS_TO method manual_override); then the relationships those records are
        behind are rebuilt from their records (_rebuild_edges): a relationship whose
        records all moved moves with its history, one that also has other records is
        split. A node left with no relationships and no records is merged into the
        target. Runs on every start (init_schema). Returns what it changed."""
        ovs = self.identity_overrides(overrides)
        out = {"records": 0, "edges_moved": 0, "edges_split": 0, "merged": 0}
        if not ovs:
            return {}
        done: set[str] = set()
        moved: dict[str, dict[str, set]] = {}     # old node -> target node -> record ids
        for o in ovs:
            rows = self.run(
                """
                MATCH (r:Record)-[:REPORTED_FOR]->(c:Company)
                WHERE r.kind IN $kinds AND r.name_key IN $keys AND ($t = '*' OR c.ticker = $t)
                  AND ($src = '' OR r.source STARTS WITH $src)
                MATCH (r)-[x:REFERS_TO]->(e)
                RETURN r.record_id AS rid, r.raw_name AS name, elementId(e) AS old,
                       e.key = $target AND x.method = 'manual_override' AS settled
                """, kinds=list(o.kinds()), keys=sorted(o.keys), t=o.company, src=o.source, target=o.target)
            todo = [r for r in rows if r["rid"] not in done and not r["settled"]]
            done.update(r["rid"] for r in rows)
            if not todo:
                continue
            target = self._override_target(o, todo[0]["name"])
            self.run(
                """
                UNWIND $rids AS rid
                MATCH (r:Record {record_id: rid})-[x:REFERS_TO]->()
                DELETE x
                WITH DISTINCT r
                MATCH (t) WHERE elementId(t) = $t
                CREATE (r)-[:REFERS_TO {method: 'manual_override', confidence: 1.0,
                                        decided_by: $by, decided_at: $at}]->(t)
                """, rids=[r["rid"] for r in todo], t=target, by=o.decided_by or "overrides file",
                at=o.decided_at or _today())
            out["records"] += len(todo)
            for r in todo:
                if r["old"] != target:
                    moved.setdefault(r["old"], {}).setdefault(target, set()).add(r["rid"])
        # relationships without records (loaded before #37) follow the node's own name
        bare_done: set[tuple] = set()
        for o in ovs:
            rels = [rel for rel, kind in _REL_KIND.items() if kind in o.kinds()]
            rows = self.run(
                f"""
                MATCH (n:Entity)-[r:{'|'.join(rels)}]->(c:Company)
                WHERE n <> c AND ($t = '*' OR c.ticker = $t) AND size(coalesce(r.records, [])) = 0
                  AND (n.key IN $keys OR toLower(n.name) = toLower($name)) AND n.key <> $target
                  AND ($src = '' OR any(s IN coalesce(r.sources, [r.source]) WHERE s STARTS WITH $src))
                RETURN DISTINCT elementId(n) AS n, n.name AS name, elementId(c) AS c, type(r) AS rel
                """, rels=rels, t=o.company, keys=sorted(o.keys), name=o.name, target=o.target, src=o.source)
            rows = [r for r in rows if (r["n"], r["c"], r["rel"]) not in bare_done]
            bare_done.update((r["n"], r["c"], r["rel"]) for r in rows)
            if o.source:
                rows = []       # without records, an edge can't be divided by source: left to the records
            for r in rows:
                target = self._override_target(o, r["name"])
                n = self._move_edges(r["n"], target, only=r["c"], rels=(r["rel"],), bare=True)
                out["edges_moved"] += n
                moved.setdefault(r["n"], {}).setdefault(target, set())
        for old, targets in moved.items():
            got = self._rebuild_edges(old, {rid for ids in targets.values() for rid in ids})
            out["edges_moved"] += got["moved"]
            out["edges_split"] += got["split"]
            left = self.run(
                f"""MATCH (o) WHERE elementId(o) = $o
                    RETURN COUNT {{ (o)-[:{EDGE_TYPES}]-() }} + COUNT {{ (o)<-[:REFERS_TO]-() }} AS n""",
                o=old)
            if left and not left[0]["n"] and not self.run(
                    "MATCH (o:Company) WHERE elementId(o) = $o RETURN 1 AS x", o=old):
                self._absorb_node(old, next(iter(targets)))     # a merge: the old key now resolves to the target
                out["merged"] += 1
        return {k: v for k, v in out.items() if v}

    def _override_target(self, o, name: str) -> str:
        """The element id of an override's target node, created if missing (people and organisations)."""
        # a key that comes back (a revoked merge) no longer resolves to the node it was merged into
        self.run("""MATCH (e:Entity) WHERE $k IN coalesce(e.former_keys, []) AND NOT EXISTS {
                        MATCH (x:Entity {key: $k}) }
                    SET e.former_keys = [x IN e.former_keys WHERE x <> $k]""", k=o.target)
        if o.label == "Person":
            return self._person_node(o.target, name, person_name(name))
        if o.label == "Organization":
            return self.run("""MERGE (e:Entity {key: $k}) ON CREATE SET e.name = $name
                               SET e:Organization RETURN elementId(e) AS id""", k=o.target, name=name)[0]["id"]
        return self.run("MATCH (c:Company {key: $k}) RETURN elementId(c) AS id", k=o.target)[0]["id"]

    def _rebuild_edges(self, old: str, rids: set[str]) -> dict:
        """Rebuild node `old`'s relationships after the records `rids` were re-pointed.

        For each relationship (past or current) that lists some of them: the part backed
        by records that now name another node goes to that node (merged into its open
        twin, if it has one), with the history those records span; what is left stays.
        A relationship with nothing left (no records, no source of its own) goes."""
        edges = self.run(
            f"""
            MATCH (o)-[r:{EDGE_TYPES}]->(c) WHERE elementId(o) = $old
              AND any(x IN coalesce(r.records, []) WHERE x IN $rids)
            RETURN elementId(r) AS id, type(r) AS rel, elementId(c) AS c, properties(r) AS p
            """, old=old, rids=sorted(rids))
        out = {"moved": 0, "split": 0}
        for e in edges:
            p = e["p"]
            info = {r["rid"]: r for r in self.run(
                """UNWIND $ids AS rid MATCH (r:Record {record_id: rid})
                   OPTIONAL MATCH (r)-[:REFERS_TO]->(t)
                   RETURN rid, elementId(t) AS t, r.source AS source, r.first_seen AS f, r.last_seen AS l""",
                ids=p.get("records") or [])}
            groups: dict[str, list[str]] = {}
            for rid in p["records"]:
                if rid in rids and rid in info and info[rid]["t"] not in (None, old):
                    groups.setdefault(info[rid]["t"], []).append(rid)
            if not groups:
                continue
            rest = [rid for rid in p["records"] if not any(rid in g for g in groups.values())]
            sources = p.get("sources") or [x for x in [p.get("source")] if x]
            gone = {info[rid]["source"] for g in groups.values() for rid in g}
            for target, ids in groups.items():
                if target == e["c"]:
                    continue        # a holder overridden to the company itself: no self-stake
                self._put_edge(target, e["c"], e["rel"], self._edge_part(p, ids, info, sources))
            keep_sources = [s for s in sources if s not in gone or
                            any(info[rid]["source"] == s for rid in rest if rid in info)]
            if not rest and not keep_sources:
                self._drop_edges([e["id"]])
                out["moved"] += 1
            else:
                left = self._edge_part(p, rest, info, keep_sources) if rest else {
                    "records": [], "sources": keep_sources,
                    "source": p.get("source") if p.get("source") in keep_sources else keep_sources[0]}
                self._set_edge(e["id"], left)
                out["split"] += 1
        return out

    @staticmethod
    def _edge_part(p: dict, ids: list[str], info: dict, sources: list) -> dict:
        """The properties of the part of edge `p` backed by the records `ids`: the edge's
        own history, narrowed to what those records were seen in."""
        q = dict(p)
        q["records"] = list(ids)
        mine = [s for s in sources if any(info[i]["source"] == s for i in ids if i in info)]
        mine += [info[i]["source"] for i in ids if i in info and info[i]["source"] and info[i]["source"] not in mine]
        q["sources"] = mine or sources
        if q.get("source") not in q["sources"]:
            q["source"] = q["sources"][0] if q["sources"] else None
        firsts = [info[i]["f"] for i in ids if i in info and info[i]["f"]]
        lasts = [info[i]["l"] for i in ids if i in info and info[i]["l"]]
        if firsts and p.get("first_seen"):
            q["first_seen"] = max(p["first_seen"], min(firsts))
        if lasts and p.get("last_seen"):
            q["last_seen"] = min(p["last_seen"], max(lasts))
        return q

    def _put_edge(self, holder: str, company: str, rel: str, props: dict) -> None:
        """Add a relationship part to `holder` (element ids): merged into its open edge to
        `company` for the same seat when this part is open too, else a new edge."""
        if props.get("current"):
            twin = self.run(
                f"""MATCH (h)-[x:{rel} {{current: true}}]->(c) WHERE elementId(h) = $h AND elementId(c) = $c
                      AND coalesce(x.post, x.title, '') = coalesce($post, $title, '')
                    RETURN elementId(x) AS id, properties(x) AS p LIMIT 1""",
                h=holder, c=company, post=props.get("post"), title=props.get("title"))
            if twin:
                x = twin[0]["p"]
                self._set_edge(twin[0]["id"], {
                    "records": self._union(x.get("records"), props.get("records")),
                    "sources": self._union(x.get("sources") or [x.get("source")], props.get("sources")),
                    "titles": self._union(x.get("titles"), props.get("titles")) or None,
                    "first_seen": min(v for v in (x.get("first_seen"), props.get("first_seen")) if v)
                    if x.get("first_seen") or props.get("first_seen") else None,
                    "last_seen": max(v for v in (x.get("last_seen"), props.get("last_seen")) if v)
                    if x.get("last_seen") or props.get("last_seen") else None,
                })
                return
        self.run(f"""MATCH (h) WHERE elementId(h) = $h MATCH (c) WHERE elementId(c) = $c
                     CREATE (h)-[y:{rel}]->(c) SET y = $p""",
                 h=holder, c=company, p={k: v for k, v in props.items() if v is not None})

    def records(self, key: str) -> list[dict]:
        """The source records that name the entity `key`: what each row said, where, who
        decided it is this entity and how (REFERS_TO), and the relationships it is behind."""
        key = self.resolve_key(key)
        return self.run(
            f"""
            MATCH (e:Entity {{key: $key}})<-[x:REFERS_TO]-(r:Record)
            OPTIONAL MATCH (r)-[:REPORTED_FOR]->(c:Company)
            RETURN r {{.*}} AS record, c.ticker AS ticker, c.key AS company_key,
                   x {{.method, .confidence, .decided_by, .decided_at}} AS decision,
                   [(e)-[rel:{EDGE_TYPES}]->(t) WHERE r.record_id IN coalesce(rel.records, []) |
                    {{type: type(rel), company: t.ticker, current: rel.current, title: rel.title,
                      percent: rel.percent, first_seen: rel.first_seen, until: rel.until}}] AS edges
            ORDER BY ticker, record.kind, record.last_seen DESC
            """, key=key)

    # ---- one node per legal entity (issue #35) ----------------------------------

    def consolidate_companies(self) -> dict:
        """One :Company node per legal entity, keyed by its register number.

        * `reg_no` (either kind of number, from graphs before #35) is split into
          `register_no` (the registry's 7-digit number) and `state_reg_no` (mse.mn's
          10-digit one);
        * companies with the same `register_no` are merged into one node: the listed
          one if any, else the one with a real ticker, else the oldest. The others'
          tickers and keys are kept in `former_tickers` / `former_keys`, so they still
          resolve (resolve_key, resolve_ticker, canonicalize);
        * an :Organization whose register number (its own `register_no`, or the
          `owner_reg` of its beneficial-owner edges) is a company's is merged into it;
        * every company's key is `company:<register_no>`, else `company:<TICKER>`, and
          listed companies carry the `Listed` label;
        * organisations get an `org_type` from their name where no source gave one.

        Runs on every start and after every load. Returns what it changed.
        """
        out = {"split_numbers": self._split_reg_numbers(),
               "companies_merged": 0, "organizations_merged": 0}
        groups = self.run(
            """
            MATCH (c:Company) WHERE c.register_no =~ $re
            WITH c.register_no AS reg, collect(c) AS cs WHERE size(cs) > 1
            RETURN reg, [c IN cs | {id: elementId(c), ticker: c.ticker, listed: coalesce(c.listed, true),
                                    first: coalesce(c.first_seen, '')}] AS cs
            """, re=REGISTER_NO_RE)
        for grp in groups:
            keep, *rest = sorted(grp["cs"], key=lambda c: (not c["listed"], c["ticker"].startswith("~"),
                                                           c["first"], c["ticker"]))
            for c in rest:
                self._merge_node(c["id"], keep["id"])
                out["companies_merged"] += 1
        pairs = self.run(
            """
            MATCH (o:Organization) WHERE NOT o:Company
            OPTIONAL MATCH (o)-[b:BENEFICIAL_OWNER]->()
            WITH o, collect(DISTINCT b.owner_reg) + collect(DISTINCT o.register_no) AS regs
            UNWIND regs AS reg
            MATCH (c:Company {register_no: reg})     // a company's register_no is always a register number
            RETURN DISTINCT elementId(o) AS org, elementId(c) AS company, reg ORDER BY reg
            """)
        merged = set()
        for p in pairs:
            if p["org"] not in merged:      # an organisation named with two numbers goes to the first
                self._merge_node(p["org"], p["company"])
                merged.add(p["org"])
        out["organizations_merged"] = len(merged)
        out["rekeyed"] = self.run(
            """
            MATCH (c:Company)
            WITH c, 'company:' + CASE WHEN c.register_no =~ $re THEN c.register_no ELSE c.ticker END AS want
            WHERE c.key IS NULL OR c.key <> want
            SET c.former_keys = CASE WHEN c.key IS NULL OR c.key IN coalesce(c.former_keys, [])
                                     THEN c.former_keys ELSE coalesce(c.former_keys, []) + c.key END,
                c.key = want
            RETURN count(c) AS n
            """, re=REGISTER_NO_RE)[0]["n"]
        self.run("MATCH (c:Company) WHERE coalesce(c.listed, true) AND NOT c:Listed SET c:Listed")
        self.run("MATCH (c:Company:Listed) WHERE c.listed = false REMOVE c:Listed")
        out["org_types"] = self._type_organizations()
        return out

    def _split_reg_numbers(self) -> int:
        """Move the `reg_no` of graphs before #35 into `register_no` / `state_reg_no`."""
        rows = self.run("""MATCH (c:Company) WHERE c.reg_no IS NOT NULL
                           RETURN elementId(c) AS id, c.reg_no AS reg_no, c.register_no AS register_no,
                                  c.state_reg_no AS state_reg_no""")
        updates = []
        for r in rows:
            register, state = split_reg_numbers(r["register_no"], r["state_reg_no"], r["reg_no"])
            if register and r["register_no"] and register != r["register_no"]:
                register = r["register_no"]
            updates.append({"id": r["id"], "register": register or None, "state": state or None})
        self.run("""UNWIND $rows AS r MATCH (c) WHERE elementId(c) = r.id
                    SET c.register_no = r.register, c.state_reg_no = r.state REMOVE c.reg_no""", rows=updates)
        return len(updates)

    def _type_organizations(self) -> int:
        """`org_type` from the name for organisations no source typed, when the rules changed."""
        rows = self.run("""MATCH (o:Organization) WHERE NOT o:Company AND coalesce(o.org_type_by, '') <> 'source'
                             AND coalesce(o.org_type_v, '') <> $v
                           RETURN elementId(o) AS id, o.name AS name""", v=ORG_TYPES_VERSION)
        self.run("""UNWIND $rows AS r MATCH (o) WHERE elementId(o) = r.id
                    SET o.org_type = r.t, o.org_type_by = 'name', o.org_type_v = $v""",
                 rows=[{"id": r["id"], "t": org_type_from_name(r["name"])} for r in rows], v=ORG_TYPES_VERSION)
        return len(rows)

    def _merge_node(self, old: str, new: str) -> None:
        """Merge node `old` into node `new` (element ids): edges, names and history.

        Every period (past or current) of every edge moves over, except a current edge
        `new` already has to or from the same node (for positions: the same seat); that
        one only adds its sources and spellings to the edge `new` keeps. Edges between
        the two nodes are dropped. `new` keeps its own properties and takes the ones it
        lacks; the old ticker, key and names are remembered (`former_tickers`,
        `former_keys`, `merged_aliases`)."""
        self._move_edges(old, new)
        self._absorb_node(old, new)

    def _move_edges(self, old: str, new: str, only: str | None = None, method: str | None = None,
                    rels: tuple = ("OWNS", "HOLDS_POSITION", "BENEFICIAL_OWNER"), bare: bool = False) -> int:
        """Move node `old`'s edges to node `new` (element ids), as _merge_node describes.
        With `only` (a node's element id), just the edges between `old` and that node.
        The source records behind them follow (REFERS_TO, issue #37); `method` is then
        how they are decided, e.g. "initial_match" (default: unchanged). With `bare`, only
        `old`'s outgoing edges that list no records move, and no records do (apply_overrides).
        Returns the number of edges moved."""
        n = 0
        for rel in rels:
            patterns = (("(o)-[r:{rel}]->(t)", "(n)-[x:{rel} {{current: true}}]->(t)"),
                        ("(t)-[r:{rel}]->(o)", "(t)-[x:{rel} {{current: true}}]->(n)"))
            for pattern, twin in patterns[:1] if bare else patterns:
                create = "(n)-[y:{rel}]->(t)" if pattern.startswith("(o)") else "(t)-[y:{rel}]->(n)"
                got = self.run(
                    f"""
                    MATCH (o) WHERE elementId(o) = $old
                    MATCH (n) WHERE elementId(n) = $new
                    MATCH {pattern.format(rel=rel)} WHERE t <> n AND t <> o AND ($only IS NULL OR elementId(t) = $only)
                      AND (NOT $bare OR size(coalesce(r.records, [])) = 0)
                    OPTIONAL MATCH {twin.format(rel=rel)}
                    WHERE coalesce(r.current, false) AND coalesce(x.post, x.title, '') = coalesce(r.post, r.title, '')
                    WITH n, r, t, head(collect(x)) AS x
                    CALL (n, r, t, x) {{
                        WITH n, r, t WHERE x IS NULL
                        CREATE {create.format(rel=rel)}
                        SET y = properties(r)
                    }}
                    CALL (r, x) {{
                        WITH r, x WHERE x IS NOT NULL
                        WITH r, x, coalesce(x.sources, [v IN [x.source] WHERE v IS NOT NULL]) AS have
                        SET x.sources = have + [v IN coalesce(r.sources, [r.source]) WHERE v IS NOT NULL
                                                AND NOT v IN have],
                            x.titles = CASE WHEN r.titles IS NULL THEN x.titles ELSE
                                coalesce(x.titles, []) + [v IN r.titles WHERE NOT v IN coalesce(x.titles, [])] END,
                            x.records = CASE WHEN r.records IS NULL THEN x.records ELSE
                                coalesce(x.records, []) + [v IN r.records WHERE NOT v IN coalesce(x.records, [])] END,
                            x.first_seen = CASE WHEN r.first_seen < x.first_seen THEN r.first_seen
                                                ELSE x.first_seen END
                    }}
                    WITH r
                    DELETE r
                    RETURN count(*) AS n
                    """, old=old, new=new, only=only, bare=bare)
                n += got[0]["n"] if got else 0
        if not bare:
            self._move_refs(old, new, only, method)
        return n

    def _move_refs(self, old: str, new: str, only: str | None = None, method: str | None = None) -> None:
        """Re-point the records that refer to node `old` to node `new` (element ids); with
        `only`, just the records reported for that company. Pinned decisions (manual_override)
        move only in a whole-node merge (`only` None), when the node itself goes."""
        self.run(
            """
            MATCH (o) WHERE elementId(o) = $old
            MATCH (n) WHERE elementId(n) = $new
            MATCH (r:Record)-[x:REFERS_TO]->(o)
            WHERE $only IS NULL OR (x.method <> 'manual_override'
                                    AND EXISTS { MATCH (r)-[:REPORTED_FOR]->(t) WHERE elementId(t) = $only })
            CREATE (r)-[y:REFERS_TO]->(n)
            SET y = properties(x)
            SET y.method = coalesce($method, x.method),
                y.confidence = CASE WHEN $method IS NULL THEN x.confidence ELSE $conf END,
                y.decided_by = CASE WHEN $method IS NULL THEN x.decided_by ELSE 'rule' END,
                y.decided_at = CASE WHEN $method IS NULL THEN x.decided_at ELSE $today END
            DELETE x
            """, old=old, new=new, only=only, method=method,
            conf=rec.CONFIDENCE.get(method or "", 1.0), today=_today())

    def _absorb_node(self, old: str, new: str) -> None:
        """The node part of _merge_node: `new` takes `old`'s names and properties, `old` goes."""
        po, pn =(self.run("MATCH (x) WHERE elementId(x) = $id RETURN properties(x) AS p", id=i)[0]["p"]
                  for i in (old, new))
        fill = {k: v for k, v in po.items() if k not in pn and k not in _OWN_PROPS}
        company = "ticker" in pn
        merged = {"former_keys": _add(pn.get("former_keys"), po.get("former_keys"), [po.get("key")],
                                      skip=pn.get("key"))}
        if po.get("name_variants"):
            merged["name_variants"] = _add(pn.get("name_variants"), po.get("name_variants"))
        if company:
            names = [a for a in (po.get("aliases") or []) + (po.get("merged_aliases") or []) + [po.get("key")]
                     if a and a.startswith("org:")]
            merged.update({
                "former_tickers": _add(pn.get("former_tickers"), po.get("former_tickers"), [po.get("ticker")],
                                       skip=pn.get("ticker")),
                "merged_aliases": _add(pn.get("merged_aliases"), names),
                "aliases": _add(pn.get("aliases"), names),
                "listed": bool(pn.get("listed", True)) or bool(po.get("listed", False)),
            })
        if po.get("first_seen") and pn.get("first_seen") and po["first_seen"] < pn["first_seen"]:
            merged["first_seen"] = po["first_seen"]
            merged["first_run"] = po.get("first_run")
        if company:
            merged["former_names"] = _add(pn.get("former_names"), po.get("former_names")) or None
            merged["former_aliases"] = _add(pn.get("former_aliases"), po.get("former_aliases")) or None
            # dated facts (issue #38) move over; an open one that `new` has from the same source
            # ends where it was last seen (history of the merged-away node, not a change)
            self.run("""MATCH (o) WHERE elementId(o) = $old MATCH (n) WHERE elementId(n) = $new
                        MATCH (o)-[x:HAS_FACT]->(f:Fact)
                        OPTIONAL MATCH (n)-[:HAS_FACT]->(k:Fact {name: f.name, source: f.source, current: true})
                        WITH n, x, f, count(k) > 0 AS clash
                        FOREACH (_ IN CASE WHEN clash AND f.current THEN [1] ELSE [] END |
                            SET f.current = false, f.until = f.last_seen, f.merged = true)
                        CREATE (n)-[:HAS_FACT]->(f)
                        DELETE x""", old=old, new=new)
        self._move_refs(old, new)
        # records reported for a company that is merged away are reported for the one kept
        self.run("""MATCH (o) WHERE elementId(o) = $old MATCH (n) WHERE elementId(n) = $new
                    MATCH (r:Record)-[x:REPORTED_FOR]->(o)
                    MERGE (r)-[:REPORTED_FOR]->(n)
                    DELETE x""", old=old, new=new)
        self.run("""MATCH (o) WHERE elementId(o) = $old MATCH (n) WHERE elementId(n) = $new
                    SET n += $props DETACH DELETE o""", old=old, new=new, props={**fill, **merged})

    def canonicalize(self, ds: Dataset) -> dict[str, str]:
        """Point a dataset's tickers at the nodes the graph keeps for them (in place).

        A ticker that was merged away (a `~<register no>` of a company that turned out
        to be listed), or a company row whose register number another company already
        has, is rewritten to that company's ticker, in every row and snapshot. Two rows
        of one dataset with the same register number become one company. Such company
        rows are marked `merge_only` (see load()): they add names and fill gaps, but
        never rename the company or change whether it is listed.
        Returns {old ticker: new ticker} for the tickers it rewrote."""
        rec.stamp(ds)        # record ids come from the rows as the source gave them (issue #37)
        rows = self.run("MATCH (c:Company) RETURN c.ticker AS t, c.register_no AS reg, "
                        "coalesce(c.former_tickers, []) AS former, coalesce(c.listed, true) AS listed")
        have = {r["t"] for r in rows}
        former = {f: r["t"] for r in rows for f in r["former"]}
        by_reg = {r["reg"]: r["t"] for r in rows if r["reg"]}
        tmap: dict[str, str] = {}

        def canon(ticker: str, reg: str = "") -> str:
            t = ticker.upper()
            if t in tmap:
                return tmap[t]
            if t in have:
                return t
            if t in former:
                return former[t]
            if reg and reg in by_reg:
                return by_reg[reg]
            if t.startswith("~") and t[1:] in by_reg:
                return by_reg[t[1:]]
            return t

        # listed rows first, so a listed company keeps its ticker within one dataset too
        for c in sorted(ds.companies, key=lambda c: not c.listed):
            reg, _ = split_reg_numbers(c.register_no, c.reg_no)
            t = c.ticker.upper()
            new = canon(t, reg)
            if new != t:
                tmap[t] = new
            elif reg and reg not in by_reg:
                by_reg[reg] = t
        for t in {x.company_ticker.upper() for x in ds.shareholdings + ds.positions + ds.beneficial_owners} | \
                 {x.holder_ticker.upper() for x in ds.shareholdings if x.holder_ticker} | \
                 {f.ticker.upper() for f in ds.facts} | \
                 {t.upper() for scopes in ds.snapshots.values() for t in scopes}:
            new = canon(t)
            if new != t:
                tmap[t] = new
        if not tmap:
            return {}
        for c in ds.companies:
            if c.ticker.upper() in tmap:
                c.merged_from, c.ticker = c.ticker.upper(), tmap[c.ticker.upper()]
                c.merge_only = True
        for x in ds.shareholdings + ds.positions + ds.beneficial_owners:
            x.company_ticker = tmap.get(x.company_ticker.upper(), x.company_ticker)
        for x in ds.shareholdings:
            if x.holder_ticker:
                x.holder_ticker = tmap.get(x.holder_ticker.upper(), x.holder_ticker)
        for f in ds.facts:
            f.ticker = tmap.get(f.ticker.upper(), f.ticker)
        for kind, scopes in ds.snapshots.items():
            ds.snapshots[kind] = {tmap.get(t.upper(), t): p for t, p in scopes.items()}
        return tmap

    def company_keys(self) -> dict[str, str]:
        """ticker (and former ticker) -> the company's node key."""
        out = {}
        for r in self.run("MATCH (c:Company) RETURN c.ticker AS t, c.key AS key, "
                          "coalesce(c.former_tickers, []) AS former"):
            for t in r["former"]:
                out.setdefault(t, r["key"])
            out[r["t"]] = r["key"]
        return out

    def resolve_key(self, key: str) -> str:
        """The current key of the node `key` names: old company keys (`company:APU`,
        `company:~2702673`), a ticker or a register / state registration number after
        `company:` all lead to the company. Unknown keys come back unchanged."""
        key = (key or "").strip()
        if not key.startswith("company:"):
            # a person merged into another (issue #36) leaves its key in `former_keys`
            rows = self.run("""MATCH (e:Entity) WHERE e.key = $key OR $key IN coalesce(e.former_keys, [])
                               RETURN e.key AS key ORDER BY (e.key = $key) DESC, e.key LIMIT 1""", key=key)
            return rows[0]["key"] if rows else key
        t = key[len("company:"):].strip()
        rows = self.run(
            """
            MATCH (c:Company)
            WHERE c.key = $key OR $key IN coalesce(c.former_keys, []) OR c.ticker IN [$t, toUpper($t)]
               OR toUpper($t) IN coalesce(c.former_tickers, []) OR c.register_no = $t OR c.state_reg_no = $t
            RETURN c.key AS key ORDER BY (c.key = $key) DESC, (c.ticker = toUpper($t)) DESC LIMIT 1
            """, key=key, t=t)
        return rows[0]["key"] if rows else key

    def resolve_ticker(self, ticker: str) -> str:
        """The ticker the graph keeps for `ticker` (it may be a former ticker or a register number)."""
        t = (ticker or "").strip().upper()
        rows = self.run(
            """
            MATCH (c:Company)
            WHERE c.ticker = $t OR $t IN coalesce(c.former_tickers, []) OR c.register_no = $t OR c.state_reg_no = $t
            RETURN c.ticker AS t ORDER BY (c.ticker = $t) DESC LIMIT 1
            """, t=t)
        return rows[0]["t"] if rows else t

    def wipe(self, keep_decisions: bool = False):
        """Delete the graph. `keep_decisions` keeps the review decisions (review.py), which
        `load --wipe`, `rebuild-history` and the switch from demo to real data do."""
        self.run("MATCH (n) WHERE NOT ($keep AND n:Decision) "
                 "CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 5000 ROWS", keep=keep_decisions)

    def has_demo_data(self) -> bool:
        return self.run("RETURN EXISTS { ()-[r]->() WHERE r.source = $s } AS demo", s=DEMO_SOURCE)[0]["demo"]

    def has_real_data(self) -> bool:
        return self.run("RETURN EXISTS { ()-[r:OWNS|HOLDS_POSITION]->() WHERE r.source <> $s } AS real",
                        s=DEMO_SOURCE)[0]["real"]

    def last_scrape(self) -> str | None:
        """When the pipeline last loaded a full scrape (ISO timestamp, UTC)."""
        rows = self.run("MATCH (m:Meta {key: 'pipeline'}) RETURN toString(m.last_scrape) AS t")
        return rows[0]["t"] if rows else None

    def mark_scrape(self) -> None:
        self.run("MERGE (m:Meta {key: 'pipeline'}) SET m.last_scrape = datetime()")

    def hours_since_last_scrape(self) -> float | None:
        rows = self.run("""MATCH (m:Meta {key: 'pipeline'})
                           RETURN duration.inSeconds(m.last_scrape, datetime()).seconds / 3600.0 AS h""")
        return rows[0]["h"] if rows else None

    def claim(self, name: str, owner: str, minutes: float) -> dict:
        """Take the lease `name` for `minutes`, unless someone else holds an unexpired one.

        Returns {"ok", "owner", "until"}; on failure `owner` / `until` are the holder's.
        The hosted pipeline and the daily job both read opendatalab.mn, which must never
        be crawled in parallel: each takes the "registry" lease first (see pipeline.py, cli.py).
        """
        return self.run(
            """
            MERGE (m:Meta {key: $k})
            WITH m, m.until IS NULL OR m.until < datetime() OR m.owner = $owner AS free
            SET m.owner = CASE WHEN free THEN $owner ELSE m.owner END,
                m.until = CASE WHEN free THEN datetime() + duration({seconds: toInteger($secs)}) ELSE m.until END
            RETURN free AS ok, m.owner AS owner, toString(m.until) AS until
            """, k="lease:" + name, owner=owner, secs=minutes * 60)[0]

    def release(self, name: str, owner: str) -> None:
        self.run("MATCH (m:Meta {key: $k}) WHERE m.owner = $owner SET m.until = null",
                 k="lease:" + name, owner=owner)

    def manual_digest(self, name: str) -> str | None:
        """Fingerprint of the hand-curated dataset `name` as last loaded (see pipeline.load_manual)."""
        rows = self.run("MATCH (m:Meta {key: $k}) RETURN m.digest AS d", k="manual:" + name)
        return rows[0]["d"] if rows else None

    def mark_manual(self, name: str, digest: str) -> None:
        self.run("MERGE (m:Meta {key: $k}) SET m.digest = $d, m.loaded_at = datetime()",
                 k="manual:" + name, d=digest)

    def mark_baseline(self, run: str) -> int:
        """Mark the edges a load opened as a baseline, so they are not reported as changes."""
        return self.run(
            """MATCH ()-[r:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER {opened_run: $run}]->()
               SET r.baseline = true RETURN count(r) AS n""", run=run)[0]["n"]

    def missing_tickers(self, tickers) -> set[str]:
        """Those of `tickers` that have no :Company node."""
        rows = self.run("UNWIND $t AS t OPTIONAL MATCH (c:Company {ticker: t}) "
                        "WITH t, c WHERE c IS NULL RETURN t", t=sorted(tickers))
        return {r["t"] for r in rows}

    def drop_demo_data(self) -> bool:
        """Remove the fictional demo graph before real data is loaded on top of it."""
        if not self.has_demo_data():
            return False
        self.wipe(keep_decisions=True)
        return True

    # ---- loading ------------------------------------------------------------

    def load(self, ds: Dataset, observed_at: str | None = None, run: str | None = None,
             overrides: list | None = None) -> dict:
        """Merge a dataset into the graph, keeping history (see module docstring).

        Load datasets in chronological order: `observed_at` (default: the
        dataset's own, else today) is the date stamped on new, refreshed and
        closed edges. `run` (default: a fresh UTC timestamp) is stamped on the
        edges this load opens or closes; history.run_changes() reads them back.
        Every row becomes a source record (records.py). `overrides` (records.Override
        rows) replace the identity overrides file.
        """
        self.init_schema(overrides)
        when = observed_at or ds.observed_at or _today()
        run = run or new_run_id()
        self.canonicalize(ds)
        companies = []
        for c in ds.companies:
            others = [n.strip() for n in (c.other_names or "").split("|") if n.strip()]
            aliases = sorted({entity_key(n) for n in (c.name, c.name_en) if n} |
                             {entity_key(n) for n in _org_variants(c.name)} |
                             # a bare former name ("Шунхлайгрупп") has no legal-form marker
                             {entity_key(n, k) for n in others for k in ("", "organization")})
            register, state = split_reg_numbers(c.register_no, c.state_reg_no, c.reg_no)
            companies.append({**{k: v for k, v in c.__dict__.items() if k not in ("reg_no", "sector")},
                              "sector_mse": c.sector,
                              "ticker": c.ticker.upper(), "register_no": register, "state_reg_no": state,
                              "key": company_key(c.ticker, register), "aliases": aliases,
                              "merge_only": bool(getattr(c, "merge_only", False)),
                              "merged_from": getattr(c, "merged_from", None)})
        # a company's own rows first: a merged-in row (merge_only) only adds to them
        companies.sort(key=lambda r: r["merge_only"])
        fill = lambda f: (f"c.{f} = CASE WHEN r.{f} <> '' AND (NOT r.merge_only OR coalesce(c.{f}, '') = '') "  # noqa: E731
                          f"THEN r.{f} ELSE c.{f} END")
        self.run(
            f"""
            UNWIND $rows AS r
            MERGE (c:Entity:Company {{ticker: r.ticker}})
            ON CREATE SET c.first_seen = $when, c.first_run = $run, c.key = r.key
            SET c.name = CASE WHEN r.merge_only THEN coalesce(c.name, r.name) ELSE r.name END,
                c.listed = CASE WHEN r.merge_only THEN coalesce(c.listed, r.listed) ELSE r.listed END,
                c.merged_aliases = CASE WHEN r.merge_only THEN coalesce(c.merged_aliases, []) +
                    [a IN r.aliases WHERE NOT a IN coalesce(c.merged_aliases, [])] ELSE c.merged_aliases END,
                c.former_tickers = CASE WHEN r.merged_from IS NULL OR r.merged_from = c.ticker
                                          OR r.merged_from IN coalesce(c.former_tickers, []) THEN c.former_tickers
                                        ELSE coalesce(c.former_tickers, []) + r.merged_from END
            // aliases: the company's own names, plus those of the rows and nodes merged into it
            SET c.aliases = CASE WHEN r.merge_only THEN coalesce(c.aliases, []) ELSE r.aliases END
            SET c.aliases = c.aliases + [a IN coalesce(c.merged_aliases, []) + coalesce(c.former_aliases, [])
                                         WHERE NOT a IN c.aliases],
                {fill("name_en")}, {fill("sector_mse")}, {fill("address")}, {fill("website")},
                {fill("mse_id")}, {fill("url")}, {fill("state_reg_no")},
                c.register_no = CASE WHEN coalesce(c.register_no, '') = '' AND r.register_no <> ''
                                     THEN r.register_no ELSE c.register_no END
            """,
            rows=companies, when=when, run=run,
        )
        self._write_records([rec.record_row("company", c, c.ticker, "", "exact_key") for c in ds.companies], when)
        # dated facts (issue #38), before the aliases are read: a former name links too
        fact_changes = self._record_facts(ds, companies, when, run)
        # Fold any :Organization created earlier (before this company was loaded)
        # into the matching :Company node.
        self._merge_orgs_into_companies()

        # who the person rows are (issue #36): first hand the full names this dataset
        # brings the edges their initial forms have at the same company
        alias_map = {row["a"]: row["key"] for row in self.run(
            "MATCH (c:Company) UNWIND c.aliases AS a RETURN a, c.key AS key")}
        overrides = self.identity_overrides(overrides)
        pending = self._dataset_people(ds, alias_map, overrides)
        adopted = self._adopt_initials(pending) if pending else 0
        people = self.people_resolver(pending, overrides)

        changes = {}
        changes.update(self._load_shareholdings(ds, when, run, people))
        changes.update(self._load_positions(ds, when, run, people))
        changes.update(self._load_beneficial_owners(ds, when, run, people))
        self._note_names(people.noted)
        if adopted:
            changes["people_linked"] = adopted
        self._load_facts(ds)
        merged = self.consolidate_companies()   # e.g. a registry page gave a company its register number
        if merged["companies_merged"] or merged["organizations_merged"]:
            changes.update({k: merged[k] for k in ("companies_merged", "organizations_merged")})
        self._refresh_former_names()
        self._derive_sectors()
        changes.update({k: v for k, v in fact_changes.items() if v})
        return {**self.stats(), "observed_at": when, "run": run, "changes": changes}

    def _new_sources(self, rel: str, snaps: dict[str, str]) -> set[str]:
        """Tickers whose snapshot source has never reported a `rel` edge for them."""
        scopes = [{"ticker": t, "prefix": p, "under": p.rstrip("/") + "/"} for t, p in snaps.items()]
        rows = self.run(
            f"""
            UNWIND $scopes AS s
            MATCH (c:Company {{ticker: s.ticker}})
            WHERE NOT EXISTS {{ MATCH ()-[r:{rel}]->(c) WHERE r.source = s.prefix OR r.source STARTS WITH s.under }}
            RETURN c.ticker AS t
            """,
            scopes=scopes,
        )
        return {r["t"] for r in rows}

    def _load_shareholdings(self, ds: Dataset, when: str, run: str, people: PeopleResolver) -> dict:
        alias_map = {row["a"]: row["key"] for row in self.run(
            "MATCH (c:Company) UNWIND c.aliases AS a RETURN a, c.key AS key")}
        tkeys = self.company_keys()
        snaps = {t.upper(): p for t, p in ds.snapshots.get("shareholdings", {}).items()}
        fresh = self._new_sources("OWNS", snaps)
        # "holdings" snapshots are keyed by the holder: a company's own list of its
        # stakes in other companies (subsidiaries, associates)
        hsnaps = {t.upper(): p for t, p in ds.snapshots.get("holdings", {}).items()}
        fresh_holders = self._new_holders(hsnaps)

        by_pair: dict[tuple, dict] = {}
        held: dict[str, list[str]] = {t: [] for t in hsnaps}   # holder -> targets it reported
        recs = []
        for h in ds.shareholdings:
            name = clean(h.holder_name)
            if not name or is_aggregate(name):
                continue  # "Бусад" / "Others" / totals are not real holders
            holder = h.holder_ticker.upper()
            key = entity_key(name, h.holder_type)
            ticker = h.company_ticker.upper()
            ov = None if holder else people.match_override(name, ticker, h.source, "shareholder", h.holder_type)
            if ov is not None:
                key, label, method = ov.target, ov.label, "manual_override"
            elif holder:
                key, label, method = tkeys.get(holder) or company_key(holder), "Company", "holder_ticker"
            elif key in alias_map or entity_key(name) in alias_map:
                key, label, method = alias_map.get(key) or alias_map[entity_key(name)], "Company", "alias"
            else:
                htype = h.holder_type or ("organization" if is_organization(name) else "person")
                label = "Organization" if htype.startswith("org") else "Person"
                method = "exact_key"
                if label == "Person":
                    key, method = people.resolve(name, h.company_ticker, h.source, "shareholder")
            recs.append(rec.record_row("shareholder", h, ticker, key, method, ov))
            prefix = snaps.get(ticker)
            in_holdings = holder in hsnaps and _under(h.source, hsnaps[holder])
            if in_holdings:
                held[holder].append(ticker)
            row = {
                "ticker": ticker, "key": key, "name": name, "label": label, "org_type": _org_type(h.org_type),
                "percent": h.percent, "shares": h.shares, "as_of": _opt(parse_date(h.as_of)),
                "source": h.source,
                # rows from a complete snapshot of the company define its current owners
                "in_snapshot": prefix is not None and _under(h.source, prefix),
                "in_holdings": in_holdings,
            }
            # what a source reports the first time it covers a company is not a change
            row["baseline"] = ((row["in_snapshot"] and ticker in fresh)
                               or (in_holdings and holder in fresh_holders))
            # ... and, unless the source only supplements others, may take edges over.
            # A holder's complete list of its own stakes counts too, below the target's
            # own shareholder table, which wins when both list the same stake.
            row["rank"] = 2 if row["in_snapshot"] else 1 if in_holdings else 0
            row["authoritative"] = row["rank"] > 0 and not ds.supplementary
            prev = by_pair.get((ticker, key))
            row["records"] = self._union(prev["records"] if prev else [], [h._rid])
            if prev is None or row["rank"] >= prev["rank"]:
                by_pair[(ticker, key)] = row   # one row per holder and company
            else:
                prev["records"] = row["records"]
        holdings = list(by_pair.values())

        for label in ("Person", "Organization"):
            self.run(
                f"""
                UNWIND $rows AS r
                MERGE (e:Entity {{key: r.key}})
                ON CREATE SET e.name = r.name
                SET e:{label}
                """,
                rows=[h for h in holdings if h["label"] == label],
            )
        self._set_org_types(holdings)
        res = self.run(
            """
            UNWIND $rows AS r
            MATCH (c:Company {ticker: r.ticker})
            MATCH (e:Entity {key: r.key})
            WHERE e <> c
            OPTIONAL MATCH (e)-[o:OWNS {current: true}]->(c)
            WITH r, c, e, o,
                 o IS NOT NULL AND (
                   (r.percent IS NOT NULL AND o.percent IS NOT NULL AND abs(r.percent - o.percent) > $tol) OR
                   (r.shares IS NOT NULL AND o.shares IS NOT NULL AND r.shares <> o.shares)) AS changed
            // A different value replaces the open stake if it comes from the company's
            // snapshot, from the same source, or is dated later; never if it is dated
            // earlier. Otherwise two sources that disagree would close and reopen the
            // stake on every load (loads are layered: mse.mn, Open Data Lab, CSVs).
            WITH r, c, e, o, changed,
                 coalesce(r.as_of =~ $iso AND o.as_of =~ $iso AND r.as_of < o.as_of, false) AS older,
                 coalesce(r.as_of =~ $iso AND o.as_of =~ $iso AND r.as_of > o.as_of, false) AS newer
            WITH r, c, e, o, changed,
                 changed AND NOT older AND (r.authoritative OR o.source = r.source OR newer) AS supersede
            CALL (r, c, e, o, supersede) {
                WITH r, c, e, o, supersede WHERE o IS NULL OR supersede
                CREATE (e)-[n:OWNS {current: true}]->(c)
                SET n.percent = r.percent, n.shares = r.shares, n.as_of = r.as_of, n.source = r.source,
                    n.sources = [r.source], n.first_seen = $when, n.last_seen = $when, n.opened_run = $run,
                    n.baseline = CASE WHEN r.baseline THEN true END, n.records = r.records
                WITH o WHERE o IS NOT NULL
                SET o.current = false, o.until = $when, o.closed_at = $when, o.closed_run = $run
            }
            CALL (r, o, changed) {
                WITH r, o WHERE o IS NOT NULL AND NOT changed
                SET o.last_seen = CASE WHEN coalesce(o.last_seen, '') < $when THEN $when ELSE o.last_seen END,
                    o.percent = coalesce(o.percent, r.percent),
                    o.shares = coalesce(o.shares, r.shares),
                    o.records = coalesce(o.records, []) + [x IN r.records WHERE NOT x IN coalesce(o.records, [])]
                WITH r, o, coalesce(o.sources, [x IN [o.source] WHERE x IS NOT NULL]) AS have
                SET o.sources = CASE WHEN r.source IN have THEN have ELSE have + r.source END
                WITH r, o WHERE NOT coalesce(r.as_of =~ $iso AND o.as_of =~ $iso AND r.as_of < o.as_of, false)
                // another source (e.g. the registry) only adds itself to `sources`: if it
                // took over `source`, the company's own snapshot could no longer close the edge
                WITH r, o, r.authoritative OR o.source = r.source OR o.source IS NULL AS owner
                SET o.source = CASE WHEN owner THEN r.source ELSE o.source END,
                    o.as_of = CASE WHEN owner OR (o.as_of IS NULL AND r.percent IS NOT NULL)
                                   THEN coalesce(r.as_of, o.as_of) ELSE o.as_of END
            }
            CALL (r, o, changed, supersede) {
                // a value that conflicts but doesn't win is still evidence for the open stake
                WITH r, o WHERE changed AND NOT supersede
                SET o.records = coalesce(o.records, []) + [x IN r.records WHERE NOT x IN coalesce(o.records, [])]
            }
            RETURN count(CASE WHEN o IS NULL THEN 1 END) AS opened,
                   count(CASE WHEN supersede THEN 1 END) AS changed,
                   count(CASE WHEN changed AND NOT supersede THEN 1 END) AS conflicting
            """,
            rows=holdings, when=when, run=run, tol=PERCENT_TOLERANCE, iso=ISO_DATE,
        )[0]

        # Holders missing from a company's complete snapshot have sold out. A
        # snapshot with no holders at all is treated as "no information" (a page
        # layout change, not every holder selling on the same day).
        seen: dict[str, list[str]] = {}
        for h in holdings:
            if h["in_snapshot"]:
                seen.setdefault(h["ticker"], []).append(h["key"])
        scopes = [{"ticker": t, "prefix": snaps[t], "under": snaps[t].rstrip("/") + "/", "seen": keys}
                  for t, keys in seen.items()]
        closed = self.run(
            """
            UNWIND $scopes AS s
            MATCH (e:Entity)-[o:OWNS {current: true}]->(c:Company {ticker: s.ticker})
            WHERE (o.source = s.prefix OR o.source STARTS WITH s.under) AND NOT e.key IN s.seen
            SET o.current = false, o.until = $when, o.closed_at = $when, o.closed_run = $run
            RETURN count(o) AS n
            """,
            scopes=scopes, when=when, run=run,
        )[0]["n"]
        # Stakes missing from a company's complete list of its own stakes were sold.
        # As above, an empty list closes nothing.
        scopes = [{"ticker": t, "prefix": hsnaps[t], "under": hsnaps[t].rstrip("/") + "/", "seen": seen_}
                  for t, seen_ in held.items() if seen_]
        closed += self.run(
            """
            UNWIND $scopes AS s
            MATCH (c:Company {ticker: s.ticker})-[o:OWNS {current: true}]->(t:Company)
            WHERE (o.source = s.prefix OR o.source STARTS WITH s.under) AND NOT t.ticker IN s.seen
            SET o.current = false, o.until = $when, o.closed_at = $when, o.closed_run = $run
            RETURN count(o) AS n
            """,
            scopes=scopes, when=when, run=run,
        )[0]["n"]
        # from now on, new stakes of these companies are changes, not their baseline
        self.run("UNWIND $t AS t MATCH (c:Company {ticker: t}) SET c.holdings_first_seen = $when",
                 t=sorted(fresh_holders), when=when)
        self._write_records(recs, when)
        return {"ownerships_opened": res["opened"], "ownerships_changed": res["changed"],
                "ownerships_closed": closed, "ownerships_conflicting": res["conflicting"]}

    def _set_org_types(self, rows: list[dict]) -> None:
        """Record the `org_type` a source gave for an organisation; it wins over the name rule."""
        typed = [{"key": r["key"], "t": r["org_type"]} for r in rows if r["label"] == "Organization" and r["org_type"]]
        self.run("""UNWIND $rows AS r MATCH (o:Organization {key: r.key}) WHERE NOT o:Company
                    SET o.org_type = r.t, o.org_type_by = 'source'""", rows=typed)

    def _new_holders(self, hsnaps: dict[str, str]) -> set[str]:
        """Companies whose own list of stakes in other companies is read for the first time."""
        if not hsnaps:
            return set()
        rows = self.run("UNWIND $t AS t MATCH (c:Company {ticker: t}) WHERE c.holdings_first_seen IS NULL "
                        "RETURN c.ticker AS t", t=list(hsnaps))
        return {r["t"] for r in rows}

    def _load_positions(self, ds: Dataset, when: str, run: str, people: PeopleResolver) -> dict:
        snaps = {t.upper(): p for t, p in ds.snapshots.get("positions", {}).items()}
        fresh = self._new_sources("HOLDS_POSITION", snaps)
        rows: dict[tuple, dict] = {}
        recs = []
        for p in ds.positions:
            ticker = p.company_ticker.upper()
            title, role = clean(p.title), p.role or "executive"
            name = clean(p.person_name)
            ov = people.match_override(name, ticker, p.source, "officer") if name else None
            key, method = people.resolve(name, ticker, p.source, "officer") if name else ("", "")
            r = {
                "ticker": ticker, "key": key,
                "name": clean(p.person_name), "title": title, "role": role,
                "role_std": role_std(title, role), "post": post_key(title, role),
                "since": _opt(parse_date(p.since)), "until": _opt(parse_date(p.until)),
                "source": p.source,
                "authoritative": ticker in snaps and _under(p.source, snaps[ticker]) and not ds.supplementary,
                "baseline": ticker in fresh,
            }
            if r["key"]:
                # one row per seat and source; other spellings of the seat go into `titles`
                k = (r["ticker"], r["key"], r["post"], r["until"], r["source"])
                titles = rows[k]["titles"] if k in rows else []
                r["titles"] = titles + [title] if title not in titles else titles
                r["records"] = self._union(rows[k]["records"] if k in rows else [], [p._rid])
                rows[k] = r
                recs.append(rec.record_row("officer", p, ticker, key, method,
                                           ov if method == "manual_override" else None))
        active = [r for r in rows.values() if r["until"] is None]
        ended = [r for r in rows.values() if r["until"] is not None]

        merge_person = """
            MATCH (c:Company {ticker: r.ticker})
            MERGE (p:Entity {key: r.key})
            ON CREATE SET p.name = r.name
            SET p:Person
        """
        opened = self.run(
            "UNWIND $rows AS r" + merge_person + """
            MERGE (p)-[h:HOLDS_POSITION {post: r.post, current: true}]->(c)
            ON CREATE SET h.first_seen = $when, h.opened_run = $run, h.new = true, h.source = r.source,
                          h.title = r.title, h.baseline = CASE WHEN r.baseline THEN true END
            WITH r, h, coalesce(h.sources, [x IN [h.source] WHERE x IS NOT NULL]) AS have,
                 coalesce(h.titles, [x IN [h.title] WHERE x IS NOT NULL]) AS spelt
            SET h.sources = CASE WHEN r.source IN have THEN have ELSE have + r.source END,
                h.titles = spelt + [t IN r.titles WHERE NOT t IN spelt],
                h.records = coalesce(h.records, []) + [x IN r.records WHERE NOT x IN coalesce(h.records, [])]
            // as for stakes: another source can't take the edge over (see _load_shareholdings)
            WITH r, h, r.authoritative OR h.source = r.source OR h.source IS NULL AS owner
            SET h.role = CASE WHEN owner THEN r.role ELSE h.role END,
                h.role_std = CASE WHEN owner OR h.role_std IS NULL THEN r.role_std ELSE h.role_std END,
                h.title = CASE WHEN owner THEN r.title ELSE h.title END,
                h.source = CASE WHEN owner THEN r.source ELSE h.source END,
                h.since = coalesce(h.since, r.since),
                h.last_seen = CASE WHEN coalesce(h.last_seen, '') < $when THEN $when ELSE h.last_seen END
            WITH h, h.new AS new
            REMOVE h.new
            RETURN count(CASE WHEN new THEN 1 END) AS n
            """,
            rows=active, when=when, run=run,
        )[0]["n"]

        # Rows that carry their own end date (e.g. a source's history table or a
        # hand-made CSV): close the matching open stint, or record a past one.
        self.run(
            "UNWIND $rows AS r" + merge_person + """
            WITH r, p, c
            OPTIONAL MATCH (p)-[o:HOLDS_POSITION {post: r.post, current: true}]->(c)
            WHERE r.since IS NULL OR o.since IS NULL OR o.since = r.since
            CALL (r, o) {
                WITH r, o WHERE o IS NOT NULL
                WITH r, o, coalesce(o.titles, [x IN [o.title] WHERE x IS NOT NULL]) AS spelt
                SET o.current = false, o.until = r.until, o.since = coalesce(o.since, r.since),
                    o.role = r.role, o.role_std = r.role_std, o.title = r.title,
                    o.titles = spelt + [t IN r.titles WHERE NOT t IN spelt],
                    o.records = coalesce(o.records, []) + [x IN r.records WHERE NOT x IN coalesce(o.records, [])],
                    o.source = r.source, o.closed_at = $when, o.closed_run = $run
            }
            CALL (r, p, c, o) {
                WITH r, p, c, o WHERE o IS NULL
                MERGE (p)-[h:HOLDS_POSITION {post: r.post, current: false, until: r.until}]->(c)
                ON CREATE SET h.first_seen = $when, h.last_seen = $when, h.titles = []
                SET h.role = r.role, h.role_std = r.role_std, h.title = r.title, h.source = r.source,
                    h.titles = h.titles + [t IN r.titles WHERE NOT t IN h.titles],
                    h.records = coalesce(h.records, []) + [x IN r.records WHERE NOT x IN coalesce(h.records, [])],
                    h.since = coalesce(h.since, r.since)
            }
            """,
            rows=ended, when=when, run=run,
        )

        # People missing from a company's complete snapshot have left.
        seen: dict[str, list[str]] = {}
        for r in active:
            if r["ticker"] in snaps:
                seen.setdefault(r["ticker"], []).append(r["key"] + "|" + r["post"])
        scopes = [{"ticker": t, "prefix": snaps[t], "under": snaps[t].rstrip("/") + "/", "seen": keys}
                  for t, keys in seen.items()]
        closed = self.run(
            """
            UNWIND $scopes AS s
            MATCH (p:Person)-[h:HOLDS_POSITION {current: true}]->(c:Company {ticker: s.ticker})
            WHERE (h.source = s.prefix OR h.source STARTS WITH s.under) AND NOT (p.key + '|' + h.post) IN s.seen
            SET h.current = false, h.until = $when, h.closed_at = $when, h.closed_run = $run
            RETURN count(h) AS n
            """,
            scopes=scopes, when=when, run=run,
        )[0]["n"]
        self._write_records(recs, when)
        return {"positions_opened": opened, "positions_closed": closed + len(ended)}

    def _load_beneficial_owners(self, ds: Dataset, when: str, run: str, people: PeopleResolver) -> dict:
        """Registry beneficial owners: one open edge per owner and company, closed
        when the company's registry page (a snapshot) no longer lists the owner."""
        if not ds.beneficial_owners and not ds.snapshots.get("beneficial_owners"):
            return {}
        alias_map = {row["a"]: row["key"] for row in self.run(
            "MATCH (c:Company) UNWIND c.aliases AS a RETURN a, c.key AS key")}
        # An owner the registry names by its registration number is that company,
        # whatever it is called on the page.
        reg_map = {row["reg"]: row["key"] for row in self.run(
            "MATCH (c:Company) WHERE c.register_no =~ $re RETURN c.register_no AS reg, c.key AS key",
            re=REGISTER_NO_RE)}
        snaps = {t.upper(): p for t, p in ds.snapshots.get("beneficial_owners", {}).items()}
        fresh = self._new_sources("BENEFICIAL_OWNER", snaps)
        rows = {}
        recs = []
        for b in ds.beneficial_owners:
            name = clean(b.owner_name)
            if not name:
                continue
            reg = "".join(ch for ch in (b.owner_reg_no or "") if ch.isdigit())
            key = entity_key(name, b.owner_type)
            ticker = b.company_ticker.upper()
            ov = people.match_override(name, ticker, b.source, "beneficial_owner", b.owner_type)
            if ov is not None:
                key, label, method = ov.target, ov.label, "manual_override"
            elif reg in reg_map:
                key, label, method = reg_map[reg], "Company", "register_no"
            else:
                label = "Company" if key in alias_map else (
                    "Organization" if (b.owner_type or ("organization" if is_organization(name) else "person"))
                    .startswith("org") else "Person")
                method = "alias" if label == "Company" else "exact_key"
                if label == "Person":
                    key, method = people.resolve(name, b.company_ticker, b.source, "beneficial_owner")
                else:
                    key = alias_map.get(key, key)
            recs.append(rec.record_row("beneficial_owner", b, ticker, key, method, ov))
            prev = rows.get((ticker, key))
            rows[(ticker, key)] = {"ticker": ticker, "key": key, "name": name, "reg": reg or None,
                                   "label": label, "org_type": _org_type(b.org_type), "since": _opt(parse_date(b.since)), "source": b.source,
                                   "baseline": ticker in fresh,
                                   "records": self._union(prev["records"] if prev else [], [b._rid])}
        rows = list(rows.values())
        for label in ("Person", "Organization"):
            self.run(
                f"""
                UNWIND $rows AS r
                MERGE (e:Entity {{key: r.key}})
                ON CREATE SET e.name = r.name
                SET e:{label}
                """,
                rows=[r for r in rows if r["label"] == label],
            )
        # an organisation the registry names by its number: once a company has that
        # number, consolidate_companies merges the two
        self.run("""UNWIND $rows AS r MATCH (o:Organization {key: r.key}) WHERE NOT o:Company
                    SET o.register_no = coalesce(o.register_no, r.reg)""",
                 rows=[r for r in rows if r["label"] == "Organization" and r["reg"]])
        self._set_org_types(rows)
        opened = self.run(
            """
            UNWIND $rows AS r
            MATCH (c:Company {ticker: r.ticker})
            MATCH (e:Entity {key: r.key})
            WHERE e <> c
            MERGE (e)-[b:BENEFICIAL_OWNER {current: true}]->(c)
            ON CREATE SET b.first_seen = $when, b.opened_run = $run, b.new = true,
                          b.baseline = CASE WHEN r.baseline THEN true END
            SET b.since = coalesce(r.since, b.since), b.source = r.source,
                b.owner_reg = coalesce(r.reg, b.owner_reg),
                b.records = coalesce(b.records, []) + [x IN r.records WHERE NOT x IN coalesce(b.records, [])],
                b.last_seen = CASE WHEN coalesce(b.last_seen, '') < $when THEN $when ELSE b.last_seen END
            WITH b, b.new AS new
            REMOVE b.new
            RETURN count(CASE WHEN new THEN 1 END) AS n
            """,
            rows=rows, when=when, run=run,
        )[0]["n"]
        seen: dict[str, list[str]] = {t: [] for t in snaps}
        for r in rows:
            if r["ticker"] in seen:
                seen[r["ticker"]].append(r["key"])
        # unlike shareholder tables, an empty list of beneficial owners is common, but
        # still treat it as "no information" so a failed parse doesn't close everything
        scopes = [{"ticker": t, "prefix": snaps[t], "under": snaps[t].rstrip("/") + "/", "seen": keys}
                  for t, keys in seen.items() if keys]
        closed = self.run(
            """
            UNWIND $scopes AS s
            MATCH (e:Entity)-[b:BENEFICIAL_OWNER {current: true}]->(c:Company {ticker: s.ticker})
            WHERE (b.source = s.prefix OR b.source STARTS WITH s.under) AND NOT e.key IN s.seen
            SET b.current = false, b.until = $when, b.closed_at = $when, b.closed_run = $run
            RETURN count(b) AS n
            """,
            scopes=scopes, when=when, run=run,
        )[0]["n"]
        self._write_records(recs, when)
        return {"beneficial_owners_opened": opened, "beneficial_owners_closed": closed}

    def _load_facts(self, ds: Dataset):
        """Registry facts: fill empty company properties only."""
        rows = []
        for f in ds.facts:
            register, state = split_reg_numbers(f.register_no, f.reg_no)
            acts = split_activities(f.activities)
            codes = [c for c in (parse_activity(a)[0] for a in acts) if c]
            main = main_activity(codes)
            std = isic_section(main)
            rows.append({**f.__dict__, "ticker": f.ticker.upper(), "register_no": register, "state_reg_no": state,
                         "activities": acts, "activity_codes": codes, "activity_main": main or None,
                         "sector_std": std or None,
                         "sector_std_name": sector_std_name(std) or None})
        self.run(
            """
            UNWIND $rows AS r
            MATCH (c:Company {ticker: r.ticker})
            SET c.state_reg_no = CASE WHEN coalesce(c.state_reg_no, '') = '' AND r.state_reg_no <> ''
                                      THEN r.state_reg_no ELSE c.state_reg_no END,
                c.address = CASE WHEN coalesce(c.address, '') = '' THEN r.address ELSE c.address END,
                c.legal_form = CASE WHEN r.legal_form <> '' THEN r.legal_form ELSE c.legal_form END,
                c.founded = CASE WHEN r.founded <> '' THEN r.founded ELSE c.founded END,
                c.registry_name = CASE WHEN r.registry_name <> '' THEN r.registry_name ELSE c.registry_name END,
                c.registry_updated = CASE WHEN r.registry_updated <> '' THEN r.registry_updated
                                          ELSE c.registry_updated END,
                c.registry_url = CASE WHEN r.registry_url <> '' THEN r.registry_url ELSE c.registry_url END,
                c.register_no = CASE WHEN r.register_no <> '' THEN r.register_no ELSE c.register_no END
            // the registry's activities replace the ones it gave before (issue #38)
            FOREACH (_ IN CASE WHEN size(r.activity_codes) > 0 THEN [1] ELSE [] END |
                SET c.activities = r.activities, c.activity_codes = r.activity_codes, c.activity_main = r.activity_main,
                    c.sector_std = r.sector_std, c.sector_std_name = r.sector_std_name,
                    c.sector_std_by = CASE WHEN r.sector_std IS NULL THEN null ELSE 'activity' END)
            """,
            rows=rows,
        )

    # ---- dated company facts (issue #38) ----------------------------------------

    def _record_facts(self, ds: Dataset, companies: list[dict], when: str, run: str) -> dict:
        """Keep the dataset's company facts as dated (:Fact) chains (module docstring).
        `companies` are load()'s company rows. A registry page's facts come before the
        company rows of the same source; rows merged into another company (merge_only)
        give none. Returns the counts of facts changed, and of companies renamed."""
        items: dict[tuple, dict] = {}

        def add(ticker: str, name: str, value: str, source: str) -> None:
            value = clean(value)
            if value and (ticker, name, source) not in items:
                items[(ticker, name, source)] = {"ticker": ticker, "name": name, "value": value,
                                                 "norm": _fact_norm(name, value), "source": source}

        for f in ds.facts:
            src = fact_source(f.registry_url) or REGISTRY_MARK
            codes = [c for c in (parse_activity(a)[0] for a in split_activities(f.activities)) if c]
            for name, value in (("name", f.registry_name), ("address", f.address),
                                ("legal_form", f.legal_form), ("activity_codes", "; ".join(codes))):
                add(f.ticker.upper(), name, value, src)
        for r in companies:
            if r["merge_only"]:
                continue
            src = company_row_source(r.get("url"))
            for name, field in COMPANY_FACTS.items():
                add(r["ticker"], name, r.get(field) or "", src)
        if not items:
            return {}
        got = self.run(
            """
            UNWIND $rows AS r
            MATCH (c:Company {ticker: r.ticker})
            OPTIONAL MATCH (c)-[:HAS_FACT]->(f:Fact {name: r.name, source: r.source})
            WITH c, r, collect(f) AS had
            WITH c, r, size(had) = 0 AS first, [f IN had WHERE f.current] AS open
            WITH c, r, first, [f IN open WHERE f.norm = r.norm] AS same, [f IN open WHERE f.norm <> r.norm] AS other
            FOREACH (f IN same | SET f.last_seen = CASE WHEN $when > f.last_seen THEN $when ELSE f.last_seen END,
                                     f.value = r.value)
            FOREACH (f IN other | SET f.current = false, f.until = $when, f.closed_at = $when, f.closed_run = $run)
            FOREACH (_ IN CASE WHEN size(same) = 0 THEN [1] ELSE [] END |
                CREATE (c)-[:HAS_FACT]->(:Fact {name: r.name, value: r.value, norm: r.norm, source: r.source,
                                                current: true, first_seen: $when, last_seen: $when,
                                                opened_run: $run, baseline: first}))
            RETURN r.name AS name, size(other) AS closed
            """, rows=list(items.values()), when=when, run=run)
        changed = [r for r in got if r["closed"]]
        return {"company_facts_changed": len(changed),
                "companies_renamed": sum(1 for r in changed if r["name"] == "name")}

    def _refresh_former_names(self) -> int:
        """`former_names` (closed `name` / `name_en` facts that are not a current name) and their
        spellings in `former_aliases` and `aliases`. Returns the companies updated."""
        rows = self.run(
            """
            MATCH (c:Company)-[:HAS_FACT]->(f:Fact) WHERE f.name IN ['name', 'name_en']
            WITH c, collect(f) AS fs
            RETURN elementId(c) AS id, [c.name, c.name_en] AS names, coalesce(c.former_names, []) AS had,
                   coalesce(c.former_aliases, []) AS had_a,
                   [f IN fs WHERE f.current | f.value] AS now, [f IN fs WHERE NOT f.current | f.value] AS old
            """)
        updates = []
        for r in rows:
            now = {_fact_norm("name", v) for v in r["now"] + [n for n in r["names"] if n]}
            former, seen = [], set()
            for v in sorted(r["old"], key=str.lower):
                if (k := _fact_norm("name", v)) not in now and k not in seen:
                    seen.add(k)
                    former.append(v)
            keys = sorted({entity_key(v, k) for n in former for v in [n, *_org_variants(n)]
                           for k in ("", "organization")} - {""})
            if former != r["had"] or keys != r["had_a"]:
                updates.append({"id": r["id"], "former": former or None, "keys": keys or None})
        self.run(
            """
            UNWIND $rows AS r
            MATCH (c) WHERE elementId(c) = r.id
            SET c.former_names = r.former, c.former_aliases = r.keys,
                c.aliases = coalesce(c.aliases, []) + [a IN coalesce(r.keys, []) WHERE NOT a IN coalesce(c.aliases, [])]
            """, rows=updates)
        return len(updates)

    def _derive_sectors(self) -> int:
        """The shown `sector`: the company list's `sector_mse`, else the standard sector's name."""
        return self.run(
            """
            MATCH (c:Company)
            WITH c, CASE WHEN coalesce(c.sector_mse, '') <> '' THEN c.sector_mse ELSE c.sector_std_name END AS want
            WHERE coalesce(c.sector, '') <> coalesce(want, '')
            SET c.sector = want
            RETURN count(c) AS n
            """)[0]["n"]

    def company_facts(self, key: str) -> list[dict]:
        """Every dated fact of a company, current first, then the most recently ended."""
        rows = self.run(
            """
            MATCH (c:Company {key: $key})-[:HAS_FACT]->(f:Fact)
            RETURN f.name AS name, f.value AS value, f.source AS source, f.current AS current,
                   f.first_seen AS first_seen, f.last_seen AS last_seen, f.until AS until
            ORDER BY f.current DESC, coalesce(f.until, '') DESC, f.name, f.source
            """, key=self.resolve_key(key))
        return rows

    def companies_with_reg(self, retry_days: int = 30) -> list[tuple[str, str]]:
        """(ticker, number to look up in the registry) of every company that has one, listed first.

        Only the 7-digit register number (`register_no`) counts: mse.mn's 10-digit state
        registration number (`state_reg_no`) is not one the registry knows. A number the
        registry answered "not found" for is left out for `retry_days` (see note_registry_checks)."""
        return [(r["t"], r["reg"]) for r in self.run(
            """
            MATCH (c:Company)
            WITH c, c.register_no AS reg
            WHERE reg =~ $re AND NOT EXISTS {
                MATCH (l:RegistryLead {reg: reg}) WHERE l.status = 'not found' AND l.checked_at >= $after }
            RETURN c.ticker AS t, reg
            ORDER BY coalesce(c.listed, true) DESC, c.ticker
            """, re=REGISTER_NO_RE, after=_days_ago(retry_days))]

    # ---- registry discovery (discovery.py) ------------------------------------

    def note_registry_checks(self, rows: list[dict]) -> None:
        """Remember registry numbers that led nowhere, so they rest for a while.

        `rows`: {"reg", "status", "name"?, "ticker"?}. "not found" and "name clash"
        are stored as (:RegistryLead) with today's date; "ok" forgets the number.
        Other statuses (a failed download) are not remembered: the next run tries again.
        """
        keep = [{"reg": r["reg"], "status": r["status"], "name": r.get("name") or None,
                 "ticker": r.get("ticker") or None}
                for r in rows if r.get("reg") and r.get("status") in ("not found", "name clash")]
        if keep:
            self.run(
                """
                UNWIND $rows AS r
                MERGE (l:RegistryLead {reg: r.reg})
                SET l.status = r.status, l.checked_at = $today,
                    l.name = coalesce(r.name, l.name), l.ticker = coalesce(r.ticker, l.ticker)
                """, rows=keep, today=_today())
        ok = sorted({r["reg"] for r in rows if r.get("reg") and r.get("status") == "ok"})
        if ok:
            self.run("UNWIND $regs AS reg MATCH (l:RegistryLead {reg: reg}) DELETE l", regs=ok)

    def discovered_tickers(self) -> set[str]:
        """Companies discovery added (see discovery.weekly_selection)."""
        return {r["t"] for r in self.run("MATCH (c:Company) WHERE c.discovered_at IS NOT NULL RETURN c.ticker AS t")}

    def resting_registry_numbers(self, retry_days: int = 30) -> set[str]:
        """Numbers noted as dead ends (note_registry_checks) within the last `retry_days`."""
        return {r["reg"] for r in self.run("MATCH (l:RegistryLead) WHERE l.checked_at >= $after RETURN l.reg AS reg",
                                           after=_days_ago(retry_days))}

    def owner_leads(self, max_depth: int, retry_days: int = 30) -> list[dict]:
        """Registry numbers of owners in beneficial-owner chains that the graph has no
        company for yet: {"reg", "name", "owner_key", "depth", "via"}.

        A company's depth is how many such steps it is from the companies the graph
        started with (listed, hand-curated, seeds: 0); a lead is one deeper than the
        shallowest company whose page names it. Numbers noted as dead ends rest for
        `retry_days`."""
        return self.run(
            """
            MATCH (e:Entity)-[b:BENEFICIAL_OWNER {current: true}]->(c:Company)
            WHERE b.owner_reg IS NOT NULL
            WITH b.owner_reg AS reg, e, c
            WITH reg, collect(DISTINCT e) AS owners, min(coalesce(c.discovery_depth, 0)) + 1 AS depth,
                 collect(DISTINCT c.ticker) AS via
            WHERE depth <= $max_depth
              AND NOT EXISTS { MATCH (x:Company) WHERE x.register_no = reg }
              AND NOT EXISTS { MATCH (l:RegistryLead {reg: reg}) WHERE l.checked_at >= $after }
            RETURN reg, owners[0].name AS name, owners[0].key AS owner_key, depth, via
            ORDER BY depth, size(via) DESC, reg
            """, max_depth=max_depth, after=_days_ago(retry_days))

    def unread_companies(self, retry_days: int = 30) -> list[dict]:
        """Companies with a register number whose registry page was never read (a new
        listing, a new hand-curated company): {"ticker", "name", "reg", "depth"}."""
        return self.run(
            """
            MATCH (c:Company) WHERE c.registry_url IS NULL
            WITH c, c.register_no AS reg
            WHERE reg =~ $re AND NOT EXISTS {
                MATCH (l:RegistryLead {reg: reg}) WHERE l.checked_at >= $after }
            RETURN c.ticker AS ticker, c.name AS name, reg, coalesce(c.discovery_depth, 0) AS depth
            ORDER BY coalesce(c.listed, true) DESC, c.ticker
            """, re=REGISTER_NO_RE, after=_days_ago(retry_days))

    def company_registers(self) -> dict[str, dict]:
        """ticker -> {"reg": its register number or None, "listed": bool}."""
        return {r["t"]: {"reg": r["reg"], "listed": r["listed"]} for r in self.run(
            """
            MATCH (c:Company)
            RETURN c.ticker AS t, coalesce(c.listed, true) AS listed,
                   CASE WHEN c.register_no =~ $re THEN c.register_no END AS reg
            """, re=REGISTER_NO_RE)}

    def listed_without_register_no(self) -> list[dict]:
        """Listed companies the registry can't be asked about yet: no register number."""
        return self.run(
            """
            MATCH (c:Company) WHERE coalesce(c.listed, true)
              AND NOT coalesce(c.register_no, '') =~ $re
            RETURN c.ticker AS ticker, c.name AS name, c.state_reg_no AS reg_no ORDER BY c.ticker
            """, re=REGISTER_NO_RE)

    def mark_discovered(self, rows: list[dict]) -> None:
        """Record how discovery found companies: {"ticker", "via", "depth", "kind"}.
        Companies it added get `discovered_at` / `discovered_via` / `discovery_depth`;
        companies it found a register number for get `register_no_found_via`."""
        self.run(
            """
            UNWIND $rows AS r
            MATCH (c:Company {ticker: r.ticker})
            CALL (r, c) {
                WITH r, c WHERE r.kind = 'added'
                SET c.discovered_at = coalesce(c.discovered_at, $today),
                    c.discovered_via = r.via,
                    c.discovery_depth = coalesce(c.discovery_depth, r.depth)
            }
            CALL (r, c) {
                WITH r, c WHERE r.kind = 'attached' AND size(r.via) > 0
                SET c.register_no_found_via = r.via, c.register_no_found_at = $today
            }
            """, rows=rows, today=_today())

    def _merge_orgs_into_companies(self):
        rows = self.run(
            """
            MATCH (c:Company) UNWIND c.aliases AS a
            MATCH (o:Organization {key: a}) WHERE NOT o:Company
            RETURN c.ticker AS ticker, o.key AS okey
            """
        )
        for r in rows:
            self.run(
                """
                MATCH (o:Organization {key: $okey}), (c:Company {ticker: $ticker})
                CALL (o, c) {
                    // carry over every period (past and current) of the org's holdings
                    MATCH (o)-[old:OWNS]->(t:Company)
                    WHERE t <> c AND NOT (old.current AND EXISTS { (c)-[:OWNS {current: true}]->(t) })
                    CREATE (c)-[n:OWNS]->(t)
                    SET n = properties(old)
                }
                CALL (o, c) {
                    // ... and its place in other companies' beneficial-owner chains
                    MATCH (o)-[old:BENEFICIAL_OWNER]->(t:Company)
                    WHERE t <> c AND NOT (coalesce(old.current, false) AND
                                          EXISTS { (c)-[:BENEFICIAL_OWNER {current: true}]->(t) })
                    CREATE (c)-[n:BENEFICIAL_OWNER]->(t)
                    SET n = properties(old)
                }
                CALL (o, c) {
                    // ... and the source records that named it (issue #37)
                    MATCH (r:Record)-[x:REFERS_TO]->(o)
                    CREATE (r)-[y:REFERS_TO]->(c)
                    SET y = properties(x)
                    SET y.method = CASE WHEN x.method = 'manual_override' THEN x.method ELSE 'alias' END
                    DELETE x
                }
                DETACH DELETE o
                """,
                okey=r["okey"], ticker=r["ticker"],
            )

    def company_lookup(self) -> dict[str, str]:
        """alias key / "reg:<no>" -> ticker, for matching external datasets to companies."""
        out = {}
        for r in self.run("MATCH (c:Company) RETURN c.ticker AS t, c.aliases AS aliases, "
                          "[x IN [c.register_no, c.state_reg_no] WHERE coalesce(x, '') <> ''] AS regs"):
            for a in r["aliases"] or []:
                out.setdefault(a, r["t"])
            for reg in r["regs"]:
                out["reg:" + "".join(reg.split())] = r["t"]
        return out

    # ---- queries ------------------------------------------------------------

    def stats(self) -> dict:
        r = self.run(
            """
            CALL () { MATCH (c:Company) WHERE coalesce(c.listed, true) RETURN count(c) AS companies }
            CALL () { MATCH (c:Company) WHERE c.listed = false RETURN count(c) AS unlisted_companies }
            CALL () { MATCH (p:Person) RETURN count(p) AS people }
            CALL () { MATCH (o:Organization) RETURN count(o) AS organizations }
            CALL () { MATCH ()-[r:OWNS]->() RETURN count(CASE WHEN r.current THEN 1 END) AS ownerships,
                                                   count(CASE WHEN NOT r.current THEN 1 END) AS past_ownerships }
            CALL () { MATCH ()-[r:HOLDS_POSITION]->() RETURN count(CASE WHEN r.current THEN 1 END) AS positions,
                                                             count(CASE WHEN NOT r.current THEN 1 END) AS past_positions,
                                                             min(r.first_seen) AS first_observed,
                                                             max(r.last_seen) AS last_observed }
            CALL () { MATCH ()-[r:BENEFICIAL_OWNER {current: true}]->() RETURN count(r) AS beneficial_owners }
            CALL () { MATCH (r:Record) RETURN count(r) AS records }
            RETURN companies, unlisted_companies, people, organizations, ownerships, positions,
                   beneficial_owners, past_ownerships, past_positions, first_observed, last_observed, records
            """
        )
        return r[0]

    def search(self, q: str, limit: int = 20) -> list[dict]:
        q = clean(q).lower()
        return self.run(
            """
            MATCH (e:Entity)
            WHERE toLower(e.name) CONTAINS $q OR toLower(coalesce(e.name_en,'')) CONTAINS $q
               OR toLower(coalesce(e.ticker,'')) = $q
            RETURN e.key AS key, e.name AS name, e.ticker AS ticker,
                   [l IN labels(e) WHERE l IN ['Company', 'Person', 'Organization']][0] AS type,
                   COUNT { (e)-[:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER]-() } AS degree
            ORDER BY (toLower(coalesce(e.ticker,'')) = $q) DESC, degree DESC, name
            LIMIT $limit
            """,
            q=q, limit=limit,
        )

    def entity(self, key: str) -> dict | None:
        """An entity with its current links, plus `history`: the links that ended.

        Ended stakes include earlier sizes of a stake that is still held
        (e.g. 12% until 2026-03-01, then the current 15%). Old company keys resolve
        (resolve_key).
        """
        key = self.resolve_key(key)
        period = "current: r.current, since: r.since, until: r.until, first_seen: r.first_seen, last_seen: r.last_seen"
        rows = self.run(
            f"""
            MATCH (e:Entity {{key: $key}})
            CALL (e) {{
                OPTIONAL MATCH (h:Entity)-[r:OWNS]->(e)
                RETURN collect(CASE WHEN h IS NULL THEN NULL ELSE
                    {{key: h.key, name: h.name, type: {_TYPE.format("h")}, source: r.source, sources: r.sources,
                      percent: r.percent, shares: r.shares, as_of: r.as_of, {period}}} END) AS owners
            }}
            CALL (e) {{
                OPTIONAL MATCH (e)-[r:OWNS]->(t:Company)
                RETURN collect(CASE WHEN t IS NULL THEN NULL ELSE
                    {{key: t.key, name: t.name, ticker: t.ticker,
                      percent: r.percent, shares: r.shares, as_of: r.as_of, {period}}} END) AS holdings
            }}
            CALL (e) {{
                OPTIONAL MATCH (p:Person)-[r:HOLDS_POSITION]->(e)
                RETURN collect(CASE WHEN p IS NULL THEN NULL ELSE
                    {{key: p.key, name: p.name, title: r.title, titles: r.titles, role: r.role,
                      role_std: r.role_std, source: r.source, {period}}} END) AS people
            }}
            CALL (e) {{
                OPTIONAL MATCH (b:Entity)-[r:BENEFICIAL_OWNER]->(e)
                RETURN collect(CASE WHEN b IS NULL THEN NULL ELSE
                    {{key: b.key, name: b.name, type: {_TYPE.format("b")}, since: r.since, source: r.source,
                      {period}}} END) AS beneficial_owners
            }}
            CALL (e) {{
                OPTIONAL MATCH (e)-[r:BENEFICIAL_OWNER]->(t:Company)
                RETURN collect(CASE WHEN t IS NULL THEN NULL ELSE
                    {{key: t.key, name: t.name, ticker: t.ticker, since: r.since, {period}}} END) AS beneficially_owns
            }}
            CALL (e) {{
                OPTIONAL MATCH (e)-[r:HOLDS_POSITION]->(c:Company)
                RETURN collect(CASE WHEN c IS NULL THEN NULL ELSE
                    {{key: c.key, name: c.name, ticker: c.ticker, title: r.title, titles: r.titles, role: r.role,
                      role_std: r.role_std, {period}}} END) AS positions
            }}
            RETURN e {{.*, type: {_TYPE.format("e")}}} AS entity, owners, holdings, people, positions,
                   beneficial_owners, beneficially_owns
            """,
            key=key,
        )
        if not rows:
            return None
        row = rows[0]
        out = {"entity": row["entity"], "history": {}}
        for part in ("owners", "holdings", "people", "positions", "beneficial_owners", "beneficially_owns"):
            items = row[part]
            out[part] = [i for i in items if i["current"]]
            # most recently ended first
            out["history"][part] = sorted((i for i in items if not i["current"]),
                                          key=lambda i: i["until"] or "", reverse=True)
        out["owners"].sort(key=lambda x: -(x["percent"] or 0))
        company = out["entity"]["type"] == "Company"
        out["related_companies"] = self.related_companies(key) if company else []
        # dated facts (issue #38): current values, and the ones that ended (former names, old addresses)
        facts = self.company_facts(key) if company else []
        out["facts"] = [f for f in facts if f["current"]]
        out["history"]["facts"] = [f for f in facts if not f["current"]]
        return out

    def related_companies(self, key: str) -> list[dict]:
        """Other listed companies connected through a shared person or shareholder (now)."""
        return self.run(
            """
            MATCH (c:Company {key: $key})<-[r1:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER {current: true}]-(link:Entity)
                  -[r2:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER {current: true}]->(o:Company)
            WHERE o <> c
            RETURN o.key AS key, o.name AS name, o.ticker AS ticker,
                   collect(DISTINCT {key: link.key, name: link.name,
                                     via: type(r1) + '/' + type(r2)}) AS links
            ORDER BY size(links) DESC, name
            """,
            key=key,
        )

    def neighborhood(self, key: str, depth: int = 1, limit: int = 300) -> dict:
        depth = max(1, min(int(depth), 3))
        key = self.resolve_key(key)
        rows = self.run(
            f"""
            MATCH (e:Entity {{key: $key}})
            OPTIONAL MATCH path = (e)-[:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER*1..{depth}]-(:Entity)
            WHERE all(r IN relationships(path) WHERE r.current)
            WITH e, path LIMIT $limit
            WITH e, collect(path) AS paths
            WITH e, [p IN paths | nodes(p)] AS ns, [p IN paths | relationships(p)] AS rs
            WITH [e] + reduce(a = [], x IN ns | a + x) AS nodes, reduce(a = [], x IN rs | a + x) AS rels
            UNWIND nodes AS n WITH collect(DISTINCT n) AS nodes, rels
            UNWIND (CASE WHEN rels = [] THEN [null] ELSE rels END) AS r
            WITH nodes, collect(DISTINCT r) AS rels
            RETURN [n IN nodes | n {{.key, .name, .ticker, type: [l IN labels(n) WHERE l IN ['Company', 'Person', 'Organization']][0]}}] AS nodes,
                   [r IN rels | {{source: startNode(r).key, target: endNode(r).key, type: type(r),
                                  percent: r.percent, title: r.title, role: r.role}}] AS edges
            """,
            key=key, limit=limit,
        )
        return rows[0] if rows else {"nodes": [], "edges": []}

    def overview(self, limit: int = 400) -> dict:
        """Whole-market graph (trimmed): companies plus entities linked to 2+ companies."""
        rows = self.run(
            """
            MATCH (x:Entity)-[r:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER {current: true}]->(c:Company)
            WITH x, collect(DISTINCT c) AS cs, collect(r) AS rs
            WHERE size(cs) >= 2 OR x:Company
            WITH x, cs, rs LIMIT $limit
            UNWIND rs AS r
            WITH collect(DISTINCT x) + collect(DISTINCT endNode(r)) AS ns, collect(DISTINCT r) AS rels
            UNWIND ns AS n WITH collect(DISTINCT n) AS nodes, rels
            RETURN [n IN nodes | n {.key, .name, .ticker, type: [l IN labels(n) WHERE l IN ['Company', 'Person', 'Organization']][0]}] AS nodes,
                   [r IN rels | {source: startNode(r).key, target: endNode(r).key, type: type(r),
                                 percent: r.percent, title: r.title, role: r.role}] AS edges
            """,
            limit=limit,
        )
        return rows[0] if rows else {"nodes": [], "edges": []}

    def shortest_path(self, a: str, b: str, max_hops: int = 8) -> dict:
        max_hops = max(1, min(int(max_hops), 12))
        a, b = self.resolve_key(a), self.resolve_key(b)
        rows = self.run(
            f"""
            MATCH (a:Entity {{key: $a}}), (b:Entity {{key: $b}})
            MATCH p = shortestPath((a)-[:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER*..{max_hops}]-(b))
            WHERE all(r IN relationships(p) WHERE r.current)
            RETURN [n IN nodes(p) | n {{.key, .name, .ticker, type: [l IN labels(n) WHERE l IN ['Company', 'Person', 'Organization']][0]}}] AS nodes,
                   [r IN relationships(p) | {{source: startNode(r).key, target: endNode(r).key, type: type(r),
                                              percent: r.percent, title: r.title, role: r.role}}] AS edges
            """,
            a=a, b=b,
        )
        return rows[0] if rows else {"nodes": [], "edges": []}

    def ultimate_owners(self, ticker: str, max_depth: int = 5, min_percent: float = 1.0) -> list[dict]:
        """Look-through ownership: effective % = product of current stakes along each chain.

        Chains never pass through the same company twice: an ownership loop would
        otherwise add a partial, depth-dependent echo of the same stake. Loops are
        reported separately (analysis.cycles)."""
        max_depth = max(1, min(int(max_depth), 8))
        ticker = self.resolve_ticker(ticker)
        return self.run(
            f"""
            MATCH p = (owner:Entity)-[:OWNS*1..{max_depth}]->(c:Company {{ticker: $ticker}})
            WHERE all(r IN relationships(p) WHERE r.current AND r.percent IS NOT NULL)
              AND NOT (owner)<-[:OWNS {{current: true}}]-(:Entity)
              AND all(i IN range(0, size(nodes(p)) - 2) WHERE NOT nodes(p)[i] IN nodes(p)[i + 1..])
            WITH owner, p, reduce(x = 100.0, r IN relationships(p) | x * r.percent / 100.0) AS eff
            WITH owner, sum(eff) AS effective_percent,
                 collect([n IN nodes(p) | CASE WHEN n:Company AND coalesce(n.listed, true) THEN n.ticker ELSE n.name END]) AS chains
            WHERE effective_percent >= $min
            RETURN owner.key AS key, owner.name AS name,
                   [l IN labels(owner) WHERE l IN ['Company', 'Person', 'Organization']][0] AS type,
                   round(effective_percent, 3) AS effective_percent, chains
            ORDER BY effective_percent DESC
            """,
            ticker=ticker.upper(), min=min_percent,
        )

    def interlocks(self, min_companies: int = 2, limit: int = 100) -> list[dict]:
        """People who currently hold positions in (or own) several listed companies."""
        return self.run(
            """
            MATCH (p:Person)-[r:HOLDS_POSITION|OWNS|BENEFICIAL_OWNER {current: true}]->(c:Company)
            WITH p, collect(DISTINCT {ticker: c.ticker, name: c.name, key: c.key,
                                      rel: type(r), title: r.title, percent: r.percent}) AS links,
                 count(DISTINCT c) AS n
            WHERE n >= $min
            RETURN p.key AS key, p.name AS name, n AS companies, links
            ORDER BY n DESC, name LIMIT $limit
            """,
            min=min_companies, limit=limit,
        )

    def common_owners(self, min_companies: int = 2, limit: int = 100) -> list[dict]:
        """Shareholders (people, organisations or listed companies) in several listed companies."""
        return self.run(
            """
            MATCH (h:Entity)-[o:OWNS {current: true}]->(c:Company)
            WITH h, collect({ticker: c.ticker, name: c.name, key: c.key, percent: o.percent}) AS stakes
            WHERE size(stakes) >= $min
            RETURN h.key AS key, h.name AS name, [l IN labels(h) WHERE l IN ['Company', 'Person', 'Organization']][0] AS type,
                   size(stakes) AS companies, stakes
            ORDER BY companies DESC, name LIMIT $limit
            """,
            min=min_companies, limit=limit,
        )

    def board_overlap(self, limit: int = 100) -> list[dict]:
        """Pairs of companies currently sharing board members / executives."""
        return self.run(
            """
            MATCH (a:Company)<-[:HOLDS_POSITION {current: true}]-(p:Person)-[:HOLDS_POSITION {current: true}]->(b:Company)
            WHERE a.ticker < b.ticker
            RETURN a.ticker AS company_a, b.ticker AS company_b,
                   collect(DISTINCT p.name) AS shared_people, count(DISTINCT p) AS n
            ORDER BY n DESC LIMIT $limit
            """,
            limit=limit,
        )

    # ---- history ------------------------------------------------------------

    def moves(self, limit: int = 100) -> list[dict]:
        """People who used to hold a position at one listed company and now hold one at another."""
        return self.run(
            """
            MATCH (a:Company)<-[old:HOLDS_POSITION {current: false}]-(p:Person)
                  -[new:HOLDS_POSITION {current: true}]->(b:Company)
            WHERE a <> b AND NOT EXISTS { (p)-[:HOLDS_POSITION {current: true}]->(a) }
            WITH p, collect(DISTINCT {from_key: a.key, from: a.ticker, from_title: old.title,
                                      since: old.since, until: old.until}) AS past,
                 collect(DISTINCT {to_key: b.key, to: b.ticker, to_title: new.title,
                                   since: coalesce(new.since, new.first_seen)}) AS now
            RETURN p.key AS key, p.name AS name, past, now,
                   reduce(m = '', x IN past | CASE WHEN coalesce(x.until, '') > m THEN x.until ELSE m END) AS last_move
            ORDER BY last_move DESC, name LIMIT $limit
            """,
            limit=limit,
        )

    def changes(self, since: str, limit: int = 500) -> list[dict]:
        """Edges that started or ended on or after `since` (YYYY-MM-DD), newest first.

        "started" uses the date the scraper first saw the edge, so the very first
        load reports everything as started on that day.
        """
        return self.run(
            """
            CALL () {
                MATCH (x:Entity)-[r:OWNS|HOLDS_POSITION]->(c:Company) WHERE r.first_seen >= $since
                RETURN x, r, c, r.first_seen AS date, 'started' AS change
                UNION ALL
                MATCH (x:Entity)-[r:OWNS|HOLDS_POSITION]->(c:Company) WHERE r.until >= $since
                RETURN x, r, c, r.until AS date, 'ended' AS change
            }
            RETURN date, change, type(r) AS rel, x.key AS key, x.name AS name, c.ticker AS ticker,
                   r.title AS title, r.percent AS percent, r.shares AS shares
            ORDER BY date DESC, ticker, name LIMIT $limit
            """,
            since=since, limit=limit,
        )


def company_row_source(url: str | None) -> str:
    """The fact source of a company row: "mse.mn" for mse.mn's company list, else
    "dataset:<host>" (a manual dataset, discovery, the MCS data). Only a registry page
    itself (CompanyFacts) speaks as "opendatalab.mn", so a hand-typed name in a dataset
    never flip-flops with the registry's."""
    src = fact_source(url)
    return src if src == "mse.mn" else "dataset:" + (src or "-")


def _fact_norm(name: str, value: str) -> str:
    """What two values of a fact are compared on: case, quotes, punctuation and spacing
    never count as a change, nor (for names) a trailing legal form ("Ард даатгал ХК")."""
    if name in ("name", "name_en"):
        return entity_key(value, "organization")
    return " ".join(re.sub(r"[^\w;]+", " ", clean(value).casefold()).split())


def fact_source(url: str | None) -> str:
    """The source family of a company fact: "mse.mn" (mse.mn, open.mse.mn),
    "opendatalab.mn", another host, or "" for no URL."""
    from urllib.parse import urlparse

    host = (urlparse(url or "").hostname or "").lower()
    for family in ("mse.mn", REGISTRY_MARK):
        if host == family or host.endswith("." + family):
            return family
    return host.removeprefix("www.")


def _org_type(value: str | None) -> str:
    """A source's organisation type if it is one of ORG_TYPES, else ""."""
    v = clean(value).lower()
    return v if v in ORG_TYPES else ""


# Properties of a merged-away node that _merge_node never copies onto the node it keeps.
_OWN_PROPS = {"key", "ticker", "former_keys", "former_tickers", "aliases", "merged_aliases", "listed",
              "former_names", "former_aliases",
              "org_type", "org_type_by", "org_type_v",
              "first_seen", "first_run"}


def _add(*lists, skip=None) -> list:
    """The items of `lists` in order, once each, without None and `skip`."""
    out = []
    for items in lists:
        for x in items or []:
            if x is not None and x != skip and x not in out:
                out.append(x)
    return out


def _org_variants(name: str) -> list[str]:
    """Extra alias spellings for a listed company, e.g. with/without "ХК" suffix."""
    n = clean(name)
    if not n:
        return []
    base = n
    for suffix in (" ХК", " ХХК", " JSC", " LLC"):
        if base.upper().endswith(suffix):
            base = base[: -len(suffix)]
    return [base, base + " ХК", base + " ХХК", base + " JSC"]
