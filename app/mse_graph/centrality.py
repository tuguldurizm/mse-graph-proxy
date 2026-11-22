"""Network centrality (issue #29): who links separate parts of the market.

`Graph.interlocks` counts people with posts or stakes in two or more companies. That says
nothing about whether those companies were already close. These scores do:

    degree        The number of distinct companies a node is tied to (any current OWNS,
                  HOLDS_POSITION or BENEFICIAL_OWNER edge, either direction).
    betweenness   The broker score: the share of all pairs of companies whose shortest paths
                  run through the node (a path split over k equally short routes counts 1/k
                  for each). 0.1 means: one pair in ten is linked through it. Hops are not
                  weighted, since a board seat links two companies as surely as a stake does.
                  Endpoints never count for their own pairs.
    pagerank      Influence through ownership: PageRank on OWNS reversed (each company passes
                  its score to its owners, in proportion to their percent), so an owner scores
                  for what it owns and for what those companies own in turn. Percents are
                  absolute: the part of a company no recorded holder owns (the rest of the
                  free float, missing rows) leaks back to everyone evenly, as does the
                  damping. A stake without a percent (registry rows) counts as UNKNOWN_PERCENT;
                  a company whose stakes add up to more than 100% is scaled down to 100%.
                  Stored relative to the average node: 1 = average, 10 = ten times the average.

All three use the same edges as the group detection (groups.Network, read once): current edges
only, without nominee and treasury stakes (a custodian holding for clients in 40 companies would
otherwise be the market's top broker). Aura Free has no GDS plugin, so the scores are computed in
Python (networkx for betweenness) and written back as `centrality_degree`,
`centrality_betweenness`, `centrality_pagerank` and `centrality_at` on the nodes after every
pipeline run (`write`) and by the daily job.

A false person merge (issue #6) joins two unrelated boards through one node and makes it an
artificial broker, so the top people by betweenness double as a review list for #6.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from . import groups

KINDS = {"person": "Person", "company": "Company", "organization": "Organization"}
SCORES = ("betweenness", "pagerank", "degree")
ALPHA = 0.85                # PageRank damping
UNKNOWN_PERCENT = 25.0      # a stake without a percent, for PageRank (as groups.owns_weight: 2 of 1-5)
TOL = 1e-12
MAX_ITER = 500


def betweenness(net: groups.Network, G=None) -> dict[str, float]:
    """key -> share of company pairs whose shortest paths pass through it (0-1)."""
    import networkx as nx

    G = groups.community_graph(net) if G is None else G
    cos = [k for k in G if net.nodes[k].get("type") == "Company"]
    pairs = len(cos) * (len(cos) - 1) / 2
    if pairs <= 0:
        return {k: 0.0 for k in G}
    # betweenness_centrality_subset on an undirected graph counts each unordered pair once
    raw = nx.betweenness_centrality_subset(G, sources=cos, targets=cos, normalized=False, weight=None)
    return {k: v / pairs for k, v in raw.items()}


def stakes(net: groups.Network) -> dict[str, list[tuple[str, float]]]:
    """company -> [(holder, fraction)] of the ownership stakes PageRank follows."""
    by: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for e in net.edges:
        a, b = e["source"], e["target"]
        if (e["rel"] != "OWNS" or e["holding_type"] not in ("direct", "unknown") or a == b
                or a not in net.nodes or b not in net.nodes):
            continue
        pct = UNKNOWN_PERCENT if e["percent"] is None else max(0.0, min(float(e["percent"]), 100.0))
        by[b][a] += pct / 100.0
    out = {}
    for c, holders in by.items():
        total = sum(holders.values())
        scale = 1.0 / total if total > 1.0 else 1.0
        out[c] = sorted((h, f * scale) for h, f in holders.items() if f > 0)
    return out


def pagerank(net: groups.Network, alpha: float = ALPHA) -> dict[str, float]:
    """key -> PageRank on reversed ownership, relative to the average node (mean 1)."""
    keys = sorted(net.nodes)
    n = len(keys)
    if not n:
        return {}
    flow = stakes(net)
    x = dict.fromkeys(keys, 1.0 / n)
    for _ in range(MAX_ITER):
        nxt = dict.fromkeys(keys, 0.0)
        leak = 0.0
        for k in keys:
            given = 0.0
            for h, f in flow.get(k, ()):
                nxt[h] += alpha * x[k] * f
                given += f
            leak += alpha * x[k] * (1.0 - given)
        base = (1.0 - alpha) / n + leak / n
        err = 0.0
        for k in keys:
            nxt[k] += base
            err += abs(nxt[k] - x[k])
        x = nxt
        if err < n * TOL:
            break
    return {k: v * n for k, v in x.items()}


def degree(net: groups.Network, G=None) -> dict[str, int]:
    """key -> number of distinct companies it is tied to."""
    G = groups.community_graph(net) if G is None else G
    return {k: sum(1 for m in G[k] if net.nodes[m].get("type") == "Company") for k in G}


def compute(g=None, net: groups.Network | None = None) -> list[dict]:
    """Every node of the current graph with its three scores and ranks, by betweenness."""
    net = net or groups.Network.read(g)
    G = groups.community_graph(net)
    btw, pr, deg = betweenness(net, G), pagerank(net), degree(net, G)
    rows = [{**net.node(k), "degree": deg.get(k, 0), "betweenness": round(btw.get(k, 0.0), 6),
             "pagerank": round(pr.get(k, 0.0), 4)} for k in net.nodes]
    return _ranked(rows)


def _ranked(rows: list[dict]) -> list[dict]:
    """Add rank_<score> (1 = highest, ties share the rank of the first) and sort by betweenness."""
    for s in SCORES:
        prev, rank = None, 0
        for i, r in enumerate(sorted(rows, key=lambda r: (-(r[s] or 0), r["key"])), 1):
            if r[s] != prev:
                rank, prev = i, r[s]
            r["rank_" + s] = rank
    rows.sort(key=lambda r: (r["rank_betweenness"], r["rank_pagerank"], r["key"]))
    return rows


def write(g, rows: list[dict] | None = None) -> dict:
    """Write the scores onto the nodes (and remove them from nodes no longer in the graph)."""
    rows = compute(g) if rows is None else rows
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    keys = [r["key"] for r in rows]
    g.run("MATCH (e:Entity) WHERE e.centrality_at IS NOT NULL AND NOT e.key IN $keys "
          "REMOVE e.centrality_at, e.centrality_degree, e.centrality_betweenness, e.centrality_pagerank",
          keys=keys)
    data = [{"key": r["key"], "d": r["degree"], "b": r["betweenness"], "p": r["pagerank"]} for r in rows]
    for i in range(0, len(data), 1000):
        g.run("UNWIND $rows AS r MATCH (e:Entity {key: r.key}) SET e.centrality_degree = r.d, "
              "e.centrality_betweenness = r.b, e.centrality_pagerank = r.p, e.centrality_at = $at",
              rows=data[i:i + 1000], at=at)
    g.run("MERGE (m:Meta {key: 'centrality'}) SET m.at = $at, m.nodes = $n", at=at, n=len(rows))
    return {"nodes": len(rows), "at": at, "top_brokers": [_label(r) for r in top(rows, "betweenness", 5)],
            "top_pagerank": [_label(r) for r in top(rows, "pagerank", 5)]}


def _label(r: dict) -> str:
    t = r.get("ticker") or ""
    return t if t and not t.startswith("~") else (r.get("name") or r["key"])


def stored(g) -> list[dict] | None:
    """The scores as last written, in `compute`'s form, or None."""
    rows = g.run(f"""
        MATCH (e:Entity) WHERE e.centrality_at IS NOT NULL
        RETURN e.key AS key, e.name AS name, e.ticker AS ticker, {groups._TYPE.format("e")} AS type,
               CASE WHEN e:Company THEN coalesce(e.listed, true) ELSE false END AS listed,
               e.centrality_degree AS degree, e.centrality_betweenness AS betweenness,
               e.centrality_pagerank AS pagerank""")
    if not rows:
        return None
    for r in rows:
        r["listed"] = bool(r["listed"])
    return _ranked(rows)


