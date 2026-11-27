"""Possible family links from registry patronymics (issue #32). Hypotheses only.

A full registry name carries the father's name: "Батын Батболд" is Бат's son Батболд. Two
people with the same patronymic *may* be siblings, but a shared patronymic is weak evidence
(common given names make many unrelated people share one), so this module only ever
*proposes* pairs for a reviewer. Nothing here is a finding, and nothing unconfirmed reaches
a graph edge, the conflict checks or a company's flags.

Candidates (`candidates`)
-------------------------
Two different Person nodes whose **full** registry names have the same patronymic (the father's
name folded: never from MSE's initials, "Б.Батболд" says nothing about the father) **and** a shared
context:

``shared_companies``  companies both are linked to (posts, stakes or beneficial ownership, now or before)
``linked_companies``  pairs of their companies where one owns the other (current, real stakes)
``same_group``        the same detected group (#28, `group_id`)
``same_clan``         the same clan name (ургийн овог), when both have one

Pairs whose clan names differ are left out: children of one father share his clan name.

``score`` = rarity x strength, 0 to 1. ``rarity`` = 1 / (people with that patronymic - 1), so a
patronymic only these two have scores 1 and one shared by eleven people 0.1. ``strength`` =
1 - 0.5 ** evidence, with evidence = shared companies + 0.5 x linked company pairs + 0.25 for the
same group + 0.5 for the same clan. Pairs under MIN_SCORE are not listed.

Public (owner, 2026-11-27)
--------------------------
The owner decided that the hypotheses need no password: `/api/review/relations` serves them to
anyone, a person's panel lists theirs (`possible_relatives`) and a company's panel the pairs among
its people (`possible_family`), always marked as unconfirmed guesses. They still never become a
graph edge, a conflict row or a company flag until confirmed. Confirming (or dismissing) still
needs the review password (`/api/review/decide`), because it writes to the graph; without one, a
session confirms a link by adding a row with its public source to ``data/overrides/relations.csv``.
A dismissed candidate (`related:<a>|<b>`) stays a `(:Decision)` in the graph and is not exported
to `data/overrides/reviewed.csv`.

Confirmed links
---------------
A reviewer confirms a pair with its basis (BASES) and a **source** (required: where the link is
documented). The decision is a `(:Decision {type: 'relation'})` in the graph (kept by wipes);
`cli review-export` writes it into ``data/overrides/relations.csv`` (header HEADER), which then
becomes public with the deploy mirror: confirm only links with a public source. File rows and graph
decisions combine per pair, the later `decided_at` winning (the graph's on a tie); basis ``none``
says "not related" and removes a link the file has.

The links in force become ``(:Person)-[:RELATED_TO {basis, source, note, decided_by, decided_at}]->(:Person)``
edges (`apply`, on every start and after every load and decision). `analysis.related_parties` reads them.
"""
from __future__ import annotations

import csv
import io
import logging
import os
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from .holdings import real
from .normalize import clean, patronymic_stem

log = logging.getLogger(__name__)

RELATIONS_FILE = "data/overrides/relations.csv"
HEADER = ["a", "b", "basis", "source", "decided_by", "decided_at", "note"]
BASES = ("sibling", "parent_child", "spouse", "relative", "none")
MIN_SCORE = 0.05
ITEM_PREFIX = "related:"
COMMENT = ("# Confirmed family links between people (issue #32, mse_graph/relations.py). Each becomes a\n"
           "# RELATED_TO edge used by the related-party check. THIS FILE IS PUBLIC (deploy mirror):\n"
           "# only links documented by a public source. a, b: person keys. basis: sibling | parent_child |\n"
           "# spouse | relative | none (none = not related: removes a link). source: where it is documented.\n"
           "# Written by `python -m mse_graph.cli review-export`.\n")


@dataclass(frozen=True)
class Relation:
    a: str
    b: str
    basis: str
    source: str = ""
    decided_by: str = ""
    decided_at: str = ""
    note: str = ""

    @property
    def pair(self) -> tuple[str, str]:
        return pair(self.a, self.b)


def pair(a: str, b: str) -> tuple[str, str]:
    a, b = (a or "").strip(), (b or "").strip()
    return (a, b) if a <= b else (b, a)


def item_id(a: str, b: str) -> str:
    return ITEM_PREFIX + "|".join(pair(a, b))


def path(p=None) -> Path:
    return Path(p or os.getenv("RELATIONS") or RELATIONS_FILE)


