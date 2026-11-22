"""Group and control analysis (issue #28): who controls what, group maps, detected groups.

`Graph.ultimate_owners` answers "who owns X" (look-through: the product of the stakes
along each chain). This module turns it around and asks "what does X control".

    control       An owner controls a company when the stakes held by the owner and by the
                  companies it already controls add up to more than 50% (CONTROL_MIN). A
                  chain of majorities (60% of A, A 60% of B) is control of B, although the
                  economic look-through is only 36%; two controlled companies with 30% each
                  also give control. Repeated until nothing changes, so loops can't recurse.
    influence     The weaker level: the owner's block (its own stake plus those of the
                  companies it controls or influences) is the largest holding in a company
                  and at least INFLUENCE_MIN (20%), the usual threshold for significant
                  influence, in a company no one outside the block controls. Ties count
                  for every tied block.
    groups        One group per top owner (an owner no one else controls): every company it
                  controls or influences, the chain that gives it, the look-through %, and
                  the number of listed companies. /api/analysis/groups, `cli groups`, the UI.
    communities   Automatic group detection: Louvain community detection (networkx) on the
                  combined graph of current edges: OWNS weighted by percent (a registry row
                  without a percent counts as a fixed mid-size tie), BENEFICIAL_OWNER and
                  HOLDS_POSITION as weaker ties. Aura Free has no GDS plugin, so the edges are
                  read into Python and the result is written back as `group_id` on the
                  nodes (`write_communities`, after every pipeline run and the daily job).
    curated_score How many of the companies of each hand-curated group (data/manual/*,
                  data/mcs/graph) land in one detected community, so the weights can be tuned
                  before the method's other groups are trusted.

Only current, ownership stakes count (holdings.REAL): a custodian holds for its clients and
a company's treasury shares are no one's (issue #39). Only stakes with a percent count for
control and influence; a registry row without a percent never does, though it does tie
companies together for the community detection. Results depend on the entity resolution
(issue #6): a false person merge joins two unrelated groups.
"""
from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import holdings as hold

CONTROL_MIN = 50.0          # more than this is control
INFLUENCE_MIN = 20.0        # at least this, and the largest block, is significant influence
MAX_DEPTH = 6               # look-through chains, as Graph.ultimate_owners (5) plus one

# Community detection: edge weights, Louvain resolution and seed (fixed: same graph, same groups)
W_OWNS_UNKNOWN = 2.0        # a stake without a percent (registry rows)
W_BENEFICIAL = 1.5
W_POSITION = 0.5
RESOLUTION = 1.0
SEED = 28
MIN_COMMUNITY = 3           # smaller communities, or ones with fewer than 2 companies, are no group

_TYPE = "[l IN labels({0}) WHERE l IN ['Company', 'Person', 'Organization']][0]"

_NODES = f"""
MATCH (e:Entity) WHERE EXISTS {{ (e)-[:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER {{current: true}}]-(:Entity) }}
RETURN e.key AS key, e.name AS name, e.ticker AS ticker, {_TYPE.format("e")} AS type,
       CASE WHEN e:Company THEN coalesce(e.listed, true) ELSE false END AS listed,
       e.register_no AS register_no
"""

_EDGES = """
MATCH (a:Entity)-[r:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER {current: true}]->(b:Entity)
WHERE a <> b
RETURN a.key AS source, b.key AS target, type(r) AS rel, r.percent AS percent,
       coalesce(r.holding_type, 'direct') AS holding_type
"""


def owns_weight(percent) -> float:
    """A stake's weight in the community graph: 1 (a few percent) to 5 (all of it)."""
    if percent is None:
        return W_OWNS_UNKNOWN
    return 1.0 + 4.0 * max(0.0, min(float(percent), 100.0)) / 100.0


