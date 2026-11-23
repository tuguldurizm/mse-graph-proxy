"""The entity-resolution review queue and the decisions made on it (issue #6).

Name matching merges people and organisations automatically (normalize.entity_key,
PeopleResolver). Every governance check relies on those merges, so a false merge
gives a false flag. This module finds what a person should look at and records what
they decide.

The queue (`queue`, served as /api/analysis/resolution)
--------------------------------------------------------
``ambiguous_people``     initial-form people a full registry name may be (Graph.ambiguous_people, #36)
``suspicious_people``    one node that may be several people: current positions at
                         MIN_COMPANIES or more companies (``many_companies``), current links to
                         companies in MIN_SECTORS or more sectors (``many_sectors``), or source rows
                         with different full names (``different_full_names``)
``possible_duplicates``  two keys that may be one entity: one letter apart (``one_letter``), the
                         same after folding Latin into Cyrillic and look-alike letters
                         (``transliteration``), or a lone given name next to a person with that
                         given name at the same company (``missing_initial``)

Each item has an ``item`` id ("ambiguous:<key>", "suspicious:<key>", "pair:<a>|<b>"); an item a
reviewer has dismissed (or decided) leaves the queue.

Decisions
---------
A decision is stored in the graph as a ``(:Decision)`` node, applied at once
(Graph.apply_overrides) and kept by ``load --wipe`` and ``rebuild-history``:

* ``type: 'identity'``: one identity-override row (records.Override: company, name, target,
  source, kind, decided_by, decided_at, note), plus ``before`` (the key the rows named) and
  ``group`` (the decision it belongs to). Graph.identity_overrides reads them with the file's.
* ``type: 'reviewed'``: an ``item`` taken out of the queue.

Actions (`decide`): ``merge`` (key into another key), ``split`` (a key's rows at some companies
become a new ``#`` key), ``keep_apart`` (pin an initial-form person's rows to itself, so the
rules never link it), ``relate`` (confirm a family link between two people, with its basis and
source: relations.py, issue #32), ``dismiss`` (an item needs nothing) and ``revoke`` (undo a
decision: an identity decision's rows go back to the key they named before).

Possible family links (``related:<a>|<b>`` items, relations.py) are not part of the public
queue: they are served only with the review password, left out of `decisions()` and never
written to reviewed.csv (the deploy mirror is public).

Into git: ``python -m mse_graph.cli review-export`` writes the decisions into
``data/overrides/identity.csv`` and ``data/overrides/reviewed.csv``; a PR then makes them part
of the code, so they also survive a lost database. A file row and a graph decision for the same
rows: the later ``decided_at`` wins, the graph's on a tie.
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import records as rec
from . import relations as rel
from .normalize import clean, is_organization, person_name

REVIEWED_FILE = "data/overrides/reviewed.csv"
REVIEWED_HEADER = ["item", "decided_by", "decided_at", "note"]
IDENTITY_HEADER = ["company", "name", "target", "source", "kind", "decided_by", "decided_at", "note"]
MIN_COMPANIES = 6           # current positions at this many companies: possibly several people
MIN_SECTORS = 4             # current links to companies in this many sectors
MIN_TOKEN = 4               # a one-letter difference counts only in a word at least this long
MIN_ORG = 6                 # ... and in an organisation name at least this long
ACTIONS = ("merge", "split", "keep_apart", "relate", "dismiss", "revoke")
_REL_KIND = {"OWNS": "shareholder", "HOLDS_POSITION": "officer", "BENEFICIAL_OWNER": "beneficial_owner"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _id(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:16]


# ---- folding names for comparison ----------------------------------------------------

_LATIN = [("shch", "щ"), ("kh", "х"), ("ch", "ч"), ("sh", "ш"), ("ts", "ц"), ("ya", "я"), ("yu", "ю"),
          ("yo", "ё"), ("ye", "е"), ("zh", "ж"), ("a", "а"), ("b", "б"), ("c", "ц"), ("d", "д"), ("e", "э"),
          ("f", "ф"), ("g", "г"), ("h", "х"), ("i", "и"), ("j", "ж"), ("k", "к"), ("l", "л"), ("m", "м"),
          ("n", "н"), ("o", "о"), ("p", "п"), ("q", "к"), ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"),
          ("v", "в"), ("w", "в"), ("x", "х"), ("y", "ы"), ("z", "з")]
_LATIN_RE = re.compile("|".join(a for a, _ in _LATIN))
_LATIN_MAP = dict(_LATIN)
# letters that sources write interchangeably (keyboards, transliteration back and forth)
_FOLD = str.maketrans({"ө": "о", "ү": "у", "ё": "е", "э": "е", "й": "и", "ы": "и", "ъ": None, "ь": None})


def fold(text: str) -> str:
    """`text` with Latin letters written in Cyrillic and look-alike letters folded, its words sorted."""
    t = _LATIN_RE.sub(lambda m: _LATIN_MAP[m.group(0)], (text or "").casefold())
    t = t.translate(_FOLD)
    return " ".join(sorted(re.sub(r"[^\w\s]", " ", t).split()))


def _one_apart(a: str, b: str) -> bool:
    """Levenshtein distance exactly 1."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i + (len(a) == len(b)):] == b[i + 1:]


