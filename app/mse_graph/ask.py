"""Ask the graph (issue #10): a question in plain Mongolian or English, answered by one of a fixed
set of read-only queries.

The question never becomes Cypher. `answer` finds the companies, people and organisations it names
(tickers, then names through `Graph.search`, longest phrase first, with Mongolian case endings
dropped), picks one **intent** from the allowlist below by its keywords, and runs that intent's
query, which is one of the graph's existing read-only functions with the found keys as parameters.

    intent           needs                  query
    owners           a company              its current shareholders (Graph.entity)
    ultimate_owners  a company              look-through owners (Graph.ultimate_owners)
    officers         a company              its board and management (Graph.entity)
    holdings         anyone                 the companies it holds stakes in (Graph.entity)
    positions        a person               where they hold positions (Graph.entity)
    related          a company              companies linked through people or owners
    flags            a company              governance flags (analysis.company_flags)
    changes          a company, or none     significant changes (history.recent_changes)
    path             two entities           the shortest connection (Graph.shortest_path)
    interlocks       none                   people tied to several listed companies
    state            none                   companies with state ownership (analysis.state_ownership)
    largest          none                   the largest companies by market cap (market.market)
    summary          one entity, no intent  what the graph has on it (Graph.entity)

When nothing fits, the answer says so and lists example questions. Every answer names the intent
it ran (`intent`), the entities it understood (`entities`, with the other candidates in
`alternatives`) and, where the graph can show it, a view to open (`view`).
"""
from __future__ import annotations

import re

from .normalize import clean

MAX_QUESTION = 300
ROLE_ORDER = ("chair", "vice_chair", "ceo", "deputy_ceo", "cfo", "board_member", "independent_director",
              "supervisory", "secretary", "other_executive", "unknown")
MAX_ROWS = 25

# keywords per intent, Mongolian and English (lower case; a keyword matches the start of a word)
KEYWORDS: dict[str, tuple[str, ...]] = {
    "path": ("холбогд", "холбоо", "хоорондын", "хооронд", "connect", "connection", "link", "path", "between",
             "related to"),
    "ultimate_owners": ("эцсийн", "хяналт", "хянадаг", "ultimate", "ultimately", "look-through", "lookthrough",
                        "controls", "control", "beneficial"),
    "owners": ("эзэмшигч", "хэн эзэмш", "эзэмшдэг вэ", "хувьцаа эзэмш", "own", "owns", "owner", "owners",
               "shareholder", "shareholders", "holders"),
    "officers": ("туз", "удирдлага", "захирал", "гүйцэтгэх", "дарга", "гишүүн", "board", "director", "directors",
                 "ceo", "management", "manager", "runs", "officer", "officers", "chair"),
    "holdings": ("юу эзэмш", "ямар компани эзэмш", "эзэмшдэг компани", "эзэмшил", "holdings", "stakes",
                 "invest", "portfolio", "what does"),
    "positions": ("ажилладаг", "ажилд", "албан тушаал", "хаана", "work", "works", "sits", "seats", "positions",
                  "where does"),
    "related": ("холбоотой компани", "related companies", "related", "linked companies"),
    "flags": ("анхаарах", "эрсдэл", "зөрчил", "flag", "flags", "risk", "risks", "red flag", "warning", "concern"),
    "changes": ("өөрчлөлт", "өөрчлөгд", "шинэ", "change", "changes", "changed", "new", "recent", "latest"),
    "interlocks": ("олон компани", "олон туз", "several boards", "many boards", "interlock", "multiple boards",
                   "several companies"),
    "state": ("төрийн", "төр ", "төрийн өмч", "state", "government", "state-owned"),
    "largest": ("хамгийн том", "томоохон", "үнэлгээ", "largest", "biggest", "market cap", "top companies"),
}

# the order intents are tried in when several keywords match: the specific before the general
PRIORITY = ("path", "interlocks", "related", "ultimate_owners", "flags", "changes", "state", "largest",
            "holdings", "positions", "officers", "owners")
NEEDS = {"owners": "company", "ultimate_owners": "company", "officers": "company", "related": "company",
         "flags": "company", "holdings": "any", "positions": "any", "changes": "optional", "path": "two",
         "interlocks": "none", "state": "none", "largest": "none"}

# words that never name an entity on their own
STOP = {
    # Mongolian question and filler words
    "хэн", "юу", "ямар", "хаана", "хэрхэн", "яаж", "вэ", "бэ", "нь", "ба", "болон", "хоёр", "бол", "уу", "үү",
    "юм", "байдаг", "байгаа", "компани", "компанийн", "компанийг", "компаниуд", "хувьцаа", "хүн", "хүмүүс",
    "хэдэн", "хэд", "нийт", "мөн", "энэ", "тэр", "ямар", "сүүлийн", "хамгийн", "том", "дээр", "дотор", "бүх",
    # English
    "who", "what", "which", "where", "how", "is", "are", "the", "a", "an", "of", "in", "on", "at", "to", "and",
    "does", "do", "did", "has", "have", "with", "for", "by", "company", "companies", "person", "people", "me",
    "show", "tell", "list", "about", "between", "from", "it", "its", "their", "his", "her", "s",
}
for words in KEYWORDS.values():
    for w in words:
        STOP.update(w.split())

