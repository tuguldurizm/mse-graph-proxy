"""Neo4j schema, loader and analytical queries.

Graph model
-----------
(:Entity:Company      {key, ticker, name, name_en, sector, reg_no, address, website, mse_id, url, listed, aliases})
(:Entity:Person       {key, name})
(:Entity:Organization {key, name})           -- non-listed corporate / state shareholders

(holder:Entity)-[:OWNS {percent, shares, as_of, source, <history>}]->(:Company)
(:Person)-[:HOLDS_POSITION {title, role, since, source, <history>}]->(:Company)

    <history> = current, first_seen, last_seen, until

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
history, `moves()` and `changes()`.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from typing import Any

from neo4j import GraphDatabase

from .config import settings
from .models import Dataset
from .normalize import clean, entity_key, is_aggregate, is_organization, parse_date

SCHEMA = [
    "CREATE CONSTRAINT entity_key IF NOT EXISTS FOR (e:Entity) REQUIRE e.key IS UNIQUE",
    "CREATE CONSTRAINT company_ticker IF NOT EXISTS FOR (c:Company) REQUIRE c.ticker IS UNIQUE",
    "CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)",
    "CREATE FULLTEXT INDEX entity_names IF NOT EXISTS FOR (e:Entity) ON EACH [e.name, e.name_en, e.ticker]",
    "CREATE INDEX owns_current IF NOT EXISTS FOR ()-[r:OWNS]-() ON (r.current)",
    "CREATE INDEX position_current IF NOT EXISTS FOR ()-[r:HOLDS_POSITION]-() ON (r.current)",
    "CREATE INDEX owns_until IF NOT EXISTS FOR ()-[r:OWNS]-() ON (r.until)",
    "CREATE INDEX position_until IF NOT EXISTS FOR ()-[r:HOLDS_POSITION]-() ON (r.until)",
]

# Graphs loaded before history tracking: every edge was the current state.
MIGRATE = """
MATCH ()-[r:OWNS|HOLDS_POSITION]->() WHERE r.current IS NULL
SET r.since = CASE WHEN r.since = '' THEN null ELSE r.since END,
    r.until = CASE WHEN r.until = '' THEN null ELSE r.until END,
    r.as_of = CASE WHEN r.as_of = '' THEN null ELSE r.as_of END
SET r.current = r.until IS NULL,
    r.first_seen = coalesce(r.first_seen, $today),
    r.last_seen = coalesce(r.last_seen, $today)
