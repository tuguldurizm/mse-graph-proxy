"""Discovery: the graph grows by itself from the Open Data Lab registry.

A registry page names, in its beneficial-owner chain (Эцсийн өмчлөгч), the legal
entities that own the company *with their registration numbers*. Those numbers
are the only way from one page to the next: shareholders and officers carry no
number, the pages don't link to each other, and robots.txt disallows name
search. So every registry page loaded keeps them (BENEFICIAL_OWNER.owner_reg),
and each run:

1. lists the numbers to read next (`frontier`), in this order:
   * companies already in the graph whose registry page was never read (a new
     listing, a newly added hand-curated company);
   * the numbers in data/discovery/seeds.csv;
   * owners in the chains that the graph has no company for, the shallowest first,
     up to `max_depth` steps away from the companies the graph started with;
   * companies that holder searches found (holder_search.py: a person's or an
     organisation's other holdings, from Open Data Lab's name search), one step deeper
     than the holder, the closest first;
2. reads up to `budget` of those pages, one every 11 s like every opendatalab.mn
   request, and loads each one (`discover`):
   * a company the graph already has (its own number, or the page's name matches a
     company that has no register number yet, such as a listed company, for which
     mse.mn gives only the 10-digit state number): the page's shareholders,
     officers and beneficial owners are added to it, and it gets the number, so the
     regular registry import keeps it up to date from then on;
   * otherwise a new unlisted company (ticker "~<reg no>"), with its shareholders,
     its officers (often people new to the graph) and its own owner chain, which
     gives the next leads;
   pages loaded in one batch give the next batch's leads, until the budget is spent.

What discovery adds is the registry's first word on a company, a baseline rather
than a change, so it doesn't flood the change feed or the alerts. Discovered
companies have a 7-digit reg_no, so the regular registry import re-reads them
(one seventh a day) and records their changes from then on. Numbers that lead
nowhere (no such page, or a page whose name another company already has) rest
for `retry_days`. Every record keeps its registry URL as `source`.

    python -m mse_graph.cli odl-discover [--budget N] [--max-depth D] [--dry-run]
"""
from __future__ import annotations

import csv
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from . import csv_io, opendatalab
from .models import Company, Dataset
from .normalize import clean, entity_key

log = logging.getLogger(__name__)

SEEDS = Path("data/discovery/seeds.csv")
DEFAULT_BUDGET = 25          # pages per run: about 5 minutes at 11 s a page
DEFAULT_MAX_DEPTH = 3        # steps along ownership chains from the starting companies
DEFAULT_RETRY_DAYS = 30
AUTO_SPREAD_DAYS = 7         # budget "auto": the frontier spread over a week of daily runs ...
AUTO_MAX_BUDGET = 300        # ... but at most this many pages a run (55 minutes)


def settings_from_env() -> dict:
    """DISCOVERY_BUDGET (0 turns discovery off; "auto": see auto_budget), DISCOVERY_MAX_DEPTH,
    DISCOVERY_RETRY_DAYS, DISCOVERY_SEEDS, DISCOVERY_MAX_BUDGET (the cap of "auto")."""
    raw = (os.getenv("DISCOVERY_BUDGET") or "").strip().lower()
    return {
        "budget": "auto" if raw == "auto" else int(raw or DEFAULT_BUDGET),
        "max_budget": int(os.getenv("DISCOVERY_MAX_BUDGET") or AUTO_MAX_BUDGET),
        "max_depth": int(os.getenv("DISCOVERY_MAX_DEPTH") or DEFAULT_MAX_DEPTH),
        "retry_days": int(os.getenv("DISCOVERY_RETRY_DAYS") or DEFAULT_RETRY_DAYS),
        "seeds_path": os.getenv("DISCOVERY_SEEDS") or str(SEEDS),
    }


@dataclass
class Lead:
    reg: str
    kind: str                    # "unread" | "seed" | "owner" | "holding"
    name: str = ""               # the name it was found under
    ticker: str = ""             # the company the page belongs to, when that is known
    depth: int = 0
    via: list[str] = field(default_factory=list)   # companies whose page named it (holders, for "holding")