# Mongolian case endings and possessives that follow a name ("Хаан Банкны", "АПУ-гийн", "Батболдын")
_ENDINGS = ("ийнхэн", "уудын", "үүдийн", "гийн", "ийн", "ын", "ны", "ний", "ийг", "ыг", "тай", "тэй", "той",
            "аас", "ээс", "оос", "өөс", "д", "т", "г", "ий", "ы")
_TOKEN = re.compile(r"[\w.'’&-]+", re.UNICODE)


def _stem(word: str) -> str:
    """A name word without a trailing case ending: 'АПУ-гийн' -> 'АПУ', 'Батболдын' -> 'Батболд'."""
    w = word.strip(".,?!:;\"'’-")
    if "-" in w:
        head, _, tail = w.rpartition("-")
        if head and tail.lower() in _ENDINGS:
            return head
    low = w.lower()
    for e in _ENDINGS:
        if low.endswith(e) and len(w) - len(e) >= 4:
            return w[: len(w) - len(e)]
    return w


def intent_of(question: str) -> str | None:
    """The first intent in PRIORITY whose keywords appear in the question (None when none do)."""
    q = " " + clean(question).lower() + " "
    hits = {name for name, words in KEYWORDS.items()
            if any(re.search(r"(?<![\w])" + re.escape(w), q) for w in words)}
    return next((name for name in PRIORITY if name in hits), None)


def _entities(g, question: str, want: int = 2) -> tuple[list[dict], list[list[dict]]]:
    """Up to `want` entities the question names: tickers first, then names (the longest phrase
    the search finds, its most connected match). Returns the entities and, for each, the other
    candidates."""
    tokens = _TOKEN.findall(question)
    tickers = g.company_keys()
    found: list[dict] = []
    alts: list[list[dict]] = []
    used = [False] * len(tokens)

    def take(i: int, j: int, hits: list[dict]):
        for k in range(i, j):
            used[k] = True
        found.append(hits[0])
        alts.append(hits[1:5])

    # tickers: as written in capitals, or a known ticker in any case that is no common word
    for i, t in enumerate(tokens):
        if len(found) >= want:
            break
        s = _stem(t)
        up = s.upper()
        if up in tickers and len(up) >= 2 and (s == up or (len(up) >= 3 and s.lower() not in STOP)):
            hits = g.search(up, 5)
            hits = [h for h in hits if (h.get("ticker") or "").upper() == up] or hits
            if hits:
                take(i, i + 1, hits)

    # names: longest run of non-stop words first
    n = len(tokens)
    for size in range(min(5, n), 0, -1):
        for i in range(0, n - size + 1):
            if len(found) >= want:
                return found, alts
            span = range(i, i + size)
            if any(used[k] for k in span):
                continue
            words = [_stem(tokens[k]) for k in span]
            if any(w.lower() in STOP for w in (words[0], words[-1])) or all(w.lower() in STOP for w in words):
                continue
            phrase = " ".join(words)
            if len(phrase.replace(" ", "")) < 3:
                continue
            hits = [h for h in g.search(phrase, 6) if h["key"] not in {f["key"] for f in found}]
            if hits:
                take(i, i + size, hits)
    return found, alts


def _row(x: dict, **extra) -> dict:
    keep = ("key", "name", "name_en", "type", "ticker", "percent", "title", "role_std", "holding_type", "as_of")
    return {**{k: x.get(k) for k in keep if x.get(k) is not None}, **extra}