def top(rows: list[dict], by: str = "betweenness", limit: int | None = 100, kind: str = "all") -> list[dict]:
    """The highest `by` scores of one kind of node (person / company / organization / all).
    Betweenness lists only nodes on at least one path."""
    if by not in SCORES:
        raise ValueError(f"unknown score {by!r}; one of {', '.join(SCORES)}")
    want = KINDS.get(kind)
    if kind != "all" and not want:
        raise ValueError(f"unknown kind {kind!r}; one of all, {', '.join(KINDS)}")
    out = [r for r in rows if (want is None or r["type"] == want) and (by != "betweenness" or r[by] > 0)]
    out.sort(key=lambda r: (r["rank_" + by], r["key"]))
    return out[:limit] if limit else out


def report(g, kind: str = "all", by: str = "betweenness", limit: int | None = 100, fresh: bool = False) -> dict:
    """The ranked list, from the stored scores (else computed now, not written)."""
    rows = None if fresh else stored(g)
    source = "stored" if rows is not None else "computed"
    if rows is None:
        rows = compute(g)
    meta = g.run("MATCH (m:Meta {key: 'centrality'}) RETURN m.at AS at") if source == "stored" else []
    items = top(rows, by, limit, kind)
    if kind in ("all", "person") and items:
        _mark_review(g, items)
    return {"items": items, "by": by, "kind": kind, "nodes": len(rows), "source": source,
            "computed_at": meta[0]["at"] if meta else None}


def _mark_review(g, items: list[dict]) -> None:
    """`review`: a person the review queue (issue #6) already lists as possibly several people."""
    try:
        from . import review
        sus = {r["key"] for r in review.suspicious_people(g)}
    except Exception:  # the review queue is a hint here, never a reason to fail
        return
    for r in items:
        if r["type"] == "Person":
            r["review"] = r["key"] in sus