def read_file(p=None) -> list[Relation]:
    f = path(p)
    if not f.is_file():
        return []
    lines = [ln for ln in f.read_text(encoding="utf-8-sig").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    out = []
    for r in csv.DictReader(lines):
        row = {k: clean(r.get(k)) for k in HEADER}
        if not (row["a"] and row["b"]) or row["a"] == row["b"]:
            continue
        if row["basis"] not in BASES:
            log.warning("relations file: %s / %s: unknown basis %r, skipped", row["a"], row["b"], row["basis"])
            continue
        out.append(Relation(**row))
    return out


def db_relations(g) -> list[Relation]:
    return [Relation(a=d["a"], b=d["b"], basis=d.get("basis") or "relative", source=d.get("source") or "",
                     decided_by=d.get("decided_by") or "review", decided_at=d.get("decided_at") or "",
                     note=d.get("note") or "")
            for d in (r["d"] for r in g.run("""MATCH (d:Decision {type: 'relation'})
                                               RETURN d {.*} AS d ORDER BY d.created_at, d.id"""))]


def combine(file_rows: list[Relation], db_rows: list[Relation]) -> list[Relation]:
    """One row per pair: the later `decided_at` wins, the graph's on a tie (as review.combine)."""
    best: dict[tuple, tuple] = {}
    for rank, (r, from_db) in enumerate([(r, False) for r in file_rows] + [(r, True) for r in db_rows]):
        score = (r.decided_at[:10], from_db, rank)
        if r.pair not in best or score > best[r.pair][0]:
            best[r.pair] = (score, r)
    return sorted((r for _, r in best.values()), key=lambda r: r.pair)


def decided(g, p=None) -> list[Relation]:
    """Every pair decided, `none` rows included (they keep a pair out of the candidates)."""
    return combine(read_file(p), db_relations(g))


def in_force(g, p=None) -> list[Relation]:
    return [r for r in decided(g, p) if r.basis != "none"]


def apply(g, p=None) -> dict:
    """Rebuild the RELATED_TO edges from the links in force. Keys of people merged since resolve
    (former_keys); a link whose people aren't both in the graph waits (logged)."""
    rows, missing = [], []
    for r in in_force(g, p):
        a, b = g.resolve_key(r.a), g.resolve_key(r.b)
        found = {x["k"] for x in g.run("MATCH (p:Person) WHERE p.key IN $ks RETURN p.key AS k", ks=[a, b])}
        if a == b or not {a, b} <= found:
            missing.append(f"{r.a}|{r.b}")
            continue
        a, b = pair(a, b)
        rows.append({"a": a, "b": b, "basis": r.basis, "source": r.source, "note": r.note,
                     "decided_by": r.decided_by or "relations file", "decided_at": r.decided_at})
    g.run("MATCH ()-[x:RELATED_TO]->() DELETE x")
    if rows:
        g.run("""UNWIND $rows AS r
                 MATCH (a:Person {key: r.a}), (b:Person {key: r.b})
                 MERGE (a)-[x:RELATED_TO]->(b)
                 SET x.basis = r.basis, x.source = r.source, x.note = r.note,
                     x.decided_by = r.decided_by, x.decided_at = r.decided_at""", rows=rows)
    if missing:
        log.info("family links waiting for their people: %s", ", ".join(missing))
    return {"relations": len(rows), "waiting": len(missing)}


def relatives(g, key: str) -> list[dict]:
    """A person's confirmed relatives (public: only confirmed links are edges)."""
    return g.run("""MATCH (p:Person {key: $k})-[x:RELATED_TO]-(q:Person)
                    RETURN q.key AS key, q.name AS name, x.basis AS basis, x.source AS source,
                           x.note AS note, x.decided_at AS decided_at ORDER BY q.name""", k=key)


# ---- candidates ----------------------------------------------------------------------

def _fold(s: str) -> str:
    from .review import fold
    return fold(s)


def candidates(g, min_score: float = MIN_SCORE, limit: int = 200, reviewed: set[str] | None = None) -> list[dict]:
    """Pairs of people who may be related (module docstring), best first, without the pairs
    already decided or dismissed."""
    people = g.run(
        """
        MATCH (p:Person) WHERE NOT p:Company AND p.patronymic IS NOT NULL AND p.given_name IS NOT NULL
          AND coalesce(p.name_form, 'full') = 'full'
        OPTIONAL MATCH (p)-[:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER]->(c:Company)
        RETURN p.key AS key, p.name AS name, p.full_name AS full_name, p.patronymic AS patronymic,
               p.clan_name AS clan, p.group_id AS group_id,
               collect(DISTINCT {key: c.key, ticker: c.ticker, name: c.name}) AS companies
        """)
    by_stem: dict[str, list[dict]] = {}
    for p in people:
        stem = _fold(patronymic_stem(p["patronymic"]))
        if len(stem) < 2:
            continue
        p["companies"] = [c for c in p["companies"] if c.get("key")]
        p["_cos"] = {c["key"] for c in p["companies"]}
        by_stem.setdefault(stem, []).append(p)
    by_stem = {s: ps for s, ps in by_stem.items() if len(ps) >= 2}
    if not by_stem:
        return []
    owns: set[tuple[str, str]] = {
        (r["a"], r["b"]) for r in g.run(
            f"""MATCH (a:Company)-[o:OWNS {{current: true}}]->(b:Company) WHERE a <> b AND {real("o")}
                RETURN a.key AS a, b.key AS b""")}
    names = {c["key"]: (c["ticker"] if c["ticker"] and not c["ticker"].startswith("~") else c["name"])
             for ps in by_stem.values() for p in ps for c in p["companies"]}
    skip = {r.pair for r in decided(g)}
    if reviewed is None:
        from .review import reviewed_items
        reviewed = reviewed_items(g)

    out = []
    for stem, ps in by_stem.items():
        rarity = 1.0 / (len(ps) - 1)
        for p, q in combinations(sorted(ps, key=lambda n: n["key"]), 2):   # a and b in key order
            if p["key"].split("#", 1)[0] == q["key"].split("#", 1)[0]:
                continue        # namesakes split on purpose: the same name, no new evidence
            if pair(p["key"], q["key"]) in skip or item_id(p["key"], q["key"]) in reviewed:
                continue
            cp, cq = _fold(p["clan"] or ""), _fold(q["clan"] or "")
            if cp and cq and cp != cq:
                continue        # another clan: another father with the same name
            shared = sorted(p["_cos"] & q["_cos"])
            linked = sorted({(x, y) for x in p["_cos"] for y in q["_cos"]
                             if x != y and ((x, y) in owns or (y, x) in owns)})
            group = bool(p["group_id"] is not None and p["group_id"] == q["group_id"])
            if not (shared or linked or group):
                continue        # a shared patronymic alone is no evidence
            clan = bool(cp and cp == cq)
            evidence = len(shared) + 0.5 * len(linked) + 0.25 * group + 0.5 * clan
            score = round(rarity * (1 - 0.5 ** evidence), 3)
            if score < min_score:
                continue
            out.append({
                "item": item_id(p["key"], q["key"]), "hypothesis": True, "score": score,
                "patronymic": p["patronymic"], "people_with_patronymic": len(ps), "rarity": round(rarity, 3),
                "a": _brief(p), "b": _brief(q),
                "shared_companies": [names.get(k, k) for k in shared],
                "linked_companies": [[names.get(x, x), names.get(y, y)] for x, y in linked],
                "same_group": group, "same_clan": clan,
                "evidence": [e for e, on in (("shared_companies", shared), ("linked_companies", linked),
                                             ("same_group", group), ("same_clan", clan)) if on]})
    out.sort(key=lambda x: (-x["score"], x["a"]["name"] or "", x["b"]["name"] or ""))
    return out[:limit]


def _brief(p: dict) -> dict:
    return {"key": p["key"], "name": p["full_name"] or p["name"],
            "companies": sorted(c["ticker"] if c["ticker"] and not c["ticker"].startswith("~") else c["name"]
                                for c in p["companies"])}


def confirmed(g) -> list[dict]:
    """The links in force, with where each one comes from (file or review decision)."""
    file_pairs = {r.pair: r for r in read_file()}
    db = {r.pair: r for r in db_relations(g)}
    rows = in_force(g)
    keys = sorted({k for r in rows for k in r.pair})
    names = {x["k"]: x["n"] for x in g.run("""UNWIND $ks AS k MATCH (p:Person)
                                              WHERE p.key = k OR k IN coalesce(p.former_keys, [])
                                              RETURN k, coalesce(p.full_name, p.name) AS n""", ks=keys)}
    out = []
    for r in rows:
        src = "review" if db.get(r.pair) == r else "file"
        out.append({"item": item_id(r.a, r.b), "a": r.a, "b": r.b, "a_name": names.get(r.a), "b_name": names.get(r.b),
                    "basis": r.basis, "source": r.source,
                    "note": r.note, "decided_by": r.decided_by, "decided_at": r.decided_at, "from": src,
                    "in_file": r.pair in file_pairs})
    return out


def cached_candidates(g) -> list[dict]:
    """All candidates at MIN_SCORE, shared between requests for a while (memo.py). Read-only."""
    from . import memo
    return memo.cached(g, "relations.candidates", candidates, MIN_SCORE, 2000)


def _other(c: dict, key: str) -> dict | None:
    if c["a"]["key"] == key:
        return c["b"]
    if c["b"]["key"] == key:
        return c["a"]
    return None


def possible_for_person(g, key: str) -> list[dict]:
    """A person's possible relatives (unconfirmed hypotheses), best first."""
    out = []
    for c in cached_candidates(g):
        o = _other(c, key)
        if o:
            out.append({"key": o["key"], "name": o["name"], "companies": o["companies"], "score": c["score"],
                        "patronymic": c["patronymic"], "evidence": c["evidence"],
                        "shared_companies": c["shared_companies"], "linked_companies": c["linked_companies"],
                        "item": c["item"], "hypothesis": True})
    return out


def possible_for_company(g, ticker: str | None, name: str | None = None) -> list[dict]:
    """Pairs of possible relatives where at least one of them is tied to the company (unconfirmed)."""
    ids = {x for x in (ticker, name) if x}
    if not ids:
        return []
    return [c for c in cached_candidates(g) if ids & (set(c["a"]["companies"]) | set(c["b"]["companies"]))]


def review(g, min_score: float = MIN_SCORE, limit: int = 200) -> dict:
    """What `/api/review/relations` serves (public since 2026-11-27: hypotheses, marked as such)."""
    cands = candidates(g, min_score, limit)
    conf = confirmed(g)
    decisions = [r["d"] for r in g.run(
        """MATCH (d:Decision) WHERE d.type = 'relation' OR (d.type = 'reviewed' AND d.item STARTS WITH $p)
           RETURN d {.*} AS d ORDER BY d.created_at DESC, d.id""", p=ITEM_PREFIX)]
    return {"candidates": cands, "confirmed": conf, "decisions": decisions,
            "counts": {"candidates": len(cands), "confirmed": len(conf)},
            "note": ("Hypotheses only: a shared patronymic is weak evidence. Not a graph edge, conflict or flag "
                     "until confirmed with a public source.")}


def summary(g) -> dict:
    return {"candidates": len(candidates(g)), "confirmed": len(in_force(g)), "pending_export": pending(g)}


# ---- decisions -------------------------------------------------------------------------

def decision_rows(g, a: str, b: str, basis: str, source: str, by: str, note: str, now: str, group: str,
                  make_id) -> list[dict]:
    """The (:Decision) rows of confirming (or, basis `none`, denying) a link between `a` and `b`."""
    from .review import ReviewError

    basis = (basis or "").strip() or "relative"
    if basis not in BASES:
        raise ReviewError(f"basis must be one of {', '.join(BASES)}")
    keys = []
    for k in (a, b):
        rk = g.resolve_key(k)
        if not g.run("MATCH (p:Person {key: $k}) RETURN 1 AS x", k=rk):
            raise ReviewError(f"no person {k}")
        keys.append(rk)
    if keys[0] == keys[1]:
        raise ReviewError("a person is not their own relative")
    source = clean(source)
    if basis != "none" and not source:
        raise ReviewError("a family link needs its source (where it is documented)")
    a, b = pair(*keys)
    return [{"id": make_id("relation", a, b), "type": "relation", "group": group, "action": "relate",
             "a": a, "b": b, "basis": basis, "source": source, "item": item_id(a, b), "decided_by": by,
             "decided_at": now[:10], "note": note, "created_at": now}]


# ---- into git ------------------------------------------------------------------------

def to_csv(rows: list[Relation]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(HEADER)
    for r in rows:
        w.writerow([r.a, r.b, r.basis, r.source, r.decided_by, r.decided_at, r.note])
    return buf.getvalue()


def pending(g, p=None) -> int:
    """Graph decisions the file doesn't hold yet."""
    have = {r.pair: r for r in read_file(p)}
    return sum(1 for r in db_relations(g) if have.get(r.pair) != r)


def export(g, p=None) -> dict:
    """Write the decided pairs (`none` rows too: they cancel older links) into the file."""
    f = path(p)
    added = pending(g, p)
    rows = decided(g, p)
    f.parent.mkdir(parents=True, exist_ok=True)
    head = ""
    if f.is_file():
        for ln in f.read_text(encoding="utf-8-sig").splitlines():
            if not ln.lstrip().startswith("#"):
                break
            head += ln + "\n"
    f.write_text((head or COMMENT) + to_csv(rows), encoding="utf-8")
    return {"relations_file": str(f), "relations_rows": len(rows), "relations_added": added}