def _run(g, intent: str, ents: list[dict]) -> dict:
    from . import analysis, history
    e = ents[0] if ents else None
    if intent in ("owners", "officers", "holdings", "positions", "summary") and e:
        d = g.entity(e["key"]) or {}
        if intent == "owners":
            rows = [_row(o) for o in d.get("owners", [])]
        elif intent == "officers":       # the board first, then management
            rows = sorted((_row(p) for p in d.get("people", [])),
                          key=lambda r: (ROLE_ORDER.index(r.get("role_std")) if r.get("role_std") in ROLE_ORDER
                                         else len(ROLE_ORDER), r.get("name") or ""))
        elif intent == "holdings":
            rows = [_row(h) for h in d.get("holdings", [])]
            rows.sort(key=lambda r: -(r.get("percent") or 0))
        elif intent == "positions":
            rows = [_row(p) for p in d.get("positions", [])]
        else:
            ent = d.get("entity", {})
            rows = [{"name": label, "count": len(d.get(part, []))}
                    for part, label in (("owners", "owners"), ("people", "officers"), ("holdings", "holdings"),
                                        ("positions", "positions"), ("beneficial_owners", "beneficial_owners"))
                    if d.get(part)]
            return {"rows": rows, "view": {"focus": e["key"]},
                    "dossier": ent.get("ticker") if ent.get("type") == "Company" else None}
        return {"rows": rows, "view": {"focus": e["key"]}}
    if intent == "ultimate_owners" and e:
        rows = [_row(u, percent=u["effective_percent"], chains=u.get("chains", [])[:2])
                for u in g.ultimate_owners(e["ticker"])]
        return {"rows": rows, "view": {"focus": e["key"]}}
    if intent == "related" and e:
        rows = [_row(r, via=sorted({x["name"] for x in r["links"]})[:6]) for r in g.related_companies(e["key"])]
        return {"rows": rows, "view": {"focus": e["key"]}}
    if intent == "flags" and e:
        return {"rows": [{"level": f["level"], "kind": f["kind"], "text": f["text"], "key": (f["keys"] or [None])[0]}
                         for f in analysis.company_flags(g, e["ticker"])], "view": {"focus": e["key"]}}
    if intent == "changes":
        if e and e.get("type") == "Company":
            rows = history.recent_changes(g, ticker=e["ticker"], significant_only=True, limit=MAX_ROWS)
        elif e:
            rows = history.recent_changes(g, key=e["key"], significant_only=True, limit=MAX_ROWS)
        else:
            rows = history.recent_changes(g, significant_only=True, limit=MAX_ROWS)
        return {"rows": rows, "view": {"focus": e["key"]} if e else None, "kind": "changes"}
    if intent == "path" and len(ents) >= 2:
        p = g.shortest_path(ents[0]["key"], ents[1]["key"])
        by = {n["key"]: n for n in p["nodes"]}
        rows = [_row(n) for n in p["nodes"]]
        links = [{"type": x.get("type"), "from": x.get("source"), "to": x.get("target"), "percent": x.get("percent"),
                  "title": x.get("title"), "role_std": x.get("role_std")} for x in p["edges"]]
        return {"rows": rows, "links": links, "found": bool(by),
                "view": {"a": ents[0]["key"], "b": ents[1]["key"]}}
    if intent == "interlocks":
        rows = [{"key": r["key"], "name": r["name"], "count": r["companies"],
                 "via": list(dict.fromkeys(l.get("ticker") or l.get("name") for l in r["links"]))[:8]} for r in g.interlocks(2, MAX_ROWS)]
        return {"rows": rows, "view": None}
    if intent == "state":
        rows = [{"key": s["key"], "name": s.get("name"), "ticker": s.get("ticker"),
                 "percent": s.get("lookthrough_percent"), "direct": s.get("direct_percent")}
                for s in analysis.state_ownership(g)[:MAX_ROWS]]
        return {"rows": rows, "view": None}
    if intent == "largest":
        from . import market
        rows = [{"key": c["key"], "name": c.get("name"), "name_en": c.get("name_en"), "ticker": c.get("ticker"),
                 "market_cap": c.get("market_cap"), "percent": c.get("weight_percent")}
                for c in market.market(g, "cap")["items"] if c.get("market_cap") is not None][:MAX_ROWS]
        return {"rows": rows, "view": None}
    return {"rows": [], "view": None}


EXAMPLES = ("Хэн АПУ-г эзэмшдэг вэ?", "Who ultimately owns BDL?", "Board of BDL", "Б.Батболд хаана ажилладаг вэ?",
            "How is APU connected to SUU?", "Flags for TTL", "Recent changes", "State-owned companies",
            "Largest companies")


def answer(g, question: str) -> dict:
    """The answer to `question`: {question, intent, entities, alternatives, rows, view, ...}."""
    q = clean(question or "")[:MAX_QUESTION]
    out = {"question": q, "intent": None, "entities": [], "alternatives": [], "rows": [], "view": None,
           "examples": list(EXAMPLES)}
    if not q:
        return out
    intent = intent_of(q)
    need = NEEDS.get(intent or "", "any")
    want = 2 if need == "two" else 0 if need == "none" else 1
    ents, alts = _entities(g, q, want) if want else ([], [])
    if intent is None and ents:
        intent = "summary"
    if intent and need == "company" and ents and ents[0].get("type") != "Company":
        # "who owns Б.Батболд" makes no sense; "what does he own" does
        intent = {"owners": "holdings", "officers": "positions"}.get(intent, intent)
        if NEEDS.get(intent) == "company":
            out.update(intent=intent, entities=ents, alternatives=alts, missing="company")
            return out
    missing = intent is not None and ((need in ("company", "any") and not ents) or (need == "two" and len(ents) < 2))
    out.update(intent=intent, entities=[_row(x) for x in ents], alternatives=[[_row(a) for a in xs] for xs in alts])
    if intent is None or missing:
        if missing:
            out["missing"] = "two" if need == "two" else "entity"
        return out
    res = _run(g, intent, ents)
    res["rows"] = res["rows"][:MAX_ROWS]
    out.update(res)
    return out
