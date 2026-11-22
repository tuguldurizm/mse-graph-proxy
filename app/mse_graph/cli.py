"""Command line entry point.

    python -m mse_graph.cli scrape   [--limit N] [--shard K/N|auto] [--out data/scraped]
    python -m mse_graph.cli parse-html <dir> [--out data/scraped]
    python -m mse_graph.cli load     <csv-dir> [--wipe] [--date YYYY-MM-DD] [--notify]
    python -m mse_graph.cli stats
    python -m mse_graph.cli review [--summary]           the entity-resolution review queue (issue #6)
    python -m mse_graph.cli review-pending | review-export   decisions made in the UI -> data/overrides/*.csv
    python -m mse_graph.cli history  <name-or-ticker>      current and past positions / stakes
    python -m mse_graph.cli moves                          people who moved between listed companies
    python -m mse_graph.cli changes  [--since YYYY-MM-DD] [--ticker T] [--all] [--json]
                                                           who joined / left, which stakes changed
    python -m mse_graph.cli ownership-history <TICKER>     shareholder percentages over time
    python -m mse_graph.cli diff     <old-csv-dir> <new-csv-dir>   compare two scrapes, nothing saved
    python -m mse_graph.cli rebuild-history [data/archive] wipe and replay every archived scrape
    python -m mse_graph.cli interlocks | common-owners | board-overlap
    python -m mse_graph.cli owners   <TICKER>
    python -m mse_graph.cli path     <name-or-ticker> <name-or-ticker>
    python -m mse_graph.cli odl-company <REG_NO>
    python -m mse_graph.cli odl-enrich [--from data/scraped] [--limit N] [--shard K/N|auto] [--load]
    python -m mse_graph.cli odl-import <file-or-url> [--kind auto|shareholders|executives|companies] [--load]
    python -m mse_graph.cli odl-discover [--budget N] [--max-depth D] [--dry-run]
                                                           grow the graph: registry pages of companies it lacks
    python -m mse_graph.cli egress-check [--reg REG_NO]   fetch one registry page through each opendatalab.mn route
    python -m mse_graph.cli cycles | cross-holdings | state
    python -m mse_graph.cli conflicts [--kind insider_owner|competitor|related_party]
    python -m mse_graph.cli flags    <TICKER>
    python -m mse_graph.cli concentration [--sort hhi|top1|top3|free_float|ticker] [--json]
                                                           largest holders, free float, HHI per company (#7)
    python -m mse_graph.cli quality [--summary] [--json]   data problems per listed company (#8)
    python -m mse_graph.cli coverage                       how many listed companies each kind of data covers (#27)

Every analysis command ends with a coverage line (on stderr for the JSON ones, so stdout
stays JSON): how many of the listed companies its result rests on (issue #27).

Set MSE_PROXY_URL / MSE_PROXY_TOKEN to route all downloads through the fetch proxy,
and ODL_PROXY_URL to send opendatalab.mn requests through a proxy of their own
(see mse_graph/egress.py).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

from . import csv_io


def _graph():
    from .graph import Graph
    return Graph()


def _resolve(g, q: str) -> str:
    hits = g.search(q, limit=1)
    if not hits:
        sys.exit(f"No entity matching {q!r}")
    return hits[0]["key"]


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


SHARD_HELP = "only slice K/N of the companies, or 'auto' = today's slice of 7 (all covered in a week)"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mse_graph")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scrape", help="crawl mse.mn and write CSVs")
    p.add_argument("--limit", type=int)
    p.add_argument("--shard", help=SHARD_HELP)
    p.add_argument("--out", default="data/scraped")
    p.add_argument("--archive", default="data/archive", help="dated archive root ('' to skip)")
    p.add_argument("--load", action="store_true", help="also load into Neo4j")

    p = sub.add_parser("parse-html", help="parse saved company HTML pages into CSVs")
    p.add_argument("dir")
    p.add_argument("--out", default="data/scraped")

    p = sub.add_parser("load", help="load a CSV directory into Neo4j")
    p.add_argument("dir")
    p.add_argument("--wipe", action="store_true", help="delete the existing graph (and its history) first")
    p.add_argument("--date", help="observation date to record (default: the data's manifest, else today)")
    p.add_argument("--notify", action="store_true",
                   help="print what this load changed and send significant changes to the alert channels")
    p = sub.add_parser("history", help="current and past positions / stakes of a person or company")
    p.add_argument("q")
    sub.add_parser("moves", help="people who used to work at one listed company and now at another")
    p = sub.add_parser("load-manual",
                       help="load the hand-curated datasets in data/manual/ (only new or changed ones)")
    p.add_argument("root", nargs="?", help="default: $MANUAL_DATA_DIR, else data/manual")
    p.add_argument("--force", action="store_true", help="load every dataset even if it is unchanged")
    p = sub.add_parser("validate-manual",
                       help="compare data/manual/ datasets with the Open Data Lab registry data in the graph")
    p.add_argument("root", nargs="?", help="default: $MANUAL_DATA_DIR, else data/manual")
    p.add_argument("--summary", action="store_true", help="counts only, no per-company details")
    p = sub.add_parser("group-people",
                       help="people of the data/manual/ groups and the other companies the graph links them to")
    p.add_argument("root", nargs="?", help="default: $MANUAL_DATA_DIR, else data/manual")
    p.add_argument("--dataset", help="one group, e.g. ochir-undraa")
    p.add_argument("--past", action="store_true", help="include past stakes and posts elsewhere")
    p.add_argument("--summary", action="store_true", help="counts and company lists only")
    p = sub.add_parser("rebuild-history", help="wipe the graph and replay every archived scrape in order")
    p.add_argument("root", nargs="?", default="data/archive")

    sub.add_parser("stats")
    p = sub.add_parser("review", help="the entity-resolution review queue (issue #6), as JSON")
    p.add_argument("--summary", action="store_true", help="counts only")
    sub.add_parser("review-pending", help="review decisions in the graph that the overrides files lack")
    sub.add_parser("review-export",
                   help="write the review decisions into data/overrides/identity.csv and reviewed.csv")
    sub.add_parser("interlocks")
    sub.add_parser("common-owners")
    sub.add_parser("nominees", help="custodians and other nominee holders, stakes by holding type, candidates (#39)")
    sub.add_parser("board-overlap")
    sub.add_parser("cycles", help="ownership loops (A owns B owns ... owns A)")
    sub.add_parser("cross-holdings", help="listed companies holding shares in listed companies")
    sub.add_parser("state", help="companies with state / municipal owners, direct and look-through")
    p = sub.add_parser("conflicts", help="insider owners, same-sector ties, related parties")
    p.add_argument("--kind", choices=["insider_owner", "competitor", "related_party"])
    p.add_argument("--min-stake", type=float, default=5.0, help="stake that counts as influence (%%)")
    p = sub.add_parser("concentration", help="largest holders, free-float estimate, HHI per company (#7)")
    p.add_argument("--sort", default="hhi", choices=["hhi", "top1", "top3", "free_float", "ticker"])
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("quality", help="data-quality report: problems per listed company (#8)")
    p.add_argument("--summary", action="store_true", help="only the number of companies per problem")
    p.add_argument("--json", action="store_true")
    sub.add_parser("coverage", help="how many listed companies each kind of data covers (#27)")
    p = sub.add_parser("flags", help="governance flags for one company")
    p.add_argument("ticker")
    p = sub.add_parser("owners", help="look-through (ultimate) owners of a company")
    p.add_argument("ticker")
    p = sub.add_parser("path", help="shortest connection between two entities")
    p.add_argument("a")
    p.add_argument("b")

    p = sub.add_parser("odl-company", help="show one company's Open Data Lab registry page, parsed")
    p.add_argument("reg_no")
    p.add_argument("--raw", action="store_true", help="print every parsed section, not just graph records")
    p = sub.add_parser("odl-enrich",
                       help="add Open Data Lab registry data (owners, officers, UBOs) for companies with a reg. no.")
    p.add_argument("--from", dest="src", metavar="CSV_DIR",
                   help="take companies from this CSV directory (default: from Neo4j)")
    p.add_argument("--limit", type=int)
    p.add_argument("--shard", help=SHARD_HELP)
    p.add_argument("--out", default="data/opendatalab")
    p.add_argument("--load", action="store_true", help="also load into Neo4j")
    p = sub.add_parser("odl-import", help="import an Open Data Lab dataset file (CSV/XLSX/JSON)")
    p.add_argument("src", help="local path or URL")
    p.add_argument("--kind", default="auto", choices=["auto", "shareholders", "executives", "companies"])
    p.add_argument("--map", action="append", default=[], metavar="FIELD=Header",
                   help="override column mapping, e.g. --map holder_name='Эзэмшигч'")
    p.add_argument("--out", default="data/opendatalab")
    p.add_argument("--load", action="store_true", help="also load into Neo4j")
    p = sub.add_parser("odl-discover",
                       help="grow the graph from the registry: read the pages of companies it doesn't have yet "
                            "(owners named in ownership chains, data/discovery/seeds.csv, companies never read)")
    p.add_argument("--budget", type=int, help="registry pages to read, 11 s each (default DISCOVERY_BUDGET or 25)")
    p.add_argument("--max-depth", type=int,
                   help="steps along ownership chains from the starting companies (default DISCOVERY_MAX_DEPTH or 3)")
    p.add_argument("--seeds", help="seeds file (default DISCOVERY_SEEDS or data/discovery/seeds.csv)")
    p.add_argument("--dry-run", action="store_true", help="only list what would be read; fetch nothing")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("egress-check",
                       help="show how requests leave this server; fetch one Open Data Lab registry page "
                            "through each opendatalab.mn route (one request per route, 11 s apart)")
    p.add_argument("--reg", default="2008572", help="registration number of the page to fetch (default %(default)s)")

    p = sub.add_parser("changes", help="who joined / left and which stakes changed, newest first")
    p.add_argument("--ticker")
    p.add_argument("--source", help="mse, opendatalab, ...")
    p.add_argument("--since", help="YYYY-MM-DD (default: all)")
    p.add_argument("--all", action="store_true", help="include small stake moves")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("ownership-history", help="shareholder percentages of a company over time")
    p.add_argument("ticker")
    p = sub.add_parser("diff", help="compare two CSV dirs without saving anything")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")

    if args.cmd in ("scrape", "parse-html"):
        from . import scraper
        if args.cmd == "scrape":
            # same crawler as the pipeline: mse.mn company list + open.mse.mn profiles
            from . import shard
            part = shard.parse(args.shard)
            if os.getenv("MSE_SOURCE") == "legacy":
                if part:
                    sys.exit("--shard needs the default open.mse.mn crawler (unset MSE_SOURCE)")
                ds = scraper.crawl(limit=args.limit)
            else:
                ds = scraper.crawl_open(limit=args.limit, shard=part)
        else:
            ds = scraper.parse_html_dir(args.dir)
        csv_io.write_dir(ds, args.out)
        print(f"wrote {args.out}: {ds.summary()}")
        if getattr(args, "archive", "") and ds.observed_at:
            print(f"archived to {csv_io.archive(ds, args.archive)}")
        if getattr(args, "load", False):
            with _graph() as g:
                g.drop_demo_data()
                _print(g.load(ds))
        return

    if args.cmd == "diff":
        from . import history
        _print_changes(history.diff(csv_io.read_dir(args.old), csv_io.read_dir(args.new)), args.json)
        return

    if args.cmd == "odl-company":
        from . import opendatalab
        page, url = opendatalab.fetch_registry_page(args.reg_no)
        if page is None:
            sys.exit(f"no registry page for {args.reg_no} ({url})")
        if args.raw:
            _print(page)
        else:
            ds = opendatalab.registry_dataset(page, page["info"].get("reg_no", args.reg_no), url)
            _print({"url": url, "info": page["info"],
                    "shareholders": [s.__dict__ for s in ds.shareholdings],
                    "officers": [p.__dict__ for p in ds.positions],
                    "beneficial_owners": [b.__dict__ for b in ds.beneficial_owners],
                    "other_sections": {k: len(v) for k, v in page.items()
                                       if k not in ("info", "shareholders", "officers", "beneficial_owners")}})
        return

    if args.cmd == "odl-enrich":
        from . import opendatalab
        g = _graph() if (args.load or not args.src) else None
        lease = opendatalab.RegistryLease(g, "odl-enrich") if g else None
        # opendatalab.mn is never crawled in parallel (the hosted pipeline may be reading it)
        if lease and not lease.acquire():
            g.close()
            sys.exit(f"{lease.busy_message()}; try again later")
        try:
            if args.src:
                companies = [(c.ticker, c.reg_no) for c in csv_io.read_dir(args.src).companies]
            else:
                companies = g.companies_with_reg()
            from . import shard
            part = shard.parse(args.shard)
            if part:
                from .manual import with_registry_tickers
                everything = companies
                companies = shard.select(companies, part, key=lambda c: c[1] or c[0])
                print(f"shard {part[0]}/{part[1]}: {len(companies)} companies")
                # the companies of data/manual/ are re-checked on every run
                companies = with_registry_tickers(companies, everything)
            print(f"{sum(1 for _, r in companies if opendatalab.is_register_no(r))} companies have a register "
                  "number the registry uses")
            ds, report = opendatalab.enrich_companies(companies, limit=args.limit,
                                                      progress=lease.keep if lease else None)
            for e in report:
                print(f"{e['ticker']:<8} {e['reg_no']:<9} {e['status']:<10} "
                      f"{e.get('shareholders', '')!s:>3} sh {e.get('officers', '')!s:>3} off "
                      f"{e.get('beneficial_owners', '')!s:>3} ubo  {e['url']}")
            if g:
                # numbers the registry doesn't know are left alone for a while (see odl-discover)
                g.note_registry_checks([{"reg": e["reg_no"], "status": e["status"], "ticker": e["ticker"]}
                                        for e in report])
            ds.observed_at = ds.observed_at or date.today().isoformat()
            csv_io.write_dir(ds, args.out)
            print(f"wrote {args.out}: {ds.summary()}")
            print(f"archived to {csv_io.archive(ds, part='opendatalab')}")
            if g and args.load:
                _print(g.load(ds))
        finally:
            if g:
                lease.release()
                g.close()
        return

    if args.cmd == "odl-discover":
        _discover_cmd(args)
        return

    if args.cmd == "egress-check":
        from . import egress, opendatalab
        from .config import settings
        from .scraper import Fetcher

        fetcher = Fetcher()
        try:
            print(f"{'mse.mn':<16} -> {fetcher.router.describe(settings.mse_base_url)}")
            print(f"{'opendatalab.mn':<16} -> {fetcher.router.describe(settings.odl_base_url)}")
            print(f"fetching {opendatalab.registry_url(args.reg)} through each opendatalab.mn route:")
            results = opendatalab.check_egress(args.reg, fetcher)
        except egress.EgressConfigError as exc:
            sys.exit(f"error: {exc}")
        for r in results:
            status = f"HTTP {r['status']}" if r["status"] else "-"
            print(f"  {'OK' if r['ok'] else 'FAIL':<5} {r['route']:<48} {status:<9} {r['seconds']:>6.2f} s  "
                  f"{r['error'] or 'registry page for ' + args.reg}")
        sys.exit(0 if any(r["ok"] for r in results) else 1)

    if args.cmd == "odl-import":
        from . import opendatalab
        columns = dict(m.split("=", 1) for m in args.map)
        g = _graph() if args.load else None
        try:
            lookup = g.company_lookup() if g else None
            ds = opendatalab.import_table(args.src, kind=args.kind, lookup=lookup, columns=columns)
            csv_io.write_dir(ds, args.out)
            print(f"wrote {args.out}: {ds.summary()}")
            if g:
                _print(g.load(ds))
        finally:
            if g:
                g.close()
        return

    with _graph() as g:
        if args.cmd == "load":
            ds = csv_io.read_dir(args.dir)
            if args.wipe:
                g.wipe(keep_decisions=True)     # review decisions (issue #6) outlive a wipe
            elif not csv_io.is_demo(ds) and g.drop_demo_data():
                print("removed the demo data before loading real data", file=sys.stderr)
            res = g.load(ds, observed_at=args.date)
            if not args.notify:
                _print(res)
            else:
                from . import history, notify
                changes = history.run_changes(g, res["run"], ds)
                print(f"{len(changes)} changes", file=sys.stderr)
                _print_changes(changes)
                r = notify.send(changes)
                print(f"alerts: {r['sent']} sent via {', '.join(r['channels']) or 'no channel configured'}"
                      + (f"; errors: {r['errors']}" if r["errors"] else ""), file=sys.stderr)
        elif args.cmd == "review":
            from . import review
            q = review.queue(g)
            _print(q["counts"] if args.summary else q)
        elif args.cmd == "review-pending":
            from . import review
            _print(review.pending(g))
        elif args.cmd == "review-export":
            from . import review
            _print(review.export(g))
        elif args.cmd == "load-manual":
            from .manual import load_manual
            _print(load_manual(g, args.root, force=args.force))
        elif args.cmd == "validate-manual":
            from .manual import validate_manual
            rep = validate_manual(g, args.root)
            _print([{k: v for k, v in r.items() if k != "details"} for r in rep] if args.summary else rep)
        elif args.cmd == "group-people":
            from .manual import group_people
            rep = group_people(g, args.root, dataset=args.dataset, past=args.past)
            _print([{k: v for k, v in r.items() if k != "details"} for r in rep] if args.summary else rep)
        elif args.cmd == "rebuild-history":
            dirs = csv_io.archived_snapshots(args.root)
            if not dirs:
                sys.exit(f"no archived scrapes under {args.root}")
            g.wipe(keep_decisions=True)
            for d in dirs:
                ds = csv_io.read_dir(d)
                # a sub-directory (e.g. 2026-11-21/opendatalab) belongs to its parent's date
                when = ds.observed_at or (d.parent.name if d.parent != Path(args.root) else d.name)
                res = g.load(ds, observed_at=when)
                print(f"{d}: {res['changes']}", file=sys.stderr)
            from .manual import load_manual
            for r in load_manual(g, force=True):
                print(f"manual dataset {r['dataset']}: {r['status']}", file=sys.stderr)
            _print(g.stats())
        elif args.cmd == "history":
            e = g.entity(_resolve(g, args.q))
            _print({"entity": e["entity"],
                    "current": {k: e[k] for k in ("owners", "holdings", "people", "positions")},
                    "past": e["history"]})
        elif args.cmd == "moves":
            _print(g.moves())
            _coverage(g, "moves")
        elif args.cmd in ("changes", "ownership-history"):
            _history_cmd(g, args)
        elif args.cmd == "stats":
            _print(g.stats())
        elif args.cmd == "interlocks":
            _print(g.interlocks())
            _coverage(g, "interlocks")
        elif args.cmd == "common-owners":
            _print(g.common_owners())
            _coverage(g, "common-owners")
        elif args.cmd == "nominees":
            from . import analysis
            _print(analysis.nominees(g))
            _coverage(g, "nominees")
        elif args.cmd == "board-overlap":
            _print(g.board_overlap())
            _coverage(g, "board-overlap")
        elif args.cmd in ("concentration", "quality", "coverage"):
            _quality_cmd(g, args)
        elif args.cmd in ("cycles", "cross-holdings", "state", "conflicts", "flags"):
            from . import analysis
            if args.cmd == "cycles":
                for c in analysis.cycles(g):
                    ring = [n["ticker"] or n["name"] for n in c["companies"]]
                    pcts = " → ".join(f"{a} ({p:g}%)" if p is not None else a
                                      for a, p in zip(ring, c["percents"]))
                    loop = f"   loop {c['loop_percent']:g}%" if c["loop_percent"] is not None else ""
                    print(f"{pcts} → {ring[0]}{loop}")
            elif args.cmd == "cross-holdings":
                for x in analysis.cross_holdings(g):
                    back = f"   (reverse {x['reverse_percent']:g}%)" if x["reverse_percent"] is not None else ""
                    pct = f"{x['percent']:g}%" if x["percent"] is not None else "?"
                    print(f"{x['holder']:<6} owns {pct:>7} of {x['company']}{back}")
            elif args.cmd == "state":
                for s in analysis.state_ownership(g):
                    print(f"{s['ticker'] or s['name']:<8} direct {s['direct_percent']:>7g}%"
                          f"   look-through {s['lookthrough_percent']:>7g}%")
            elif args.cmd == "conflicts":
                _print(analysis.conflicts(g, args.kind, args.min_stake))
            else:
                for f in analysis.company_flags(g, args.ticker):
                    print(f"{'!' if f['level'] == 'warn' else '-'} {f['text']}")
            if args.cmd != "flags":
                _coverage(g, {"state": "state-ownership"}.get(args.cmd, args.cmd),
                          sys.stderr if args.cmd == "conflicts" else sys.stdout)
        elif args.cmd == "owners":
            _print(g.ultimate_owners(args.ticker))
        elif args.cmd == "path":
            _print(g.shortest_path(_resolve(g, args.a), _resolve(g, args.b)))


def _coverage(g, analysis: str, out=None) -> None:
    """The coverage line of an analysis (issue #27). JSON commands print it on stderr."""
    from . import quality
    print(f"coverage: {quality.coverage(g, analysis)['text']}", file=out or sys.stderr)


def _quality_cmd(g, args) -> None:
    from . import quality
    if args.cmd == "coverage":
        cov = quality.coverage(g)
        for p in cov["parts"]:
            print(f"{p['companies']:>4}/{p['of']:<4} {p['basis']}")
        return
    if args.cmd == "concentration":
        res = quality.concentration(g, args.sort)
        if args.json:
            _print(res)
            print(f"coverage: {res['coverage']['text']}", file=sys.stderr)
            return
        f = lambda v: "-" if v is None else f"{v:g}"
        print(f"{'ticker':<8} {'HHI':>7} {'top1':>6} {'top3':>6} {'float~':>6}  largest holder / look-through")
        for c in res["items"]:
            big = f"{c['largest']['name']} {c['largest']['percent']:g}%" if c["largest"] else "-"
            lt = c["largest_lookthrough"]
            if lt and (not c["largest"] or lt["key"] != c["largest"]["key"]):
                big += f"  /  {lt['name']} {lt['percent']:g}%"
            bad = "" if c["plausible"] else f"  (!) sums to {c['total_percent']:g}%"
            print(f"{c['ticker'] or '':<8} {c['hhi']:>7g} {f(c['top1']):>6} {f(c['top3']):>6} "
                  f"{f(c['free_float_estimate']):>6}  {big}{bad}")
        s = res["summary"]
        print(f"{s['companies']} companies, {s['controlled']} controlled (look-through >= 50%), "
              f"median HHI {f(s['median_hhi'])}, median free float ~{f(s['median_free_float'])}%")
        print(f"coverage: {res['coverage']['text']}")
        return
    rep = quality.report(g)
    if args.json:
        _print({k: v for k, v in rep.items() if k != "companies"} if args.summary else rep)
        print(f"coverage: {rep['coverage']['text']}", file=sys.stderr)
        return
    print(f"{rep['of']} listed companies: {rep['levels']['error']} with errors, {rep['levels']['warn']} with "
          f"warnings, {rep['levels']['info']} with notes only, {rep['clean']} clean")
    for code, v in sorted(rep["summary"].items(), key=lambda kv: -kv[1]["companies"]):
        if v["companies"]:
            print(f"  {v['level']:<5} {v['companies']:>4}  {code}")
    if not args.summary:
        for c in rep["companies"]:
            print(f"{c['ticker'] or c['key']}  {c['name']}")
            for i in c["issues"]:
                print(f"    {i['level']:<5} {i['code']}: {i['text']}")
    print(f"coverage: {rep['coverage']['text']}")


def _print_changes(changes, as_json=False):
    from .notify import describe
    if as_json:
        _print(changes)
        return
    if not changes:
        print("no changes")
        return
    for c in changes:
        when = c.get("detected_at", "")[:10]
        print(f"{when:<10} {c['ticker']:<6} {describe(c)}")


def _discover_cmd(args):
    from . import discovery

    opts = discovery.settings_from_env()
    budget = opts["budget"] if args.budget is None else args.budget
    max_depth = opts["max_depth"] if args.max_depth is None else args.max_depth
    seeds_path = args.seeds or opts["seeds_path"]
    with _graph() as g:
        if args.dry_run:
            leads = discovery.frontier(g, max_depth, discovery.read_seeds(seeds_path), opts["retry_days"])
            missing = g.listed_without_register_no()
            if args.json:
                _print({"frontier": [lead.__dict__ for lead in leads], "listed_without_register_no": missing})
                return
            print(f"{len(leads)} registry numbers to read; the next run reads up to {budget}:")
            for lead in leads[:max(budget, 50)]:
                via = f" via {','.join(lead.via[:5])}" if lead.via else ""
                to = f" -> {lead.ticker}" if lead.ticker else ""
                print(f"  {lead.reg:<9} {lead.kind:<6} depth {lead.depth}  {lead.name}{to}{via}")
            print(f"{len(missing)} listed companies have no register number, so the registry can't be read "
                  f"for them yet: {' '.join(m['ticker'] for m in missing)}")
            print(f"A number found elsewhere goes in {seeds_path} (reg_no,ticker,note).")
            return
        if budget <= 0:
            print("discovery is off (budget 0)")
            return
        from .opendatalab import RegistryLease
        lease = RegistryLease(g, "odl-discover")
        if not lease.acquire():
            # opendatalab.mn is never crawled in parallel; the next run picks up where this one would have
            print(f"discovery skipped: {lease.busy_message()}")
            return
        try:
            report, ds = discovery.discover(g, budget=budget, max_depth=max_depth, seeds_path=seeds_path,
                                            retry_days=opts["retry_days"], progress=lease.keep)
        finally:
            lease.release()
        path = discovery.save(ds)
    if args.json:
        _print(report)
    else:
        for a in report["added"]:
            print(f"added     {a['ticker']:<10} {a['name']}  depth {a['depth']}"
                  f"{'  via ' + ','.join(a['via'][:5]) if a['via'] else ''}  "
                  f"({a['shareholders']} owners, {a['officers']} officers, {a['beneficial_owners']} in owner chain)")
        for a in report["attached"]:
            print(f"attached  {a['ticker']:<10} {a['name']}  register no. {a['reg']}"
                  f"{'  found via ' + ','.join(a['via'][:5]) if a['via'] else ''}  "
                  f"({a['shareholders']} owners, {a['officers']} officers, {a['beneficial_owners']} in owner chain)")
        for c in report["clashes"]:
            print(f"clash     {c['reg']:<10} {c['registry_name']}: {c['with']} already has that name")
        for n in report["not_found"]:
            print(f"not found {n['reg']:<10} {n['name']}")
        for f in report["failed"]:
            print(f"failed    {f['reg']:<10} {f['error']}")
        print(f"read {report['fetched']} registry pages: {report['new_companies']} new companies, "
              f"{len(report['attached'])} companies completed, {report['new_people']} new people; "
              f"{report['frontier_left']} leads left for later runs")
        if path:
            print(f"archived to {path}")
        if report["stopped"]:
            print(f"stopped: {report['stopped']}", file=sys.stderr)
    nothing = not (report["added"] or report["attached"] or report["not_found"] or report["clashes"])
    sys.exit(1 if report["stopped"] or (report["failed"] and nothing) else 0)


def _history_cmd(g, args):
    from . import history

    if args.cmd == "changes":
        _print_changes(history.recent_changes(g, source=args.source, ticker=args.ticker, since=args.since,
                                              significant_only=not args.all, limit=5000), args.json)
        return
    tl = history.ownership_timeline(g, args.ticker)
    if not tl["points"]:
        sys.exit(f"no shareholders recorded for {args.ticker.upper()}")
    dates = [p["taken_at"] for p in tl["points"]]          # when the scraper saw each state
    width = max(len(h["name"]) for h in tl["holders"])
    print(" " * width + "  " + "  ".join(f"{d:>10}" for d in dates))
    for h in tl["holders"]:
        cells = "  ".join(f"{'—' if v is None else f'{v:g}%':>10}" for v in h["values"])
        print(f"{h['name']:<{width}}  {cells}")


if __name__ == "__main__":
    main()
