"""Neo4j schema, loader and analytical queries.

Graph model
-----------
(:Entity:Company      {key, ticker, name, name_en, sector, reg_no, address, website, mse_id, url, listed, aliases})
(:Entity:Person       {key, name})
(:Entity:Organization {key, name})           -- non-listed corporate / state shareholders

(holder:Entity)-[:OWNS {percent, shares, as_of, source, sources, <history>}]->(:Company)
(:Person)-[:HOLDS_POSITION {post, role_std, title, titles, role, since, source, sources, <history>}]->(:Company)
(owner:Entity)-[:BENEFICIAL_OWNER {since, source, <history>}]->(:Company)  -- declared to the registry (Open Data Lab)

    <history> = current, first_seen, last_seen, until, closed_at, opened_run, closed_run, baseline

`source` is the source that owns an edge: only that source, or a complete
snapshot of the company, can change or close it. `sources` lists every source
that reported it (mse.mn and/or the Open Data Lab registry). A source without
a percentage never clears one that another source gave.

A position's `post` (normalize.post_key) is the seat it fills, e.g. "ceo" or
"board_member"; `role_std` is its standard role (normalize.ROLES_STD). Open
positions merge on (person, company, post), so a respelt title refreshes the
stint: `title` is the owning source's latest spelling, `titles` every spelling
seen. `role` is the source's coarse "board" / "executive".

`key` is the unique merge key (see normalize.entity_key). A shareholder whose
name matches a listed company's name / English name is linked to that
:Company node, which is how cross-holdings between listed companies appear.

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

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any

from neo4j import GraphDatabase

from .config import settings
from .models import Dataset
from .normalize import ROLES_VERSION, clean, entity_key, is_aggregate, is_organization, parse_date, \
    post_key, role_std
from .opendatalab import REGISTER_NO_RE

SCHEMA = [
    "CREATE CONSTRAINT entity_key IF NOT EXISTS FOR (e:Entity) REQUIRE e.key IS UNIQUE",
    "CREATE CONSTRAINT company_ticker IF NOT EXISTS FOR (c:Company) REQUIRE c.ticker IS UNIQUE",
    "CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)",
    "CREATE FULLTEXT INDEX entity_names IF NOT EXISTS FOR (e:Entity) ON EACH [e.name, e.name_en, e.ticker]",
    "CREATE INDEX owns_current IF NOT EXISTS FOR ()-[r:OWNS]-() ON (r.current)",
    "CREATE INDEX position_current IF NOT EXISTS FOR ()-[r:HOLDS_POSITION]-() ON (r.current)",
    "CREATE INDEX owns_until IF NOT EXISTS FOR ()-[r:OWNS]-() ON (r.until)",
    "CREATE INDEX position_until IF NOT EXISTS FOR ()-[r:HOLDS_POSITION]-() ON (r.until)",
    "CREATE INDEX company_reg_no IF NOT EXISTS FOR (c:Company) ON (c.reg_no)",
    "CREATE INDEX company_register_no IF NOT EXISTS FOR (c:Company) ON (c.register_no)",
    # registry numbers discovery tried and should leave alone for a while (discovery.py)
    "CREATE CONSTRAINT registry_lead_reg IF NOT EXISTS FOR (l:RegistryLead) REQUIRE l.reg IS UNIQUE",
]

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
# as_of values that compare correctly as strings (see normalize.parse_date)
ISO_DATE = r"\d{4}(-\d{2}){0,2}"
# Two stakes differing by less than this many percentage points count as equal.
PERCENT_TOLERANCE = 0.001

_TYPE = "[l IN labels({0}) WHERE l <> 'Entity'][0]"


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


def company_key(ticker: str) -> str:
    return "company:" + ticker.upper()


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

    def init_schema(self):
        for stmt in SCHEMA:
            self.run(stmt)
        self.run(MIGRATE, today=_today())
        for stmt in MIGRATE_CHANGES:
            self.run(stmt, today=_today())
        self.migrate_positions()
        self.rekey_entities()

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
                })
                self._drop_edges([r["oid"]])
                joined += 1
            total += joined
            if not joined:
                break
        return total

    def rekey_entities(self) -> int:
        """Give people and organisations the key the current entity_key() makes.

        When the key rules change (e.g. "Батын Болд" now matches "Б.Болд"), the
        next load would otherwise create new nodes and report everyone as having
        left and rejoined. Nodes that end up with the same key are merged; their
        edges move over unless the target already has the same open edge.
        Returns the number of nodes re-keyed or merged.
        """
        rows = self.run("""MATCH (e:Entity) WHERE (e:Person OR e:Organization) AND NOT e:Company
                           RETURN e.key AS key, e.name AS name, e:Organization AS org""")
        moves = []
        for r in rows:
            new = entity_key(r["name"] or "", "organization" if r["org"] else "person")
            if new and new != r["key"] and not r["key"].startswith("company:"):
                moves.append((r["key"], new))
        for old, new in moves:
            if not self.run("MATCH (n:Entity {key: $new}) RETURN count(n) > 0 AS x", new=new)[0]["x"]:
                self.run("MATCH (o:Entity {key: $old}) SET o.key = $new", old=old, new=new)
                continue
            for rel in ("OWNS", "HOLDS_POSITION", "BENEFICIAL_OWNER"):
                # copy every period over, except an open one the target already has
                self.run(
                    f"""
                    MATCH (o:Entity {{key: $old}})-[r:{rel}]->(t), (n:Entity {{key: $new}})
                    WHERE t <> n AND NOT (coalesce(r.current, false) AND EXISTS {{
                        MATCH (n)-[x:{rel} {{current: true}}]->(t)
                        WHERE coalesce(x.post, x.title, '') = coalesce(r.post, r.title, '') }})
                    CREATE (n)-[x:{rel}]->(t)
                    SET x = properties(r)
                    """,
                    old=old, new=new,
                )
            self.run("MATCH (o:Entity {key: $old}) DETACH DELETE o", old=old)
        return len(moves)

    def wipe(self):
        self.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 5000 ROWS")

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
        self.wipe()
        return True

    # ---- loading ------------------------------------------------------------

    def load(self, ds: Dataset, observed_at: str | None = None, run: str | None = None) -> dict:
        """Merge a dataset into the graph, keeping history (see module docstring).

        Load datasets in chronological order: `observed_at` (default: the
        dataset's own, else today) is the date stamped on new, refreshed and
        closed edges. `run` (default: a fresh UTC timestamp) is stamped on the
        edges this load opens or closes; history.run_changes() reads them back.
        """
        self.init_schema()
        when = observed_at or ds.observed_at or _today()
        run = run or new_run_id()
        companies = []
        for c in ds.companies:
            others = [n.strip() for n in (c.other_names or "").split("|") if n.strip()]
            aliases = sorted({entity_key(n) for n in (c.name, c.name_en) if n} |
                             {entity_key(n) for n in _org_variants(c.name)} |
                             # a bare former name ("Шунхлайгрупп") has no legal-form marker
                             {entity_key(n, k) for n in others for k in ("", "organization")})
            companies.append({**c.__dict__, "ticker": c.ticker.upper(),
                              "key": company_key(c.ticker), "aliases": aliases})
        self.run(
            """
            UNWIND $rows AS r
            MERGE (c:Entity:Company {ticker: r.ticker})
            ON CREATE SET c.first_seen = $when, c.first_run = $run
            SET c.key = r.key, c.name = r.name, c.listed = r.listed,
                c.aliases = r.aliases,
                c.name_en  = CASE WHEN r.name_en  <> '' THEN r.name_en  ELSE c.name_en  END,
                c.sector   = CASE WHEN r.sector   <> '' THEN r.sector   ELSE c.sector   END,
                c.reg_no   = CASE WHEN r.reg_no   <> '' THEN r.reg_no   ELSE c.reg_no   END,
                c.address  = CASE WHEN r.address  <> '' THEN r.address  ELSE c.address  END,
                c.website  = CASE WHEN r.website  <> '' THEN r.website  ELSE c.website  END,
                c.mse_id   = CASE WHEN r.mse_id   <> '' THEN r.mse_id   ELSE c.mse_id   END,
                c.url      = CASE WHEN r.url      <> '' THEN r.url      ELSE c.url      END
            """,
            rows=companies, when=when, run=run,
        )
        # Fold any :Organization created earlier (before this company was loaded)
        # into the matching :Company node.
        self._merge_orgs_into_companies()

        changes = {}
        changes.update(self._load_shareholdings(ds, when, run))
        changes.update(self._load_positions(ds, when, run))
        changes.update(self._load_beneficial_owners(ds, when, run))
        self._load_facts(ds)
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

    def _load_shareholdings(self, ds: Dataset, when: str, run: str) -> dict:
        alias_map = {row["a"]: row["key"] for row in self.run(
            "MATCH (c:Company) UNWIND c.aliases AS a RETURN a, c.key AS key")}
        snaps = {t.upper(): p for t, p in ds.snapshots.get("shareholdings", {}).items()}
        fresh = self._new_sources("OWNS", snaps)
        # "holdings" snapshots are keyed by the holder: a company's own list of its
        # stakes in other companies (subsidiaries, associates)
        hsnaps = {t.upper(): p for t, p in ds.snapshots.get("holdings", {}).items()}
        fresh_holders = self._new_holders(hsnaps)

        by_pair: dict[tuple, dict] = {}
        held: dict[str, list[str]] = {t: [] for t in hsnaps}   # holder -> targets it reported
        for h in ds.shareholdings:
            name = clean(h.holder_name)
            if not name or is_aggregate(name):
                continue  # "Бусад" / "Others" / totals are not real holders
            holder = h.holder_ticker.upper()
            key = entity_key(name, h.holder_type)
            if holder:
                key, label = company_key(holder), "Company"
            elif key in alias_map or entity_key(name) in alias_map:
                key, label = alias_map.get(key) or alias_map[entity_key(name)], "Company"
            else:
                htype = h.holder_type or ("organization" if is_organization(name) else "person")
                label = "Organization" if htype.startswith("org") else "Person"
            ticker = h.company_ticker.upper()
            prefix = snaps.get(ticker)
            in_holdings = holder in hsnaps and _under(h.source, hsnaps[holder])
            if in_holdings:
                held[holder].append(ticker)
            row = {
                "ticker": ticker, "key": key, "name": name, "label": label,
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
            if prev is None or row["rank"] >= prev["rank"]:
                by_pair[(ticker, key)] = row   # one row per holder and company
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
                    n.baseline = CASE WHEN r.baseline THEN true END
                WITH o WHERE o IS NOT NULL
                SET o.current = false, o.until = $when, o.closed_at = $when, o.closed_run = $run
            }
            CALL (r, o, changed) {
                WITH r, o WHERE o IS NOT NULL AND NOT changed
                SET o.last_seen = CASE WHEN coalesce(o.last_seen, '') < $when THEN $when ELSE o.last_seen END,
                    o.percent = coalesce(o.percent, r.percent),
                    o.shares = coalesce(o.shares, r.shares)
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
        return {"ownerships_opened": res["opened"], "ownerships_changed": res["changed"],
                "ownerships_closed": closed, "ownerships_conflicting": res["conflicting"]}

    def _new_holders(self, hsnaps: dict[str, str]) -> set[str]:
        """Companies whose own list of stakes in other companies is read for the first time."""
        if not hsnaps:
            return set()
        rows = self.run("UNWIND $t AS t MATCH (c:Company {ticker: t}) WHERE c.holdings_first_seen IS NULL "
                        "RETURN c.ticker AS t", t=list(hsnaps))
        return {r["t"] for r in rows}

    def _load_positions(self, ds: Dataset, when: str, run: str) -> dict:
        snaps = {t.upper(): p for t, p in ds.snapshots.get("positions", {}).items()}
        fresh = self._new_sources("HOLDS_POSITION", snaps)
        rows: dict[tuple, dict] = {}
        for p in ds.positions:
            ticker = p.company_ticker.upper()
            title, role = clean(p.title), p.role or "executive"
            r = {
                "ticker": ticker, "key": entity_key(p.person_name, "person"),
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
                rows[k] = r
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
                h.titles = spelt + [t IN r.titles WHERE NOT t IN spelt]
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
                    o.source = r.source, o.closed_at = $when, o.closed_run = $run
            }
            CALL (r, p, c, o) {
                WITH r, p, c, o WHERE o IS NULL
                MERGE (p)-[h:HOLDS_POSITION {post: r.post, current: false, until: r.until}]->(c)
                ON CREATE SET h.first_seen = $when, h.last_seen = $when, h.titles = []
                SET h.role = r.role, h.role_std = r.role_std, h.title = r.title, h.source = r.source,
                    h.titles = h.titles + [t IN r.titles WHERE NOT t IN h.titles],
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
        return {"positions_opened": opened, "positions_closed": closed + len(ended)}

    def _load_beneficial_owners(self, ds: Dataset, when: str, run: str) -> dict:
        """Registry beneficial owners: one open edge per owner and company, closed
        when the company's registry page (a snapshot) no longer lists the owner."""
        if not ds.beneficial_owners and not ds.snapshots.get("beneficial_owners"):
            return {}
        alias_map = {row["a"]: row["key"] for row in self.run(
            "MATCH (c:Company) UNWIND c.aliases AS a RETURN a, c.key AS key")}
        # An owner the registry names by its registration number is that company,
        # whatever it is called on the page.
        reg_map = {row["reg"]: row["key"] for row in self.run(
            """MATCH (c:Company) UNWIND [x IN [c.reg_no, c.register_no] WHERE x =~ $re] AS reg
               RETURN reg, c.key AS key""", re=REGISTER_NO_RE)}
        snaps = {t.upper(): p for t, p in ds.snapshots.get("beneficial_owners", {}).items()}
        fresh = self._new_sources("BENEFICIAL_OWNER", snaps)
        rows = {}
        for b in ds.beneficial_owners:
            name = clean(b.owner_name)
            if not name:
                continue
            reg = "".join(ch for ch in (b.owner_reg_no or "") if ch.isdigit())
            key = entity_key(name, b.owner_type)
            if reg in reg_map:
                key, label = reg_map[reg], "Company"
            else:
                label = "Company" if key in alias_map else (
                    "Organization" if (b.owner_type or ("organization" if is_organization(name) else "person"))
                    .startswith("org") else "Person")
                key = alias_map.get(key, key)
            ticker = b.company_ticker.upper()
            rows[(ticker, key)] = {"ticker": ticker, "key": key, "name": name, "reg": reg or None,
                                   "label": label, "since": _opt(parse_date(b.since)), "source": b.source,
                                   "baseline": ticker in fresh}
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
        return {"beneficial_owners_opened": opened, "beneficial_owners_closed": closed}

    def _load_facts(self, ds: Dataset):
        """Registry facts: fill empty company properties only."""
        rows = [{**f.__dict__, "ticker": f.ticker.upper()} for f in ds.facts]
        self.run(
            """
            UNWIND $rows AS r
            MATCH (c:Company {ticker: r.ticker})
            SET c.reg_no  = CASE WHEN coalesce(c.reg_no, '')  = '' THEN r.reg_no  ELSE c.reg_no  END,
                c.address = CASE WHEN coalesce(c.address, '') = '' THEN r.address ELSE c.address END,
                c.legal_form = CASE WHEN r.legal_form <> '' THEN r.legal_form ELSE c.legal_form END,
                c.founded = CASE WHEN r.founded <> '' THEN r.founded ELSE c.founded END,
                c.registry_name = CASE WHEN r.registry_name <> '' THEN r.registry_name ELSE c.registry_name END,
                c.registry_updated = CASE WHEN r.registry_updated <> '' THEN r.registry_updated
                                          ELSE c.registry_updated END,
                c.registry_url = CASE WHEN r.registry_url <> '' THEN r.registry_url ELSE c.registry_url END,
                c.register_no = CASE WHEN r.register_no <> '' THEN r.register_no ELSE c.register_no END
            """,
            rows=rows,
        )

    def companies_with_reg(self, retry_days: int = 30) -> list[tuple[str, str]]:
        """(ticker, number to look up in the registry) of every company that has one, listed first.

        The 7-digit register number (`register_no`) wins over `reg_no`, which for
        listed companies is mse.mn's 10-digit state registration number. Only numbers
        that can be register numbers count, and a number the registry answered "not
        found" for is left out for `retry_days` (see note_registry_checks)."""
        return [(r["t"], r["reg"]) for r in self.run(
            """
            MATCH (c:Company)
            WITH c, coalesce(c.register_no, c.reg_no) AS reg
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
              AND NOT EXISTS { MATCH (x:Company) WHERE x.reg_no = reg OR x.register_no = reg }
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
            WITH c, coalesce(c.register_no, c.reg_no) AS reg
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
                   [x IN [c.register_no, c.reg_no] WHERE x =~ $re][0] AS reg
            """, re=REGISTER_NO_RE)}

    def listed_without_register_no(self) -> list[dict]:
        """Listed companies the registry can't be asked about yet: no register number."""
        return self.run(
            """
            MATCH (c:Company) WHERE coalesce(c.listed, true)
              AND NOT coalesce(c.register_no, c.reg_no, '') =~ $re
            RETURN c.ticker AS ticker, c.name AS name, c.reg_no AS reg_no ORDER BY c.ticker
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
                DETACH DELETE o
                """,
                okey=r["okey"], ticker=r["ticker"],
            )

    def company_lookup(self) -> dict[str, str]:
        """alias key / "reg:<no>" -> ticker, for matching external datasets to companies."""
        out = {}
        for r in self.run("MATCH (c:Company) RETURN c.ticker AS t, c.aliases AS aliases, "
                          "[x IN [c.reg_no, c.register_no] WHERE coalesce(x, '') <> ''] AS regs"):
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
            RETURN companies, unlisted_companies, people, organizations, ownerships, positions,
                   beneficial_owners, past_ownerships, past_positions, first_observed, last_observed
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
                   [l IN labels(e) WHERE l <> 'Entity'][0] AS type,
                   COUNT { (e)--() } AS degree
            ORDER BY (toLower(coalesce(e.ticker,'')) = $q) DESC, degree DESC, name
            LIMIT $limit
            """,
            q=q, limit=limit,
        )

    def entity(self, key: str) -> dict | None:
        """An entity with its current links, plus `history`: the links that ended.

        Ended stakes include earlier sizes of a stake that is still held
        (e.g. 12% until 2026-03-01, then the current 15%).
        """
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
        out["related_companies"] = self.related_companies(key) if out["entity"]["type"] == "Company" else []
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
            RETURN [n IN nodes | n {{.key, .name, .ticker, type: [l IN labels(n) WHERE l <> 'Entity'][0]}}] AS nodes,
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
            RETURN [n IN nodes | n {.key, .name, .ticker, type: [l IN labels(n) WHERE l <> 'Entity'][0]}] AS nodes,
                   [r IN rels | {source: startNode(r).key, target: endNode(r).key, type: type(r),
                                 percent: r.percent, title: r.title, role: r.role}] AS edges
            """,
            limit=limit,
        )
        return rows[0] if rows else {"nodes": [], "edges": []}

    def shortest_path(self, a: str, b: str, max_hops: int = 8) -> dict:
        max_hops = max(1, min(int(max_hops), 12))
        rows = self.run(
            f"""
            MATCH (a:Entity {{key: $a}}), (b:Entity {{key: $b}})
            MATCH p = shortestPath((a)-[:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER*..{max_hops}]-(b))
            WHERE all(r IN relationships(p) WHERE r.current)
            RETURN [n IN nodes(p) | n {{.key, .name, .ticker, type: [l IN labels(n) WHERE l <> 'Entity'][0]}}] AS nodes,
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
                   [l IN labels(owner) WHERE l <> 'Entity'][0] AS type,
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
            RETURN h.key AS key, h.name AS name, [l IN labels(h) WHERE l <> 'Entity'][0] AS type,
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