class Network:
    """The current edges of the graph, read once: nodes, ownership stakes and all ties."""

    def __init__(self, nodes: list[dict], edges: list[dict]):
        self.nodes = {n["key"]: n for n in nodes}
        self.edges = edges
        self.owned: dict[str, list[tuple[str, float]]] = defaultdict(list)    # holder -> [(company, %)]
        self.holders: dict[str, list[tuple[str, float]]] = defaultdict(list)  # company -> [(holder, %)]
        for e in edges:
            if (e["rel"] == "OWNS" and e["percent"] is not None
                    and e["holding_type"] in ("direct", "unknown") and e["source"] != e["target"]):
                self.owned[e["source"]].append((e["target"], float(e["percent"])))
                self.holders[e["target"]].append((e["source"], float(e["percent"])))

        self._controllers: dict[str, set[str]] | None = None

    @classmethod
    def read(cls, g) -> "Network":
        return cls(g.run(_NODES), g.run(_EDGES))

    def label(self, key: str) -> str:
        n = self.nodes.get(key) or {}
        t = n.get("ticker") or ""
        return t if t and not t.startswith("~") else (n.get("name") or key)

    def node(self, key: str) -> dict:
        n = self.nodes.get(key) or {"key": key}
        return {"key": key, "name": n.get("name"), "ticker": n.get("ticker"), "type": n.get("type"),
                "listed": bool(n.get("listed"))}

    # ---- control and influence -----------------------------------------------------

    def controllers(self) -> dict[str, set[str]]:
        """company -> the owners that control it (control only, every holder once)."""
        if self._controllers is None:
            self._controllers = defaultdict(set)
            for owner in list(self.owned):
                for c, (lv, *_r) in self.control(owner, influence=False)[0].items():
                    self._controllers[c].add(owner)
        return self._controllers

    def control(self, owner: str, influence: bool = True) -> tuple[dict, dict]:
        """({company: (level, block %, [(member, %)...])}, parent) for one owner.

        `level` is "control" or "influence"; the members are the holders inside the
        owner's block whose stakes make up `block %`. `parent` maps each company to the
        block member with the largest stake in it, for the chain."""
        level: dict[str, str] = {owner: "self"}
        detail: dict[str, bool] = {}
        parent: dict[str, str] = {}

        def blocks(members) -> dict[str, list[tuple[str, float]]]:
            out = defaultdict(list)
            for m in members:
                for c, p in self.owned.get(m, ()):
                    if c not in level:
                        out[c].append((m, p))
            return out

        def admit(c, lv, parts):
            level[c] = lv
            detail[c] = True
            parent[c] = max(parts, key=lambda x: (x[1], x[0]))[0]

        # control: more than 50% held by the owner and what it controls, until nothing changes
        while True:
            ctl = [k for k, v in level.items() if v in ("self", "control")]
            new = [(c, parts) for c, parts in blocks(ctl).items() if sum(p for _, p in parts) > CONTROL_MIN]
            if not new:
                break
            for c, parts in new:
                admit(c, "control", parts)
        # influence: the largest block and at least 20%, through controlled and influenced companies,
        # in a company no one outside the block controls
        bosses = self.controllers() if influence else {}
        while influence:
            new = []
            for c, parts in blocks(list(level)).items():
                mine = sum(p for _, p in parts)
                if mine < INFLUENCE_MIN:
                    continue
                if any(o not in level for o in bosses.get(c, ())):
                    continue
                inside = {m for m, _ in parts}
                others = [p for h, p in self.holders.get(c, ()) if h not in inside and h not in level]
                if mine >= max(others, default=0.0):
                    new.append((c, parts))
            if not new:
                break
            for c, parts in new:
                admit(c, "influence", parts)
        # the final blocks: a company admitted early may also be held by one admitted later
        ctl = {k for k, v in level.items() if v in ("self", "control")}
        out = {}
        for c in detail:
            inside = ctl if level[c] == "control" else set(level)
            parts = sorted(((h, p) for h, p in self.holders.get(c, ()) if h in inside), key=lambda x: (-x[1], x[0]))
            out[c] = (level[c], round(sum(p for _, p in parts), 4), parts)
        return out, parent

    def chain(self, owner: str, company: str, parent: dict) -> list[str]:
        path, seen = [company], {company}
        while path[-1] != owner:
            nxt = parent.get(path[-1])
            if nxt is None or nxt in seen:
                break
            path.append(nxt)
            seen.add(nxt)
        return path[::-1]

    def lookthrough(self, owner: str, max_depth: int = MAX_DEPTH) -> dict[str, float]:
        """Look-through % of every company the owner reaches: the sum over simple chains of
        the product of the stakes (as Graph.ultimate_owners)."""
        out: dict[str, float] = defaultdict(float)

        def walk(node, frac, seen, depth):
            if depth >= max_depth:
                return
            for c, p in self.owned.get(node, ()):
                if c in seen:
                    continue
                f = frac * p / 100.0
                out[c] += f * 100.0
                walk(c, f, seen | {c}, depth + 1)

        walk(owner, 1.0, {owner}, 0)
        return out