def _deletions(s: str) -> set[str]:
    return {s} | {s[:i] + s[i + 1:] for i in range(len(s))}


def _base(key: str) -> str:
    return key.split("#", 1)[0]


# ---- decisions stored in the graph ---------------------------------------------------

def db_overrides(g) -> list[rec.Override]:
    """The identity decisions stored in the graph, oldest first, as override rows."""
    out = []
    for d in g.run("""MATCH (d:Decision {type: 'identity'})
                      RETURN d {.*} AS d ORDER BY d.created_at, d.id"""):
        d = d["d"]
        out.append(rec.Override(company=d["company"], name=d["name"], target=d["target"],
                                source=d.get("source") or "", kind=d.get("kind") or "",
                                decided_by=d.get("decided_by") or "review", decided_at=d.get("decided_at") or "",
                                note=d.get("note") or ""))
    return out


def _signature(o: rec.Override) -> tuple:
    return (o.company, rec.name_key(o.name), o.source, o.kind)


def combine(file_rows: list[rec.Override], db_rows: list[rec.Override]) -> list[rec.Override]:
    """The overrides in force: file rows and graph decisions, one row per set of rows they
    match (company, name key, source, kind). The later `decided_at` wins; on a tie the
    graph's decision (the newer action). The winners keep the file's order, then the graph's."""
    best: dict[tuple, tuple] = {}
    for rank, (o, from_db) in enumerate([(o, False) for o in file_rows] + [(o, True) for o in db_rows]):
        sig = _signature(o)
        score = (o.decided_at[:10] if o.decided_at else "", from_db, rank)
        if sig not in best or score > best[sig][0]:
            best[sig] = (score, o)
    order = {id(o): i for i, o in enumerate(file_rows + db_rows)}
    return sorted((o for _, o in best.values()), key=lambda o: order[id(o)])


def overrides_in_force(g, path=None) -> list[rec.Override]:
    return combine(rec.read_overrides(path), db_overrides(g))


def reviewed_path(path=None) -> Path:
    return Path(path or os.getenv("REVIEWED_ITEMS") or REVIEWED_FILE)


