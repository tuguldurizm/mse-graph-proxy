"""Neo4j schema, loader and analytical queries.

Graph model
-----------
(:Entity:Company      {key, ticker, name, name_en, sector, reg_no, address, website, mse_id, url, listed, aliases})
(:Entity:Person       {key, name})
(:Entity:Organization {key, name})           -- non-listed corporate / state shareholders

(holder:Entity)-[:OWNS {percent, shares, as_of, source}]->(:Company)
(:Person)-[:HOLDS_POSITION {title, role, since, until, source}]->(:Company)

`key` is the unique merge key (see normalize.entity_key). A shareholder whose
name matches a listed company's name / English name is linked to that
:Company node, which is how cross-holdings between listed companies appear.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from neo4j import GraphDatabase

from .config import settings
from .models import Dataset
from .normalize import clean, entity_key, is_aggregate, is_organization

SCHEMA = [
    "CREATE CONSTRAINT entity_key IF NOT EXISTS FOR (e:Entity) REQUIRE e.key IS UNIQUE",
    "CREATE CONSTRAINT company_ticker IF NOT EXISTS FOR (c:Company) REQUIRE c.ticker IS UNIQUE",
    "CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)",
    "CREATE FULLTEXT INDEX entity_names IF NOT EXISTS FOR (e:Entity) ON EACH [e.name, e.name_en, e.ticker]",
]


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

    def wipe(self):
        self.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 5000 ROWS")

    # ---- loading ------------------------------------------------------------

    def load(self, ds: Dataset) -> dict:
        self.init_schema()
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

        alias_map = {row["a"]: row["key"] for row in self.run(
            "MATCH (c:Company) UNWIND c.aliases AS a RETURN a, c.key AS key")}

        holdings = []
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
            holdings.append({
                "ticker": h.company_ticker.upper(), "key": key, "name": name, "label": label,
                "percent": h.percent, "shares": h.shares, "as_of": h.as_of, "source": h.source,
            })
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
        self.run(
            """
            UNWIND $rows AS r
            MATCH (c:Company {ticker: r.ticker})
            MATCH (e:Entity {key: r.key})
            WHERE e <> c
            MERGE (e)-[o:OWNS]->(c)
            SET o.percent = r.percent, o.shares = r.shares, o.as_of = r.as_of, o.source = r.source
            """,
            rows=holdings,
        )

        positions = [{
            "ticker": p.company_ticker.upper(), "key": entity_key(p.person_name),
            "name": clean(p.person_name), "title": clean(p.title), "role": p.role or "executive",
            "since": p.since, "until": p.until, "source": p.source,
        } for p in ds.positions]
        self.run(
            """
            UNWIND $rows AS r
            MATCH (c:Company {ticker: r.ticker})
            MERGE (p:Entity {key: r.key})
            ON CREATE SET p.name = r.name
            SET p:Person
            MERGE (p)-[h:HOLDS_POSITION {title: r.title}]->(c)
            SET h.role = r.role, h.since = r.since, h.until = r.until, h.source = r.source
            """,
            rows=positions,
        )
        return self.stats()

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
                OPTIONAL MATCH (o)-[old:OWNS]->(t:Company)
                FOREACH (_ IN CASE WHEN t IS NULL OR t = c THEN [] ELSE [1] END |
                    MERGE (c)-[n:OWNS]->(t) SET n += properties(old))
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
            CALL () { MATCH ()-[r:OWNS]->() RETURN count(r) AS ownerships }
            CALL () { MATCH ()-[r:HOLDS_POSITION]->() RETURN count(r) AS positions }
            RETURN companies, unlisted_companies, people, organizations, ownerships, positions
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
        rows = self.run(
            """
            MATCH (e:Entity {key: $key})
            OPTIONAL MATCH (h:Entity)-[o:OWNS]->(e)
            WITH e, collect(CASE WHEN h IS NULL THEN NULL ELSE
                 {key: h.key, name: h.name, type: [l IN labels(h) WHERE l <> 'Entity'][0],
                  percent: o.percent, shares: o.shares}END) AS owners
            OPTIONAL MATCH (e)-[o2:OWNS]->(t:Company)
            WITH e, owners, collect(CASE WHEN t IS NULL THEN NULL ELSE
                 {key: t.key, name: t.name, ticker: t.ticker, percent: o2.percent, shares: o2.shares} END) AS holdings
            OPTIONAL MATCH (p:Person)-[r:HOLDS_POSITION]->(e)
            WITH e, owners, holdings, collect(CASE WHEN p IS NULL THEN NULL ELSE
                 {key: p.key, name: p.name, title: r.title, role: r.role} END) AS people
            OPTIONAL MATCH (e)-[r2:HOLDS_POSITION]->(c:Company)
            RETURN e {.*, type: [l IN labels(e) WHERE l <> 'Entity'][0]} AS entity,
                   owners, holdings, people,
                   collect(CASE WHEN c IS NULL THEN NULL ELSE
                     {key: c.key, name: c.name, ticker: c.ticker, title: r2.title, role: r2.role} END) AS positions
            """,
            key=key,
        )
        if not rows:
            return None
        out = rows[0]
        out["owners"].sort(key=lambda x: -(x["percent"] or 0))
        out["related_companies"] = self.related_companies(key) if out["entity"]["type"] == "Company" else []
        return out

    def related_companies(self, key: str) -> list[dict]:
        """Other listed companies connected through a shared person or shareholder."""
        return self.run(
            """
            MATCH (c:Company {key: $key})<-[r1:OWNS|HOLDS_POSITION]-(link:Entity)-[r2:OWNS|HOLDS_POSITION]->(o:Company)
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
        return self.run(
            """
            MATCH (x:Entity)-[r:OWNS|HOLDS_POSITION]->(c:Company)
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
        )[0] if self.stats()["companies"] else {"nodes": [], "edges": []}

    def shortest_path(self, a: str, b: str, max_hops: int = 8) -> dict:
        max_hops = max(1, min(int(max_hops), 12))
        rows = self.run(
            f"""
            MATCH (a:Entity {{key: $a}}), (b:Entity {{key: $b}})
            MATCH p = shortestPath((a)-[:OWNS|HOLDS_POSITION*..{max_hops}]-(b))
            RETURN [n IN nodes(p) | n {{.key, .name, .ticker, type: [l IN labels(n) WHERE l <> 'Entity'][0]}}] AS nodes,
                   [r IN relationships(p) | {{source: startNode(r).key, target: endNode(r).key, type: type(r),
                                              percent: r.percent, title: r.title, role: r.role}}] AS edges
            """,
            a=a, b=b,
        )
        return rows[0] if rows else {"nodes": [], "edges": []}

    def ultimate_owners(self, ticker: str, max_depth: int = 5, min_percent: float = 1.0) -> list[dict]:
        """Look-through ownership: effective % = product of stakes along each chain."""
        max_depth = max(1, min(int(max_depth), 8))
        return self.run(
            f"""
            MATCH p = (owner:Entity)-[:OWNS*1..{max_depth}]->(c:Company {{ticker: $ticker}})
            WHERE all(r IN relationships(p) WHERE r.percent IS NOT NULL)
              AND NOT (owner)<-[:OWNS]-(:Entity)
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
        """People who hold positions in (or own) several listed companies."""
        return self.run(
            """
            MATCH (p:Person)-[r:HOLDS_POSITION|OWNS]->(c:Company)
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
            MATCH (h:Entity)-[o:OWNS]->(c:Company)
            WITH h, collect({ticker: c.ticker, name: c.name, key: c.key, percent: o.percent}) AS stakes
            WHERE size(stakes) >= $min
            RETURN h.key AS key, h.name AS name, [l IN labels(h) WHERE l <> 'Entity'][0] AS type,
                   size(stakes) AS companies, stakes
            ORDER BY companies DESC, name LIMIT $limit
            """,
            min=min_companies, limit=limit,
        )

    def board_overlap(self, limit: int = 100) -> list[dict]:
        """Pairs of companies sharing board members / executives."""
        return self.run(
            """
            MATCH (a:Company)<-[:HOLDS_POSITION]-(p:Person)-[:HOLDS_POSITION]->(b:Company)
            WHERE a.ticker < b.ticker
            RETURN a.ticker AS company_a, b.ticker AS company_b,
                   collect(DISTINCT p.name) AS shared_people, count(DISTINCT p) AS n
            ORDER BY n DESC LIMIT $limit
            """,
            limit=limit,
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