def _group(net: Network, owner: str, members: dict, parent: dict) -> dict:
    eff = net.lookthrough(owner)
    companies = []
    for c, (lv, block, parts) in members.items():
        path = net.chain(owner, c, parent)
        percents = []
        for a, b in zip(path, path[1:]):
            percents.append(next((p for x, p in net.owned.get(a, ()) if x == b), None))
        companies.append({**net.node(c), "level": lv, "block_percent": block,
                          "lookthrough_percent": round(eff.get(c, 0.0), 3),
                          "chain": [net.label(k) for k in path], "chain_keys": path, "chain_percents": percents,
                          "via": [{"key": m, "name": net.label(m), "percent": p} for m, p in parts]})
    companies.sort(key=lambda x: (x["level"] != "control", not x["listed"], -x["lookthrough_percent"],
                                  x["ticker"] or x["name"] or ""))
    return {**net.node(owner),
            "companies": companies,
            "controlled": sum(1 for x in companies if x["level"] == "control"),
            "influenced": sum(1 for x in companies if x["level"] == "influence"),
            "listed_companies": sum(1 for x in companies if x["listed"]),
            "listed_controlled": sum(1 for x in companies if x["listed"] and x["level"] == "control")}


def control_groups(g=None, net: Network | None = None, min_companies: int = 1,
                   listed_only: bool = False) -> list[dict]:
    """One group per top owner: the companies it controls or influences, listed first.

    A top owner is a holder no other holder controls. Owners that control each other
    (a loop of majorities) are one group, under the smallest key."""
    net = net or Network.read(g)
    sets = {}
    for owner in net.owned:
        members, parent = net.control(owner)
        if members:
            sets[owner] = (members, parent)
    controlled_by: dict[str, set[str]] = defaultdict(set)
    for owner, (members, _p) in sets.items():
        for c, (lv, *_rest) in members.items():
            if lv == "control":
                controlled_by[c].add(owner)
    def controls(a, b):
        return b in sets.get(a, ({},))[0] and sets[a][0][b][0] == "control"

    out = []
    for owner, (members, parent) in sets.items():
        # someone controls this owner, and it doesn't control them back (or comes first in their loop)
        if any(b != owner and (not controls(owner, b) or b < owner) for b in controlled_by.get(owner, ())):
            continue
        grp = _group(net, owner, members, parent)
        if len(grp["companies"]) >= min_companies and (not listed_only or grp["listed_companies"]):
            out.append(grp)
    out.sort(key=lambda x: (-x["listed_controlled"], -x["listed_companies"], -x["controlled"], -len(x["companies"]),
                            x["name"] or ""))
    return out


def group_of(g, key: str) -> dict | None:
    """The control group of one owner (any holder, also one that is itself controlled)."""
    net = Network.read(g)
    key = g.resolve_key(key)
    members, parent = net.control(key)
    return _group(net, key, members, parent) if key in net.nodes else None


# ---- community detection ---------------------------------------------------------

def community_graph(net: Network):
    import networkx as nx

    G = nx.Graph()
    G.add_nodes_from(net.nodes)
    for e in net.edges:
        a, b = e["source"], e["target"]
        if a == b or a not in net.nodes or b not in net.nodes:
            continue
        if e["rel"] == "OWNS":
            if e["holding_type"] not in ("direct", "unknown"):
                continue        # a custodian would tie its clients' companies together
            w = owns_weight(e["percent"])
        elif e["rel"] == "BENEFICIAL_OWNER":
            w = W_BENEFICIAL
        else:
            w = W_POSITION
        if G.has_edge(a, b):
            G[a][b]["weight"] += w
        else:
            G.add_edge(a, b, weight=w)
    return G