"""

DEMO_SOURCE = "DEMO-fictional"
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


def company_key(ticker: str) -> str:
    return "company:" + ticker.upper()


class Graph:
    def __init__(self, uri=None, user=None, password=None, database=None):
        self.driver = GraphDatabase.driver(
            uri or settings.neo4j_uri,
            auth=(user or settings.neo4j_user, password or settings.neo4j_password),
            notifications_min_severity="OFF",
        )
        self.database = database or settings.neo4j_database

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
        with self.session() as s:
            return [r.data() for r in s.run(cypher, **params)]

    # ---- schema / admin -----------------------------------------------------

    def init_schema(self):
        for stmt in SCHEMA:
            self.run(stmt)
        self.run(MIGRATE, today=_today())

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

    def drop_demo_data(self) -> bool:
        """Remove the fictional demo graph before real data is loaded on top of it."""
        if not self.has_demo_data():
            return False
        self.wipe()
        return True

    # ---- loading ------------------------------------------------------------

    def load(self, ds: Dataset, observed_at: str | None = None) -> dict:
        """Merge a dataset into the graph, keeping history (see module docstring).

        Load datasets in chronological order: `observed_at` (default: the
        dataset's own, else today) is the date stamped on new, refreshed and
        closed edges.
        """
        self.init_schema()
        when = observed_at or ds.observed_at or _today()
        companies = []
        for c in ds.companies:
            aliases = sorted({entity_key(n) for n in (c.name, c.name_en) if n} |
                             {entity_key(n) for n in _org_variants(c.name)})
            companies.append({**c.__dict__, "ticker": c.ticker.upper(),
                              "key": company_key(c.ticker), "aliases": aliases})
        self.run(
            """
            UNWIND $rows AS r
            MERGE (c:Entity:Company {ticker: r.ticker})
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
            rows=companies,
        )
        # Fold any :Organization created earlier (before this company was loaded)
        # into the matching :Company node.
        self._merge_orgs_into_companies()

        changes = {}
        changes.update(self._load_shareholdings(ds, when))
        changes.update(self._load_positions(ds, when))
        return {**self.stats(), "observed_at": when, "changes": changes}

    def _load_shareholdings(self, ds: Dataset, when: str) -> dict:
        alias_map = {row["a"]: row["key"] for row in self.run(
            "MATCH (c:Company) UNWIND c.aliases AS a RETURN a, c.key AS key")}
        snaps = {t.upper(): p for t, p in ds.snapshots.get("shareholdings", {}).items()}

        by_pair: dict[tuple, dict] = {}
        for h in ds.shareholdings:
            name = clean(h.holder_name)
            if not name or is_aggregate(name):
                continue  # "Бусад" / "Others" / totals are not real holders
            key = entity_key(name)
            if key in alias_map:
                key, label = alias_map[key], "Company"
            else:
                htype = h.holder_type or ("organization" if is_organization(name) else "person")
                label = "Organization" if htype.startswith("org") else "Person"
            ticker = h.company_ticker.upper()
            prefix = snaps.get(ticker)
            row = {
                "ticker": ticker, "key": key, "name": name, "label": label,
                "percent": h.percent, "shares": h.shares, "as_of": _opt(parse_date(h.as_of)),
                "source": h.source,
                # rows from a complete snapshot of the company define its current owners
                "authoritative": prefix is not None and _under(h.source, prefix),
            }
            prev = by_pair.get((ticker, key))
            if prev is None or row["authoritative"] or not prev["authoritative"]:
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
            // a different value replaces the open stake only if it comes from the
            // company's snapshot or from the same source; otherwise two sources that
            // disagree would close and reopen the stake on every load
            WITH r, c, e, o, changed, changed AND (r.authoritative OR o.source = r.source) AS supersede
            CALL (r, c, e, o, supersede) {
                WITH r, c, e, o, supersede WHERE o IS NULL OR supersede
                CREATE (e)-[n:OWNS {current: true}]->(c)
                SET n.percent = r.percent, n.shares = r.shares, n.as_of = r.as_of,
                    n.source = r.source, n.first_seen = $when, n.last_seen = $when
                WITH o WHERE o IS NOT NULL
                SET o.current = false, o.until = $when
            }
            CALL (r, o, changed) {
                WITH r, o WHERE o IS NOT NULL AND NOT changed
                SET o.last_seen = CASE WHEN coalesce(o.last_seen, '') < $when THEN $when ELSE o.last_seen END,
                    o.percent = coalesce(o.percent, r.percent),
                    o.shares = coalesce(o.shares, r.shares)
                WITH r, o WHERE r.authoritative OR o.source = r.source OR o.as_of IS NULL
                SET o.as_of = coalesce(r.as_of, o.as_of), o.source = r.source
            }
            RETURN count(CASE WHEN o IS NULL THEN 1 END) AS opened,
                   count(CASE WHEN supersede THEN 1 END) AS changed,
                   count(CASE WHEN changed AND NOT supersede THEN 1 END) AS conflicting
            """,
            rows=holdings, when=when, tol=PERCENT_TOLERANCE,
        )[0]

        # Holders missing from a company's complete snapshot have sold out. A
        # snapshot with no holders at all is treated as "no information" (a page
        # layout change, not every holder selling on the same day).
        seen: dict[str, list[str]] = {}
        for h in holdings:
            if h["authoritative"]:
                seen.setdefault(h["ticker"], []).append(h["key"])
        scopes = [{"ticker": t, "prefix": snaps[t], "under": snaps[t].rstrip("/") + "/", "seen": keys}
                  for t, keys in seen.items()]
        closed = self.run(
            """
            UNWIND $scopes AS s
            MATCH (e:Entity)-[o:OWNS {current: true}]->(c:Company {ticker: s.ticker})
            WHERE (o.source = s.prefix OR o.source STARTS WITH s.under) AND NOT e.key IN s.seen
            SET o.current = false, o.until = $when
            RETURN count(o) AS n
            """,
            scopes=scopes, when=when,
        )[0]["n"]
        return {"ownerships_opened": res["opened"], "ownerships_changed": res["changed"],
                "ownerships_closed": closed, "ownerships_conflicting": res["conflicting"]}

    def _load_positions(self, ds: Dataset, when: str) -> dict:
        snaps = {t.upper(): p for t, p in ds.snapshots.get("positions", {}).items()}
        rows: dict[tuple, dict] = {}
        for p in ds.positions:
            r = {
                "ticker": p.company_ticker.upper(), "key": entity_key(p.person_name),
                "name": clean(p.person_name), "title": clean(p.title), "role": p.role or "executive",
                "since": _opt(parse_date(p.since)), "until": _opt(parse_date(p.until)),
                "source": p.source,
            }
            if r["key"]:
                rows[(r["ticker"], r["key"], r["title"], r["until"])] = r
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
            MERGE (p)-[h:HOLDS_POSITION {title: r.title, current: true}]->(c)
            ON CREATE SET h.first_seen = $when, h.new = true
            SET h.role = r.role, h.source = r.source, h.since = coalesce(h.since, r.since),
                h.last_seen = CASE WHEN coalesce(h.last_seen, '') < $when THEN $when ELSE h.last_seen END
            WITH h, h.new AS new
            REMOVE h.new
            RETURN count(CASE WHEN new THEN 1 END) AS n
            """,
            rows=active, when=when,
        )[0]["n"]

        # Rows that carry their own end date (e.g. a source's history table or a
        # hand-made CSV): close the matching open stint, or record a past one.
        self.run(
            "UNWIND $rows AS r" + merge_person + """
            WITH r, p, c
            OPTIONAL MATCH (p)-[o:HOLDS_POSITION {title: r.title, current: true}]->(c)
            WHERE r.since IS NULL OR o.since IS NULL OR o.since = r.since
            CALL (r, o) {
                WITH r, o WHERE o IS NOT NULL
                SET o.current = false, o.until = r.until, o.since = coalesce(o.since, r.since),
                    o.role = r.role, o.source = r.source
            }
            CALL (r, p, c, o) {
                WITH r, p, c, o WHERE o IS NULL
                MERGE (p)-[h:HOLDS_POSITION {title: r.title, current: false, until: r.until}]->(c)
                ON CREATE SET h.first_seen = $when, h.last_seen = $when
                SET h.role = r.role, h.source = r.source, h.since = coalesce(h.since, r.since)
            }
            """,
            rows=ended, when=when,
        )

        # People missing from a company's complete snapshot have left.
        seen: dict[str, list[str]] = {}
        for r in active:
            if r["ticker"] in snaps:
                seen.setdefault(r["ticker"], []).append(r["key"] + "|" + r["title"])
        scopes = [{"ticker": t, "prefix": snaps[t], "under": snaps[t].rstrip("/") + "/", "seen": keys}
                  for t, keys in seen.items()]
        closed = self.run(
            """
            UNWIND $scopes AS s
            MATCH (p:Person)-[h:HOLDS_POSITION {current: true}]->(c:Company {ticker: s.ticker})
            WHERE (h.source = s.prefix OR h.source STARTS WITH s.under) AND NOT (p.key + '|' + h.title) IN s.seen
            SET h.current = false, h.until = $when
            RETURN count(h) AS n
            """,
            scopes=scopes, when=when,
        )[0]["n"]
        return {"positions_opened": opened, "positions_closed": closed + len(ended)}

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
                DETACH DELETE o
                """,
                okey=r["okey"], ticker=r["ticker"],
            )

    def company_lookup(self) -> dict[str, str]:
        """alias key / "reg:<no>" -> ticker, for matching external datasets to companies."""
        out = {}
        for r in self.run("MATCH (c:Company) RETURN c.ticker AS t, c.aliases AS aliases, c.reg_no AS reg"):
            for a in r["aliases"] or []:
                out.setdefault(a, r["t"])
            if r["reg"]:
                out["reg:" + "".join(r["reg"].split())] = r["t"]
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
            RETURN companies, unlisted_companies, people, organizations, ownerships, positions,
                   past_ownerships, past_positions, first_observed, last_observed
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
                    {{key: h.key, name: h.name, type: {_TYPE.format("h")},
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
                    {{key: p.key, name: p.name, title: r.title, role: r.role, {period}}} END) AS people
            }}
            CALL (e) {{
                OPTIONAL MATCH (e)-[r:HOLDS_POSITION]->(c:Company)
                RETURN collect(CASE WHEN c IS NULL THEN NULL ELSE
                    {{key: c.key, name: c.name, ticker: c.ticker, title: r.title, role: r.role, {period}}} END) AS positions
            }}
            RETURN e {{.*, type: {_TYPE.format("e")}}} AS entity, owners, holdings, people, positions
            """,
            key=key,
        )
        if not rows:
            return None
        row = rows[0]
        out = {"entity": row["entity"], "history": {}}
        for part in ("owners", "holdings", "people", "positions"):
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
            MATCH (c:Company {key: $key})<-[r1:OWNS|HOLDS_POSITION {current: true}]-(link:Entity)
                  -[r2:OWNS|HOLDS_POSITION {current: true}]->(o:Company)
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
            OPTIONAL MATCH path = (e)-[:OWNS|HOLDS_POSITION*1..{depth}]-(:Entity)
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
            MATCH (x:Entity)-[r:OWNS|HOLDS_POSITION {current: true}]->(c:Company)
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
            MATCH p = shortestPath((a)-[:OWNS|HOLDS_POSITION*..{max_hops}]-(b))
            WHERE all(r IN relationships(p) WHERE r.current)
            RETURN [n IN nodes(p) | n {{.key, .name, .ticker, type: [l IN labels(n) WHERE l <> 'Entity'][0]}}] AS nodes,
                   [r IN relationships(p) | {{source: startNode(r).key, target: endNode(r).key, type: type(r),
                                              percent: r.percent, title: r.title, role: r.role}}] AS edges
            """,
            a=a, b=b,
        )
        return rows[0] if rows else {"nodes": [], "edges": []}

    def ultimate_owners(self, ticker: str, max_depth: int = 5, min_percent: float = 1.0) -> list[dict]:
        """Look-through ownership: effective % = product of current stakes along each chain."""
        max_depth = max(1, min(int(max_depth), 8))
        return self.run(
            f"""
            MATCH p = (owner:Entity)-[:OWNS*1..{max_depth}]->(c:Company {{ticker: $ticker}})
            WHERE all(r IN relationships(p) WHERE r.current AND r.percent IS NOT NULL)
              AND NOT (owner)<-[:OWNS {{current: true}}]-(:Entity)
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
            MATCH (p:Person)-[r:HOLDS_POSITION|OWNS {current: true}]->(c:Company)
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