def read_reviewed(path=None) -> list[dict]:
    p = reviewed_path(path)
    if not p.is_file():
        return []
    lines = [ln for ln in p.read_text(encoding="utf-8-sig").splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    return [{k: clean(r.get(k)) for k in REVIEWED_HEADER} for r in csv.DictReader(lines) if clean(r.get("item"))]


def reviewed_items(g, path=None) -> set[str]:
    return {r["item"] for r in read_reviewed(path)} | {
        r["item"] for r in g.run("MATCH (d:Decision {type: 'reviewed'}) RETURN d.item AS item")}


def _private(d: dict) -> bool:
    """A decision about a possible family link (issue #32): only served with the review password."""
    return d.get("type") == "relation" or (d.get("item") or "").startswith(rel.ITEM_PREFIX)


def decisions(g, private: bool = False) -> list[dict]:
    """Every decision stored in the graph, newest first. Family-link decisions only when
    `private` (relations.review serves them behind the password)."""
    rows = [r["d"] for r in g.run("MATCH (d:Decision) RETURN d {.*} AS d ORDER BY d.created_at DESC, d.id")]
    return rows if private else [d for d in rows if not _private(d)]


def _store(g, rows: list[dict]) -> None:
    g.run("UNWIND $rows AS r MERGE (d:Decision {id: r.id}) SET d += r", rows=rows)


# ---- the queue ------------------------------------------------------------------------

def suspicious_people(g, min_companies: int = MIN_COMPANIES, min_sectors: int = MIN_SECTORS) -> list[dict]:
    """People whose node may be several people (see the module docstring)."""
    rows = g.run(
        """
        MATCH (p:Person) WHERE NOT p:Company
        OPTIONAL MATCH (p)-[r:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER {current: true}]->(c:Company)
        WITH p, c, collect(DISTINCT type(r)) AS rels, collect(DISTINCT r.title) AS titles
        WITH p, collect(CASE WHEN c IS NULL THEN NULL ELSE
                 {ticker: c.ticker, key: c.key, name: c.name, sector: c.sector, rels: rels,
                  titles: [t IN titles WHERE t IS NOT NULL]} END) AS cos
        OPTIONAL MATCH (p)<-[:REFERS_TO]-(x:Record)
        RETURN p.key AS key, p.name AS name, coalesce(p.name_variants, []) AS variants, cos,
               collect(DISTINCT x.raw_name) AS raws
        """)
    out = []
    for r in rows:
        if is_organization(r["name"] or ""):
            continue
        cos = sorted(r["cos"], key=lambda c: c["ticker"] or "")
        boards = [c for c in cos if "HOLDS_POSITION" in c["rels"]]
        sectors = sorted({clean(c["sector"]) for c in cos if clean(c["sector"])})
        spellings = {v.split(" | ", 1)[0] for v in r["variants"]} | {s for s in r["raws"] if s}
        # full names that can't be one person's: another patronymic (key) or another clan name
        fulls, keys, clans = set(), set(), set()
        for s in spellings:
            pn = person_name(s)
            if pn.form == "full":
                fulls.add(pn.full_name)
                keys.add(pn.key)
                if pn.clan:
                    clans.add(fold(pn.clan))
        reasons = []
        if len(boards) >= min_companies:
            reasons.append("many_companies")
        if len(sectors) >= min_sectors:
            reasons.append("many_sectors")
        if len(keys) > 1 or len(clans) > 1:
            reasons.append("different_full_names")
        if reasons:
            out.append({"item": f"suspicious:{r['key']}", "key": r["key"], "name": r["name"], "reasons": reasons,
                        "companies": [{k: c[k] for k in ("ticker", "key", "name", "sector", "titles")} for c in cos],
                        "boards": len(boards), "sectors": sectors, "full_names": sorted(fulls)})
    return sorted(out, key=lambda x: (-len(x["reasons"]), -x["boards"], x["name"] or ""))


def _nodes(g) -> list[dict]:
    return g.run(
        """
        MATCH (e:Entity) WHERE (e:Person OR e:Organization) AND NOT e:Company
        OPTIONAL MATCH (e)-[:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER]->(c:Company)
        RETURN e.key AS key, e.name AS name, e.full_name AS full_name,
               CASE WHEN e:Person THEN 'Person' ELSE 'Organization' END AS label,
               collect(DISTINCT c.ticker) AS companies
        """)


def possible_duplicates(g) -> list[dict]:
    """Pairs of people or organisations whose keys may name one entity (module docstring)."""
    nodes = [n for n in _nodes(g) if n["key"]]
    info = {n["key"]: n for n in nodes}
    pairs: dict[tuple, str] = {}

    def add(a: str, b: str, reason: str) -> None:
        if a == b or _base(a) == _base(b):
            return          # namesakes split on purpose (`#` keys) are no duplicates
        pairs.setdefault(tuple(sorted((a, b))), reason)

    for label in ("Person", "Organization"):
        group = [n for n in nodes if n["label"] == label]
        parsed = {}
        for n in group:
            body = n["key"].split(":", 1)[1]
            parsed[n["key"]] = person_name(n["full_name"] or n["name"] or "") if label == "Person" else None
            n["_fold"] = fold(_base(body))
        # the same once folded
        by_fold: dict[str, list[str]] = {}
        for n in group:
            by_fold.setdefault(n["_fold"], []).append(n["key"])
        for keys in by_fold.values():
            for i, a in enumerate(keys):
                for b in keys[i + 1:]:
                    add(a, b, "transliteration")
        # one letter apart (symmetric deletion index, then checked)
        index: dict[str, set[str]] = {}
        for n in group:
            for d in _deletions(n["_fold"]):
                index.setdefault(d, set()).add(n["key"])
        for keys in index.values():
            keys = sorted(keys)
            for i, a in enumerate(keys):
                for b in keys[i + 1:]:
                    fa, fb = info[a]["_fold"], info[b]["_fold"]
                    if not _one_apart(fa, fb):
                        continue
                    if label == "Organization":
                        if min(len(fa), len(fb)) >= MIN_ORG:
                            add(a, b, "one_letter")
                    elif _close_people(parsed[a], parsed[b], fa, fb):
                        add(a, b, "one_letter")
        # a lone given name next to a person with that given name at the same company
        if label == "Person":
            lone = [n for n in group if parsed[n["key"]].form == "other" and len(n["_fold"].split()) == 1]
            for n in lone:
                for m in group:
                    pm = parsed[m["key"]]
                    if pm.form in ("initial", "full") and fold(pm.given) == n["_fold"] \
                            and set(n["companies"]) & set(m["companies"]):
                        add(n["key"], m["key"], "missing_initial")

    out = []
    for (a, b), reason in pairs.items():
        na, nb = info[a], info[b]
        # suggest keeping the node with more links (on a tie, the fuller name)
        keep, drop = sorted((na, nb), key=lambda n: (-len(n["companies"]), -len(n["name"] or ""), n["key"]))
        out.append({"item": f"pair:{a}|{b}", "reason": reason, "label": na["label"],
                    "a": _brief(na), "b": _brief(nb), "shared": sorted(set(na["companies"]) & set(nb["companies"])),
                    "suggest": {"merge": drop["key"], "into": keep["key"]}})
    order = {"missing_initial": 0, "transliteration": 1, "one_letter": 2}
    return sorted(out, key=lambda x: (not x["shared"], order[x["reason"]], x["a"]["name"] or ""))


def _close_people(pa, pb, fa: str, fb: str) -> bool:
    """Two person keys one letter apart that may be one person: the letter is in the given name
    or the patronymic of a full name (not an initial: another initial is another father)."""
    if pa.form != pb.form or pa.form not in ("initial", "full", "other"):
        return False
    ta, tb = fa.split(), fb.split()
    if len(ta) != len(tb):
        return False
    diff = [(x, y) for x, y in zip(ta, tb) if x != y]
    if len(diff) != 1:
        return False
    x, y = diff[0]
    if pa.form == "initial" and fold(pa.initial) != fold(pb.initial):
        return False        # "Б.Батболд" and "Д.Батболд" are two fathers' sons
    return min(len(x), len(y)) >= MIN_TOKEN


def _brief(n: dict) -> dict:
    return {"key": n["key"], "name": n["name"], "companies": sorted(c for c in n["companies"] if c)}


def queue(g, min_companies: int = MIN_COMPANIES, min_sectors: int = MIN_SECTORS) -> dict:
    """The review queue: what is left after the decisions (module docstring)."""
    done = reviewed_items(g)
    amb = [dict(p, item=f"ambiguous:{p['key']}") for p in g.ambiguous_people()]
    amb = [p for p in amb if p["item"] not in done]
    sus = [p for p in suspicious_people(g, min_companies, min_sectors) if p["item"] not in done]
    dup = [p for p in possible_duplicates(g) if p["item"] not in done]
    counts = {r: sum(1 for p in amb if p["reason"] == r) for r in ("several_at_company", "no_shared_company")}
    counts.update({"suspicious_people": len(sus), "possible_duplicates": len(dup)})
    return {"ambiguous_people": amb, "suspicious_people": sus, "possible_duplicates": dup, "counts": counts}


# ---- actions ---------------------------------------------------------------------------

class ReviewError(ValueError):
    pass


def spellings(g, key: str) -> list[dict]:
    """The rows that name `key`, as override rows would match them: one per (company, name
    key, kind). From its source records, and for relationships without records (loaded
    before #37) from the node's own name."""
    out: dict[tuple, dict] = {}
    for r in g.run(
            """MATCH (e:Entity {key: $k})<-[:REFERS_TO]-(x:Record)-[:REPORTED_FOR]->(c:Company)
               WHERE x.kind IN ['shareholder', 'officer', 'beneficial_owner'] AND x.raw_name IS NOT NULL
               RETURN c.ticker AS t, x.raw_name AS name, x.kind AS kind ORDER BY x.last_seen DESC""", k=key):
        out.setdefault((r["t"], rec.name_key(r["name"]), r["kind"]), {"company": r["t"], "name": r["name"],
                                                                     "kind": r["kind"]})
    for r in g.run(
            """MATCH (e:Entity {key: $k})-[x:OWNS|HOLDS_POSITION|BENEFICIAL_OWNER]->(c:Company)
               WHERE size(coalesce(x.records, [])) = 0
               RETURN DISTINCT c.ticker AS t, type(x) AS rel, e.name AS name""", k=key):
        kind = _REL_KIND[r["rel"]]
        out.setdefault((r["t"], rec.name_key(r["name"]), kind), {"company": r["t"], "name": r["name"], "kind": kind})
    return sorted(out.values(), key=lambda x: (x["company"], x["kind"], x["name"]))


def _entity(g, key: str) -> dict:
    rows = g.run("""MATCH (e:Entity) WHERE e.key = $k OR $k IN coalesce(e.former_keys, [])
                    RETURN e.key AS key, e.name AS name,
                           [l IN labels(e) WHERE l IN ['Company', 'Person', 'Organization']][0] AS label
                    ORDER BY (e.key = $k) DESC LIMIT 1""", k=(key or "").strip())
    if not rows:
        raise ReviewError(f"no entity {key}")
    return rows[0]


def _identity_rows(rows: list[dict], target: str, before: str, group: str, action: str,
                   by: str, note: str, now: str) -> list[dict]:
    return [{"id": _id(group, r["company"], r["name"], r["kind"]), "type": "identity", "group": group,
             "action": action, "company": r["company"], "name": r["name"], "target": target, "source": "",
             "kind": r["kind"], "before": before, "decided_by": by, "decided_at": now[:10], "note": note,
             "created_at": now} for r in rows]


def decide(g, action: str, *, key: str = "", into: str = "", companies: list[str] | None = None,
           new_key: str = "", item: str = "", id: str = "", by: str = "", note: str = "",
           basis: str = "", source: str = "") -> dict:
    """Record a decision and apply it. Returns {"decision": group id, "stored": n, "applied": ...,
    "relations": ...}. `relate` takes `key`, `into` (the other person), `basis` and `source`."""
    if action not in ACTIONS:
        raise ReviewError(f"unknown action {action!r}")
    by = clean(by) or "review"
    note = clean(note)
    now = _now()
    group = _id(action, key, into, ",".join(companies or []), new_key, item, id, now)
    rows: list[dict] = []
    if action == "relate":
        rows = rel.decision_rows(g, key, into, basis, source, by, note, now, group, _id)
    elif action == "dismiss":
        if not item:
            raise ReviewError("dismiss needs an item")
        rows = [{"id": _id("reviewed", item), "type": "reviewed", "group": group, "action": action, "item": item,
                 "decided_by": by, "decided_at": now[:10], "note": note, "created_at": now}]
    elif action == "revoke":
        found = g.run("MATCH (d:Decision) WHERE d.id = $id OR d.group = $id RETURN d {.*} AS d", id=id)
        if not found:
            raise ReviewError(f"no decision {id}")
        ds = [f["d"] for f in found]
        g.run("MATCH (d:Decision) WHERE d.id IN $ids DETACH DELETE d", ids=[d["id"] for d in ds])
        # an identity decision's rows go back to the key they named before, as a decision of its own
        back = [{"company": d["company"], "name": d["name"], "kind": d.get("kind") or ""}
                for d in ds if d["type"] == "identity" and d.get("before") and d["before"] != d["target"]]
        if back:
            before = next(d["before"] for d in ds if d["type"] == "identity" and d.get("before"))
            rows = _identity_rows(back, before, before, group, "revoke", by, note or f"revoked {id}", now)
    else:
        e = _entity(g, key)
        if e["label"] not in ("Person", "Organization"):
            raise ReviewError("only people and organisations are reviewed here")
        found = spellings(g, e["key"])
        if action == "merge":
            t = _entity(g, into)
            if t["key"] == e["key"]:
                raise ReviewError("merge into itself")
            if (e["label"], t["label"]) not in (("Person", "Person"), ("Organization", "Organization"),
                                                ("Organization", "Company")):
                raise ReviewError(f"cannot merge a {e['label']} into a {t['label']}")
            rows = _identity_rows(found, t["key"], e["key"], group, action, by, note, now)
            rows.append({"id": _id("reviewed", group), "type": "reviewed", "group": group, "action": action,
                         "item": f"pair:{'|'.join(sorted((e['key'], t['key'])))}", "decided_by": by,
                         "decided_at": now[:10], "note": note, "created_at": now})
        elif action == "split":
            tickers = {g.resolve_ticker(t) for t in (companies or []) if t}
            if not tickers:
                raise ReviewError("split needs the companies whose rows are another entity")
            mine = [r for r in found if r["company"] in tickers]
            if not mine:
                raise ReviewError(f"{e['key']} has no rows at {', '.join(sorted(tickers))}")
            if {r["company"] for r in found} <= tickers:
                raise ReviewError("that is every company: nothing would be left to split from")
            target = (new_key or "").strip()
            if not target:
                target = f"{_base(e['key'])}#{min(tickers).lower()}"
            prefix, _, rest = target.partition(":")
            target = prefix.lower() + ":" + rest.strip().casefold()
            if rec.label_of(target) != e["label"] or _entity_exists(g, target):
                raise ReviewError(f"{target} must be a new {e['label']} key")
            rows = _identity_rows(mine, target, e["key"], group, action, by, note, now)
        elif action == "keep_apart":
            rows = _identity_rows(found, e["key"], e["key"], group, action, by, note, now)
            rows.append({"id": _id("reviewed", "ambiguous", e["key"]), "type": "reviewed", "group": group,
                         "action": action, "item": f"ambiguous:{e['key']}", "decided_by": by,
                         "decided_at": now[:10], "note": note, "created_at": now})
        if not any(r["type"] == "identity" for r in rows):
            raise ReviewError(f"{e['key']} has no rows to decide")
    _store(g, rows)
    applied = g.apply_overrides()
    return {"decision": group, "stored": len(rows), "applied": applied, "relations": rel.apply(g)}


def _entity_exists(g, key: str) -> bool:
    return bool(g.run("MATCH (e:Entity) WHERE e.key = $k OR $k IN coalesce(e.former_keys, []) RETURN 1 AS x",
                      k=key))


# ---- into git ------------------------------------------------------------------------

def identity_csv(rows: list[rec.Override]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(IDENTITY_HEADER)
    for o in rows:
        w.writerow([o.company, o.name, o.target, o.source, o.kind, o.decided_by, o.decided_at, o.note])
    return buf.getvalue()


def reviewed_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(REVIEWED_HEADER)
    for r in rows:
        w.writerow([r.get(k) or "" for k in REVIEWED_HEADER])
    return buf.getvalue()


def _comments(p: Path) -> str:
    if not p.is_file():
        return ""
    lines = []
    for ln in p.read_text(encoding="utf-8-sig").splitlines():
        if not ln.lstrip().startswith("#"):
            break
        lines.append(ln)
    return "\n".join(lines) + "\n" if lines else ""


def all_reviewed(g, path=None) -> list[dict]:
    """The file's reviewed items and the graph's, one row per item (the file's first). Dismissed
    family-link hypotheses (`related:`) stay in the graph only: the file goes to a public repository."""
    out = {r["item"]: r for r in read_reviewed(path)}
    for d in g.run("MATCH (d:Decision {type: 'reviewed'}) RETURN d {.*} AS d ORDER BY d.created_at"):
        out.setdefault(d["d"]["item"], {k: d["d"].get(k) or "" for k in REVIEWED_HEADER})
    return [r for item, r in out.items() if not item.startswith(rel.ITEM_PREFIX)]


def pending(g, identity_path=None, reviewed_path_=None) -> dict:
    """Graph decisions the files don't hold yet (what `review-export` would add)."""
    file_ids = {_signature(o): o for o in rec.read_overrides(identity_path)}
    ids = [o for o in db_overrides(g) if file_ids.get(_signature(o)) is None
           or (file_ids[_signature(o)].target, file_ids[_signature(o)].decided_at) != (o.target, o.decided_at)]
    have = {r["item"] for r in read_reviewed(reviewed_path_)}
    items = [r for r in all_reviewed(g, reviewed_path_) if r["item"] not in have]
    return {"identity": len(ids), "reviewed": len(items)}


def export(g, identity_path=None, reviewed_path_=None) -> dict:
    """Write the decisions in force into the overrides files (comments at their top kept)."""
    ip = rec.overrides_path(identity_path)
    rp = reviewed_path(reviewed_path_)
    before = pending(g, identity_path, reviewed_path_)
    rows = overrides_in_force(g, identity_path)
    reviewed = all_reviewed(g, reviewed_path_)
    ip.parent.mkdir(parents=True, exist_ok=True)
    ip.write_text(_comments(ip) + identity_csv(rows), encoding="utf-8")
    rp.write_text((_comments(rp) or REVIEWED_COMMENT) + reviewed_csv(reviewed), encoding="utf-8")
    out = {"identity_file": str(ip), "identity_rows": len(rows), "reviewed_file": str(rp),
           "reviewed_rows": len(reviewed), "added": before}
    out.update(rel.export(g))       # confirmed family links (issue #32)
    return out


REVIEWED_COMMENT = ("# Review queue items a reviewer dismissed or decided (issue #6, mse_graph/review.py).\n"
                    "# item: ambiguous:<key> | suspicious:<key> | pair:<key a>|<key b> (keys sorted).\n"
                    "# Written by `python -m mse_graph.cli review-export`; such items stay out of the queue.\n")