def detect(g=None, net: Network | None = None, resolution: float = RESOLUTION, seed: int = SEED) -> list[dict]:
    """Detected groups, largest first: {id, anchor, size, companies, listed, members}.

    `id` is `grp:` + the key of the anchor, the company with the largest weighted degree in
    it (a listed one if any), so it stays the same while the group keeps its core."""
    import networkx as nx

    net = net or Network.read(g)
    G = community_graph(net)
    if not G.number_of_edges():
        return []
    found = nx.community.louvain_communities(G, weight="weight", resolution=resolution, seed=seed)
    out = []
    for nodes in found:
        cos = [k for k in nodes if net.nodes[k].get("type") == "Company"]
        if len(nodes) < MIN_COMMUNITY or len(cos) < 2:
            continue
        deg = {k: G.degree(k, weight="weight") for k in nodes}
        anchor = max(cos, key=lambda k: (bool(net.nodes[k].get("listed")), deg[k], k))
        members = sorted((net.node(k) | {"degree": round(deg[k], 2)} for k in nodes),
                         key=lambda m: (m["type"] != "Company", not m["listed"], -m["degree"], m["key"]))
        out.append({"id": "grp:" + anchor, "anchor": net.node(anchor), "size": len(nodes),
                    "companies": len(cos), "listed": sum(1 for k in cos if net.nodes[k].get("listed")),
                    "people": sum(1 for k in nodes if net.nodes[k].get("type") == "Person"),
                    "members": members})
    out.sort(key=lambda c: (-c["listed"], -c["companies"], -c["size"], c["id"]))
    return out


def write_communities(g, found: list[dict] | None = None) -> dict:
    """Detect the groups and write `group_id` onto their nodes (and remove stale ones)."""
    found = detect(g) if found is None else found
    rows = [{"key": m["key"], "g": c["id"]} for c in found for m in c["members"]]
    g.run("MATCH (e:Entity) WHERE e.group_id IS NOT NULL AND NOT e.key IN $keys REMOVE e.group_id",
          keys=[r["key"] for r in rows])
    for i in range(0, len(rows), 1000):
        g.run("UNWIND $rows AS r MATCH (e:Entity {key: r.key}) SET e.group_id = r.g", rows=rows[i:i + 1000])
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    g.run("MERGE (m:Meta {key: 'groups'}) SET m.at = $at, m.communities = $n, m.nodes = $nodes",
          at=at, n=len(found), nodes=len(rows))
    return {"communities": len(found), "nodes": len(rows), "at": at,
            "listed_in_groups": sum(c["listed"] for c in found)}


def stored(g) -> list[dict] | None:
    """The groups as last written (`group_id` on the nodes), in `detect`'s form, or None."""
    rows = g.run(f"""
        MATCH (e:Entity) WHERE e.group_id IS NOT NULL
        RETURN e.group_id AS id, e.key AS key, e.name AS name, e.ticker AS ticker, {_TYPE.format("e")} AS type,
               CASE WHEN e:Company THEN coalesce(e.listed, true) ELSE false END AS listed,
               COUNT {{ (e)-[:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER {{current: true}}]-(:Entity) }} AS degree""")
    if not rows:
        return None
    by: dict[str, list] = defaultdict(list)
    for r in rows:
        by[r.pop("id")].append(r)
    out = []
    for gid, members in by.items():
        members.sort(key=lambda m: (m["type"] != "Company", not m["listed"], -m["degree"], m["key"]))
        anchor = next((m for m in members if m["key"] == gid[len("grp:"):]), members[0])
        cos = [m for m in members if m["type"] == "Company"]
        out.append({"id": gid, "anchor": {k: anchor[k] for k in ("key", "name", "ticker", "type", "listed")},
                    "size": len(members), "companies": len(cos), "listed": sum(1 for m in cos if m["listed"]),
                    "people": sum(1 for m in members if m["type"] == "Person"), "members": members})
    out.sort(key=lambda c: (-c["listed"], -c["companies"], -c["size"], c["id"]))
    return out


def communities(g, fresh: bool = False) -> dict:
    """The detected groups: the stored ones (`group_id`), else computed now (not written)."""
    found = None if fresh else stored(g)
    meta = g.run("MATCH (m:Meta {key: 'groups'}) RETURN m.at AS at")
    source = "stored" if found is not None else "computed"
    if found is None:
        found = detect(g)
    return {"communities": found, "source": source, "detected_at": meta[0]["at"] if meta and source == "stored" else None,
            "curated": curated_score(g, found)}