def auto_budget(leads: int, max_budget: int = AUTO_MAX_BUDGET, spread_days: int = AUTO_SPREAD_DAYS) -> int:
    """Pages for one run with budget "auto": the waiting leads spread over `spread_days`
    daily runs, at least DEFAULT_BUDGET and at most `max_budget`. As holder searches find
    more companies the daily share grows with them."""
    return max(DEFAULT_BUDGET, min(max_budget, -(-leads // max(1, spread_days))))


def resolve_budget(g, opts: dict) -> int:
    """The page budget of a run: DISCOVERY_BUDGET, or for "auto" auto_budget() of the frontier."""
    if opts["budget"] != "auto":
        return int(opts["budget"])
    leads = frontier(g, opts["max_depth"], read_seeds(opts["seeds_path"]), opts["retry_days"])
    return auto_budget(len(leads), opts.get("max_budget", AUTO_MAX_BUDGET))


def read_seeds(path: str | Path | None = None) -> list[dict]:
    """Rows of the seeds file: {"reg", "ticker", "note"}. `ticker` (optional) names
    the company in the graph the number belongs to, e.g. a listed company."""
    p = Path(path or SEEDS)
    if not p.is_file():
        return []
    text = p.read_text(encoding="utf-8-sig")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    out = []
    for row in csv.DictReader(lines):
        reg = re.sub(r"\D", "", row.get("reg_no") or "")
        if not opendatalab.is_register_no(reg):
            log.warning("seeds: %r is not a register number (6-8 digits), skipped", row.get("reg_no"))
            continue
        out.append({"reg": reg, "ticker": clean(row.get("ticker")).upper(), "note": clean(row.get("note"))})
    return out


# Pages read between loads into the graph (and marks of what was read).
LOAD_EVERY = int(os.getenv("DISCOVERY_LOAD_EVERY", "25"))


def frontier(g, max_depth: int = DEFAULT_MAX_DEPTH, seeds: list[dict] | None = None,
             retry_days: int = DEFAULT_RETRY_DAYS) -> list[Lead]:
    """The registry numbers discovery would read next, in order (see the module docstring)."""
    out: list[Lead] = []
    seen: set[str] = set()

    def add(lead: Lead):
        if lead.reg not in seen:
            seen.add(lead.reg)
            out.append(lead)

    for r in g.unread_companies(retry_days):
        add(Lead(r["reg"], "unread", r["name"] or "", r["ticker"], r["depth"]))
    if seeds:
        regs = g.company_registers()
        known = {v["reg"] for v in regs.values() if v["reg"]}
        resting = g.resting_registry_numbers(retry_days)
        for s in seeds:
            if s["reg"] in known or s["reg"] in resting:
                continue
            ticker = s["ticker"]
            if ticker and ticker not in regs:
                log.warning("seeds: %s names %s, which isn't in the graph; reading it as a new company",
                            s["reg"], ticker)
                ticker = ""
            if ticker and regs[ticker]["reg"]:
                log.warning("seeds: %s already has register number %s, not %s; skipped",
                            ticker, regs[ticker]["reg"], s["reg"])
                continue
            add(Lead(s["reg"], "seed", s["note"], ticker, 0))
    for r in g.owner_leads(max_depth, retry_days):
        add(Lead(r["reg"], "owner", r["name"] or "", "", r["depth"], list(r["via"] or [])))
    for r in g.holding_leads(retry_days):
        add(Lead(r["reg"], "holding", r["name"] or "", "", r["depth"], list(r["via"] or [])))
    return out


def _keys(name: str) -> list[str]:
    """The alias keys a company name can be known under (see Graph.load)."""
    name = clean(name)
    return [k for k in dict.fromkeys([entity_key(name, "organization"), entity_key(name)]) if name and k]


def _is_jsc(legal_form: str) -> bool:
    """A listed company is a joint-stock company (Хувьцаат компани, ХК). Unknown counts as yes."""
    f = clean(legal_form).lower()
    return not f or "хувьцаат" in f or f in ("хк", "төхк", "өхк", "jsc")


def _target(lead: Lead, name: str, legal_form: str, lookup: dict, regs: dict) -> tuple[str | None, str]:
    """Which company a page belongs to: (ticker, "") or (None, the company whose name it clashes with).
    A ticker starting with "~" that isn't in `regs` means a new company."""
    if lead.ticker:                 # the number of a company the graph has (unread, or a seed naming it)
        return lead.ticker, ""
    match = next((lookup[k] for k in _keys(name) if k in lookup), None)
    if match is None:
        return "~" + lead.reg, ""
    have = regs.get(match, {})
    if have.get("reg") and have["reg"] != lead.reg:
        return None, match          # another legal entity of that name is in the graph already
    if have.get("listed") and not _is_jsc(legal_form):
        return None, match          # same name as a listed company, but not a joint-stock company
    return match, ""


def weekly_selection(everything: list[tuple[str, str]], discovered: set[str],
                     part: tuple[int, int] | None, today: date | None = None) -> list[tuple[str, str]]:
    """The companies a registry import reads: `part` (a shard) of `everything`, as before,
    but discovered companies at most one seventh at a time, also in a run without a shard
    (the hosted full run). Otherwise every company discovery adds would make each full run
    11 s longer, without end."""
    from . import shard

    key = lambda c: c[1] or c[0]  # noqa: E731
    base = [c for c in everything if c[0] not in discovered]
    found = [c for c in everything if c[0] in discovered]
    weekly = part or shard.parse("auto", today)
    return shard.select(base, part, key=key) + shard.select(found, weekly, key=key)


def discover(g, fetcher=None, budget: int = DEFAULT_BUDGET, max_depth: int = DEFAULT_MAX_DEPTH,
             seeds_path: str | Path | None = None, retry_days: int = DEFAULT_RETRY_DAYS,
             max_failures: int = opendatalab.MAX_CONSECUTIVE_FAILURES, progress=None) -> tuple[dict, Dataset]:
    """Read up to `budget` registry pages from the frontier and load them into `g`.

    Returns (report, everything loaded). The report lists what was `added` (new
    companies), `attached` (pages of companies the graph had, which now have their
    register number), `not_found`, `clashes` and `failed` downloads, plus
    `new_companies`, `new_people` and `frontier_left` (leads still waiting).
    After `max_failures` failed downloads in a row it stops (`stopped`).
    `progress()`, if given, is called after each page (e.g. RegistryLease.keep).
    """
    fetcher = fetcher or opendatalab._fetcher()
    seeds = read_seeds(seeds_path)
    before = g.stats()
    lookup = g.company_lookup()            # alias key / "reg:<no>" -> ticker
    regs = g.company_registers()           # ticker -> {"reg", "listed"}
    report: dict = {"fetched": 0, "added": [], "attached": [], "not_found": [], "clashes": [], "failed": [],
                    "stopped": ""}
    everything = Dataset()
    tried: set[str] = set()
    failures = 0
    while report["fetched"] < budget and not report["stopped"]:
        todo = [lead for lead in frontier(g, max_depth, seeds, retry_days) if lead.reg not in tried]
        # loaded and marked every LOAD_EVERY pages, so a failure loses at most that many
        todo = todo[: min(budget - report["fetched"], LOAD_EVERY)]
        if not todo:
            break
        batch, notes, marks = Dataset(), [], []
        for lead in todo:
            tried.add(lead.reg)
            status, page, url = opendatalab.fetch_entity(lead.reg, fetcher)
            report["fetched"] += 1
            if progress:
                progress()
            n = report["fetched"]
            seen_as = {"reg": lead.reg, "name": lead.name, "kind": lead.kind, "via": lead.via}
            if status == "fetch failed":
                failures += 1
                report["failed"].append({**seen_as, "error": getattr(fetcher, "last_error", "")})
                log.info("DISCOVERY [%d/%d] %s: fetch failed", n, budget, lead.reg)
                if failures >= max_failures:
                    report["stopped"] = (f"{failures} registry pages in a row could not be downloaded; "
                                         "check the route: python -m mse_graph.cli egress-check")
                    break
                continue
            failures = 0
            if status == "not found":
                report["not_found"].append(seen_as)
                notes.append({"reg": lead.reg, "status": "not found", "name": lead.name, "ticker": lead.ticker})
                log.info("DISCOVERY [%d/%d] %s: not found", n, budget, lead.reg)
                continue
            info = page["info"]
            name = clean(info.get("name")) or lead.name or lead.reg
            ticker, clash = _target(lead, name, info.get("legal_form", ""), lookup, regs)
            if ticker is None:
                report["clashes"].append({**seen_as, "registry_name": name, "with": clash})
                notes.append({"reg": lead.reg, "status": "name clash", "name": name, "ticker": clash})
                log.info("DISCOVERY [%d/%d] %s %s: name clash with %s", n, budget, lead.reg, name, clash)
                continue
            ds = opendatalab.registry_dataset(page, ticker, url)
            new = ticker.startswith("~") and ticker not in regs
            if new:
                # the name it was found under links its rows on other pages, unless another company has it
                other = lead.name if lead.name and not any(k in lookup for k in _keys(lead.name)) else ""
                other = other if other and set(_keys(other)) - set(_keys(name)) else ""
                ds.companies.append(Company(ticker=ticker, name=name, reg_no=lead.reg, listed=False, url=url,
                                            other_names=other))
            elif regs.get(ticker, {}).get("reg") != lead.reg:
                ds.facts[0].register_no = lead.reg     # e.g. a listed company: mse.mn gave the 10-digit number
            batch.extend(ds)
            kind = "added" if new else "attached"
            regs[ticker] = {"reg": lead.reg, "listed": regs.get(ticker, {}).get("listed", False)}
            for k in _keys(name) + _keys(lead.name if new else ""):
                lookup.setdefault(k, ticker)
            lookup["reg:" + lead.reg] = ticker
            report[kind].append({"ticker": ticker, "name": name, "reg": lead.reg, "kind": lead.kind,
                                 "depth": lead.depth, "via": lead.via, "url": url,
                                 "shareholders": len(ds.shareholdings), "officers": len(ds.positions),
                                 "beneficial_owners": len(ds.beneficial_owners)})
            marks.append({"ticker": ticker, "via": lead.via, "depth": lead.depth, "kind": kind})
            notes.append({"reg": lead.reg, "status": "ok"})
            log.info("DISCOVERY [%d/%d] %s %s: %s as %s (%d owners, %d officers, %d in its owner chain)",
                     n, budget, lead.reg, name, kind, ticker, len(ds.shareholdings), len(ds.positions),
                     len(ds.beneficial_owners))
        if batch.facts:
            batch.observed_at = date.today().isoformat()
            g.load(batch)
            g.mark_discovered(marks)
            everything.extend(batch)
        g.note_registry_checks(notes)
    after = g.stats()
    report["new_companies"] = len(report["added"])
    report["new_people"] = (after.get("people") or 0) - (before.get("people") or 0)
    report["frontier_left"] = sum(1 for lead in frontier(g, max_depth, seeds, retry_days) if lead.reg not in tried)
    report["listed_without_register_no"] = len(g.listed_without_register_no())
    return report, everything


def summary(report: dict, names: int = 20) -> dict:
    """The report in a few lines, for the pipeline status and the log."""
    out = {k: report[k] for k in ("fetched", "new_companies", "new_people", "frontier_left",
                                  "listed_without_register_no") if k in report}
    out.update({k: len(report[k]) for k in ("attached", "not_found", "clashes", "failed") if report.get(k)})
    if report.get("added"):
        out["added_names"] = [f"{a['name']} ({a['reg']})" for a in report["added"][:names]]
    if report.get("attached"):
        out["attached_names"] = [f"{a['ticker']} {a['name']} ({a['reg']})" for a in report["attached"][:names]]
    if report.get("stopped"):
        out["stopped"] = report["stopped"]
    return out


def save(ds: Dataset, root: str | Path = "data/archive") -> Path | None:
    """Archive what a run loaded (data/archive/<date>/opendatalab-discovery-<time>/), so
    rebuild-history replays it after that day's registry import. Each run gets its own
    directory: unlike a scrape, two runs on one day read different companies."""
    if not ds.facts:
        return None
    ds.observed_at = ds.observed_at or date.today().isoformat()
    stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    return csv_io.archive(ds, root, part=f"opendatalab-discovery-{stamp}")