def community_subgraph(g, gid: str, limit: int = 600) -> dict:
    """Nodes and current edges of one detected group, for the UI."""
    rows = g.run(f"""
        MATCH (e:Entity {{group_id: $gid}})
        WITH collect(e) AS ns
        UNWIND ns AS a
        OPTIONAL MATCH (a)-[r:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER {{current: true}}]->(b:Entity) WHERE b IN ns
        WITH ns, collect(r)[..$limit] AS rels
        RETURN [n IN ns | n {{.key, .name, .ticker, type: {_TYPE.format("n")}}}] AS nodes,
               [r IN rels | {{source: startNode(r).key, target: endNode(r).key, type: type(r), percent: r.percent,
                             title: r.title, role: r.role, holding_type: r.holding_type}}] AS edges""", gid=gid,
                 limit=limit)
    if rows and rows[0]["nodes"]:
        return rows[0]
    # not written yet: compute
    found = next((c for c in detect(g) if c["id"] == gid), None)
    return subgraph(g, [m["key"] for m in found["members"]]) if found else {"nodes": [], "edges": []}


def subgraph(g, keys: list[str], limit: int = 600) -> dict:
    rows = g.run(f"""
        MATCH (e:Entity) WHERE e.key IN $keys
        WITH collect(e) AS ns
        UNWIND ns AS a
        OPTIONAL MATCH (a)-[r:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER {{current: true}}]->(b:Entity) WHERE b IN ns
        WITH ns, collect(r)[..$limit] AS rels
        RETURN [n IN ns | n {{.key, .name, .ticker, type: {_TYPE.format("n")}}}] AS nodes,
               [r IN rels | {{source: startNode(r).key, target: endNode(r).key, type: type(r), percent: r.percent,
                             title: r.title, role: r.role, holding_type: r.holding_type}}] AS edges""",
                 keys=keys, limit=limit)
    return rows[0] if rows else {"nodes": [], "edges": []}


def group_subgraph(g, key: str) -> dict:
    """A control group as a subgraph: the owner, its companies and the ownership edges between them."""
    grp = group_of(g, key)
    if not grp:
        return {"nodes": [], "edges": []}
    keys = {grp["key"]} | {k for c in grp["companies"] for k in c["chain_keys"]}
    sub = subgraph(g, sorted(keys))
    sub["edges"] = [e for e in sub["edges"] if e["type"] == "OWNS"]
    return sub


# ---- check against the hand-curated groups ------------------------------------------

def curated_dirs() -> list[Path]:
    """data/manual/* (or $MANUAL_DATA_DIR) and data/mcs/graph (or $MCS_DATA_DIR)."""
    from . import csv_io
    root = os.getenv("MANUAL_DATA_DIR") or csv_io.MANUAL_ROOT
    dirs = list(csv_io.manual_dirs(root))
    mcs = Path(os.getenv("MCS_DATA_DIR") or "data/mcs/graph")
    if (mcs / "companies.csv").is_file():
        dirs.append(mcs)
    return dirs


def curated_score(g, found: list[dict], dirs: list[Path] | None = None) -> list[dict]:
    """Per hand-curated dataset: of its companies the graph has, how many are in the one
    detected group that holds most of them (`score` = that share)."""
    from . import csv_io
    from .normalize import split_reg_numbers

    dirs = curated_dirs() if dirs is None else dirs
    if not dirs:
        return []
    tickers = g.company_keys()
    regs = {r["reg"]: r["key"] for r in g.run(
        "MATCH (c:Company) WHERE c.register_no IS NOT NULL RETURN c.register_no AS reg, c.key AS key")}
    where = {m["key"]: c["id"] for c in found for m in c["members"]}
    out = []
    for d in dirs:
        ds = csv_io.read_dir(d)
        keys = []
        for co in ds.companies:
            reg = split_reg_numbers(co.reg_no)[0] if co.reg_no else ""
            k = tickers.get(co.ticker.upper()) or tickers.get(co.ticker) or regs.get(reg)
            if not k and co.ticker.startswith("~"):
                k = regs.get(co.ticker[1:])
            if k:
                keys.append(k)
        keys = sorted(set(keys))
        counts: dict[str, int] = defaultdict(int)
        for k in keys:
            if k in where:
                counts[where[k]] += 1
        best = max(counts.items(), key=lambda x: (x[1], x[0]), default=(None, 0))
        out.append({"dataset": d.name if d.name != "graph" else d.parent.name, "companies": len(ds.companies),
                    "in_graph": len(keys), "in_groups": sum(counts.values()), "group": best[0],
                    "in_main_group": best[1], "groups": len(counts),
                    "score": round(best[1] / len(keys), 3) if keys else None})
    return out
