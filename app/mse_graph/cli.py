"""Command line entry point.

    python -m mse_graph.cli scrape   [--limit N] [--shard K/N|auto] [--out data/scraped]
    python -m mse_graph.cli parse-html <dir> [--out data/scraped]
    python -m mse_graph.cli load     <csv-dir> [--wipe] [--date YYYY-MM-DD] [--notify]
    python -m mse_graph.cli stats
    python -m mse_graph.cli review [--summary]           the entity-resolution review queue (issue #6)
    python -m mse_graph.cli review-pending | review-export   decisions made in the UI -> data/overrides/*.csv
    python -m mse_graph.cli relations [--summary]        possible family links from patronymics (#32, hypotheses)
    python -m mse_graph.cli officials [--summary] [--min-companies N] [--listed] [--matches]
                                                           officials whose asset declarations name a company (#9)
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
    python -m mse_graph.cli odl-discover [--budget N|auto] [--max-depth D] [--dry-run]
                                                           grow the graph: registry pages of companies it lacks
    python -m mse_graph.cli odl-holders [--minutes M] [--quota auto|all|N] [--key KEY] [--dry-run]
                                                           search each holder's other companies (a 7th a day)
    python -m mse_graph.cli egress-check [--reg REG_NO]   fetch one registry page through each opendatalab.mn route
    python -m mse_graph.cli cycles | cross-holdings | state
    python -m mse_graph.cli conflicts [--kind insider_owner|competitor|related_party]
    python -m mse_graph.cli flags    <TICKER>
    python -m mse_graph.cli concentration [--sort hhi|top1|top3|free_float|ticker] [--json]
                                                           largest holders, free float, HHI per company (#7)
    python -m mse_graph.cli quality [--summary] [--json]   data problems per listed company (#8)
    python -m mse_graph.cli coverage                       how many listed companies each kind of data covers (#27)
    python -m mse_graph.cli groups [--owner NAME] [--min-companies N] [--listed] [--json]
                                                           who controls / influences which companies (#28)
    python -m mse_graph.cli communities [--write] [--json] detected groups and their score against the
                                                           hand-curated groups (#28); --write sets group_id
    python -m mse_graph.cli centrality [--kind all|person|company|organization] [--by betweenness|pagerank|degree]
                                       [--limit N] [--write] [--json]
                                                           brokers and influential owners (#29); --write sets centrality_*
    python -m mse_graph.cli turnover [--sort rate|departures|tenure|ceo_tenure|ticker] [--ticker T] [--json]
                                                           officers who arrived / left, tenure, CEO tenure (#30)
    python -m mse_graph.cli accumulation [--json]          holders whose stake keeps rising, thresholds crossed (#30)
    python -m mse_graph.cli trends [--every auto|day|week|month] [--json]
                                                           market time series: state, interlocks, HHI (#30)
    python -m mse_graph.cli market [--fetch [--limit N]] [--sort cap|price_date|ticker] [--ticker T] [--json]
                                                           prices and market caps (#31); --fetch reads mse.mn first

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
    p.add_argument("--compare", action="store_true",
                   help="counts and what changed since the last saved result, then save this one in the graph")
    p = sub.add_parser("prune-listed-registry-owners",
                       help="remove stakes that only the registry reports from listed companies with a long "
                            "registry list (its share register, e.g. Хаан банк's 4,105 small holders); "
                            "counts only without --apply")
    p.add_argument("--apply", action="store_true", help="delete them (and records/people left unlinked)")
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
                   help="write the review decisions into data/overrides/identity.csv, reviewed.csv and relations.csv")
    p = sub.add_parser("relations", help="possible family links from registry patronymics (issue #32): "
                                         "hypotheses for review, and the confirmed links")
    p.add_argument("--summary", action="store_true", help="counts only")
    p.add_argument("--min-score", type=float, default=0.05)
    p = sub.add_parser("officials", help="public officials whose asset declarations name a company, from the "
                                         "Open Data Lab registry pages (issue #9): disclosed interests, not findings")
    p.add_argument("--summary", action="store_true", help="counts only (officials, interests, licences, tenders)")
    p.add_argument("--min-companies", type=int, default=1)
    p.add_argument("--listed", action="store_true", help="only officials linked to a listed company")
    p.add_argument("--matches", action="store_true",
                   help="people at the same company whose name fits an official's (hypotheses for review)")
    p.add_argument("--limit", type=int, default=None)
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
    p = sub.add_parser("groups", help="control groups: what each top owner controls or influences (#28)")
    p.add_argument("--owner", help="one owner (name, ticker or key), also one that is itself controlled")
    p.add_argument("--min-companies", type=int, default=1)
    p.add_argument("--listed", action="store_true", help="only groups with a listed company")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("communities", help="detected groups (Louvain) and their score against data/manual, data/mcs (#28)")
    p.add_argument("--write", action="store_true", help="write group_id onto the nodes")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("centrality", help="brokers (betweenness), influential owners (PageRank), degree (#29)")
    p.add_argument("--kind", default="all", choices=["all", "person", "company", "organization"])
    p.add_argument("--by", default="betweenness", choices=["betweenness", "pagerank", "degree"])
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--write", action="store_true", help="write centrality_* onto the nodes")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("turnover", help="officer turnover, tenure and CEO tenure per listed company (#30)")
    p.add_argument("--sort", default="rate", choices=["rate", "departures", "tenure", "ceo_tenure", "ticker"])
    p.add_argument("--ticker", help="one company, with its arrivals and departures")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("accumulation", help="holders whose stake rose over several scrapes (#30)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("trends", help="market-wide time series rebuilt from the edge periods (#30)")
    p.add_argument("--every", default="auto", choices=["auto", "day", "week", "month"])
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("market", help="share prices and market caps from mse.mn (#31)")
    p.add_argument("--fetch", action="store_true", help="read today's quotes from mse.mn and store them first")
    p.add_argument("--limit", type=int, help="with --fetch: only the first N companies")
    p.add_argument("--sort", default="cap", choices=["cap", "price_date", "ticker"])
    p.add_argument("--ticker", help="one company, with its price series")
    p.add_argument("--top", type=int, default=30, help="rows to print (0 = all)")
    p.add_argument("--json", action="store_true")
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
    p.add_argument("--budget", help="registry pages to read, 11 s each, or \"auto\": the waiting leads spread "
                                    "over a week, 25 to DISCOVERY_MAX_BUDGET (300) (default DISCOVERY_BUDGET or 25)")
    p.add_argument("--max-depth", type=int,
                   help="steps along ownership chains from the starting companies (default DISCOVERY_MAX_DEPTH or 3)")
    p.add_argument("--seeds", help="seeds file (default DISCOVERY_SEEDS or data/discovery/seeds.csv)")
    p.add_argument("--dry-run", action="store_true", help="only list what would be read; fetch nothing")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("odl-holders",
                       help="search the registry for the other companies each person and organisation in the "
                            "graph owns; what it finds becomes discovery leads (holder_search.py)")
    p.add_argument("--minutes", type=float,
                   help="time budget, 11 s a request (default HOLDER_SEARCH_MINUTES or 60; 0 = no limit)")
    p.add_argument("--quota", help="holders to search: auto (a seventh of all, the default), all, or a number")
    p.add_argument("--every-days", type=int, help="search each holder again after this many days (default 7)")
    p.add_argument("--key", action="append", help="search this holder (graph key, e.g. 'person:батын батболд'); "
                                                  "repeatable; due or not")
    p.add_argument("--dry-run", action="store_true", help="only list who would be searched; fetch nothing")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("egress-check",
                       help="show how requests leave this server; fetch one Open Data Lab registry page "
                            "through each opendatalab.mn route (one request per route, 11 s apart)")
    p.add_argument("--reg", default="2008572", help="registration number of the page to fetch (default %(default)s)")

    p = sub.add_parser("ask", help="ask the graph a question in plain Mongolian or English (#10); answered by a "
                                   "fixed set of read-only queries")
    p.add_argument("question", nargs="+")
    p = sub.add_parser("frc", help="import FRC's licensed entities (frc.mn port 5001; needs open egress, not a cloud "
                                   "session): lists, licences, register numbers, decisions (people: Open Data Lab)")
    p = sub.add_parser("frc-decisions", help="the FRC's decisions with the companies they name (adverse only "
                                             "unless --all)")
    p.add_argument("--all", action="store_true")
    p.add_argument("--since", default="", help="YYYY-MM-DD")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("frc-probe",
                       help="describe FRC's data API (frc.mn, port 5001): the menu and the disclosure-like listings, "
                            "as 'FRC layout' lines; needs open egress (not a cloud session)")
    p.add_argument("--max-requests", type=int, help="at most this many requests (default FRC_MAX_REQUESTS or 15)")

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

    if args.cmd == "frc-probe":
        from . import frc
        logging.getLogger("mse_graph.frc").setLevel(logging.INFO)
        print(json.dumps(frc.probe(max_requests=args.max_requests), ensure_ascii=False, indent=2))
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

    if args.cmd == "odl-holders":
        _holders_cmd(args)
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
            from . import relations, review
            _print({**review.pending(g), "relations": relations.pending(g)})
        elif args.cmd == "review-export":
            from . import review
            _print(review.export(g))
        elif args.cmd == "relations":
            from . import relations
            _print(relations.summary(g) if args.summary else relations.review(g, args.min_score))
        elif args.cmd == "officials":
            from . import officials
            if args.summary:
                _print(officials.counts(g))
            elif args.matches:
                _print(officials.possible_people(g, args.limit or 500))
            else:
                _print(officials.report(g, args.min_companies, args.listed, args.limit))
        elif args.cmd == "load-manual":
            from .manual import load_manual
            _print(load_manual(g, args.root, force=args.force))
        elif args.cmd == "validate-manual":
            from .manual import validate_manual
            rep = validate_manual(g, args.root)
            if args.compare:
                from .manual import compare_validation
                _print(compare_validation(g, rep))
            else:
                _print([{k: v for k, v in r.items() if k != "details"} for r in rep] if args.summary else rep)
        elif args.cmd == "prune-listed-registry-owners":
            _print(g.drop_registry_owners_of_listed(apply=args.apply))
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
        elif args.cmd in ("groups", "communities"):
            _groups_cmd(g, args)
        elif args.cmd == "centrality":
            _centrality_cmd(g, args)
        elif args.cmd in ("turnover", "accumulation", "trends"):
            _trends_cmd(g, args)
        elif args.cmd == "market":
            _market_cmd(g, args)
        elif args.cmd == "frc":
            from . import frc
            logging.getLogger("mse_graph.frc").setLevel(logging.INFO)
            res = frc.step(g)
            frc.mark_step(g, res)
            print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        elif args.cmd == "frc-decisions":
            from . import frc
            res = frc.decisions_report(g, not args.all, args.since)
            if args.json:
                print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
            else:
                print(json.dumps(res["summary"], ensure_ascii=False))
                for d in res["items"]:
                    cos = ", ".join(c["ticker"] if not c["ticker"].startswith("~") else c["name"] for c in d["companies"])
                    print(f"{d['date']} №{d['number'] or '-':<6} {d['kind']:<14} {cos or '(' + '; '.join(d['named'] or []) + ')'}")
        elif args.cmd == "ask":
            from . import ask
            print(json.dumps(ask.answer(g, " ".join(args.question)), ensure_ascii=False, indent=2, default=str))
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


def _groups_cmd(g, args) -> None:
    """Control groups and detected groups (issue #28, groups.py)."""
    from . import groups
    f = lambda v: "-" if v is None else f"{v:g}"
    if args.cmd == "communities":
        found = groups.detect(g)
        score = groups.curated_score(g, found)
        written = groups.write_communities(g, found) if args.write else None
        if args.json:
            _print({"communities": found, "curated": score, "written": written})
            _coverage(g, "communities")
            return
        for c in found:
            names = ", ".join(m["ticker"] if m["ticker"] and not m["ticker"].startswith("~") else m["name"]
                              for m in c["members"][:8])
            print(f"{c['id']:<34} {c['size']:>4} nodes {c['companies']:>3} companies {c['listed']:>3} listed  {names}")
        print(f"{len(found)} groups")
        print("hand-curated groups recovered (share of their companies in one detected group):")
        for s in score:
            print(f"  {s['dataset']:<14} {f(s['score']):>6}  {s['in_main_group']}/{s['in_graph']} in {s['group'] or '-'}"
                  f" ({s['groups']} groups, {s['companies']} companies in the dataset)")
        if written:
            print(f"group_id written: {written['nodes']} nodes in {written['communities']} groups")
        _coverage(g, "communities", sys.stdout)
        return
    if args.owner:
        key = args.owner if ":" in args.owner else _resolve(g, args.owner)
        one = groups.group_of(g, key)
        res = [one] if one else []
    else:
        res = groups.control_groups(g, min_companies=args.min_companies, listed_only=args.listed)
    if args.json:
        _print(res)
        _coverage(g, "groups")
        return
    for grp in res:
        print(f"{grp['name']} ({grp['type']}): controls {grp['controlled']}, influences {grp['influenced']}, "
              f"{grp['listed_companies']} listed")
        for c in grp["companies"]:
            chain = " → ".join(f"{a} ({p:g}%)" if p is not None else a
                               for a, p in zip(c["chain"][1:], c["chain_percents"]))
            print(f"  {c['level']:<9} {c['ticker'] or c['name']:<12} block {f(c['block_percent']):>6}%"
                  f"  look-through {f(c['lookthrough_percent']):>6}%   via {chain}")
    print(f"{len(res)} groups")
    _coverage(g, "groups", sys.stdout)


def _centrality_cmd(g, args) -> None:
    """Network centrality (issue #29, centrality.py), computed now; --write stores it."""
    from . import centrality
    rows = centrality.compute(g)
    written = centrality.write(g, rows) if args.write else None
    res = centrality.top(rows, args.by, args.limit, args.kind)
    if args.kind in ("all", "person"):
        centrality._mark_review(g, res)
    if args.json:
        _print({"items": res, "written": written})
        _coverage(g, "centrality")
        return
    print(f"{'#':>4}  {'name':<40} {'type':<12} {'companies':>9} {'betweenness':>11} {'pagerank':>8}")
    for i, r in enumerate(res, 1):
        name = r["ticker"] if r["ticker"] and not r["ticker"].startswith("~") else r["name"]
        mark = "  review (#6)" if r.get("review") else ""
        print(f"{i:>4}  {(name or r['key'])[:40]:<40} {r['type'] or '':<12} {r['degree']:>9}"
              f" {r['betweenness']:>11.4f} {r['pagerank']:>8.2f}{mark}")
    print(f"{len(res)} of {len(rows)} nodes, by {args.by} (betweenness: share of company pairs linked through"
          f" the node; pagerank: 1 = average)")
    if written:
        print(f"centrality written: {written['nodes']} nodes")
    _coverage(g, "centrality", sys.stdout)


def _trends_cmd(g, args) -> None:
    """Trends over time (issue #30, trends.py)."""
    from . import trends

    def f(v, fmt="{:g}"):
        return "-" if v is None else fmt.format(v)

    if args.cmd == "turnover":
        res = trends.turnover(g, args.sort, key=g.resolve_key("company:" + g.resolve_ticker(args.ticker))
                              if args.ticker else None)
        if args.json:
            _print(res)
            _coverage(g, "turnover")
            return
        s = res["summary"]
        print(f"history {s['history_start']} – {s['as_of']}; a rate needs a full year of history")
        print(f"{'ticker':<10} {'officers':>8} {'arrived':>7} {'left':>5} {'left/yr':>7} {'rate':>6}"
              f" {'tenure':>7}  CEO (years)")
        for c in res["items"][:args.limit]:
            ceo = f"{c['ceo']['name']} ({f(c['ceo']['tenure_years'])}{'+' if c['ceo']['tenure_from'] == 'first_seen' else ''})" \
                if c["ceo"] else "-"
            print(f"{c['ticker'] or c['name']:<10} {c['officers']:>8} {c['arrivals']:>7} {c['departures']:>5}"
                  f" {f(c['departures_per_year']):>7} {f(c['turnover_rate']):>6} {f(c['avg_tenure_years']):>7}"
                  f"  {ceo}{'  HIGH' if c['high'] else ''}")
            if args.ticker:
                for a in c["arrived"]:
                    print(f"    + {a['date']}  {a['name']} — {a['title']}")
                for d in c["left"]:
                    print(f"    - {d['between'][0]} … {d['between'][1]}  {d['name']} — {d['title']}")
        print(f"{s['companies']} companies, {s['rated']} with a rate, {s['high']} high; "
              f"median rate {f(s['median_rate'])}, median tenure {f(s['median_tenure_years'])} y, "
              f"median CEO tenure {f(s['median_ceo_tenure_years'])} y  ('+' = at least: since first seen)")
        _coverage(g, "turnover", sys.stdout)
    elif args.cmd == "accumulation":
        res = trends.accumulation_report(g)
        if args.json:
            _print(res)
            _coverage(g, "accumulation")
            return
        for a in res["items"]:
            path = " → ".join(f"{p['percent']:g}%" for p in a["path"])
            mark = (" crossed " + ", ".join(f"{t:g}%" for t in a["crossed"]) if a["crossed"] else "") + \
                   (f" near {a['approaching']:g}%" if a["approaching"] is not None else "")
            print(f"{a['ticker'] or a['company']:<10} {a['entity'][:40]:<40} {path}  ({a['steps']} steps since"
                  f" {a['since']}){mark}")
        print(f"{len(res['items'])} holders accumulating (history from {res['history_start']}; "
              f"thresholds {', '.join(f'{t:g}' for t in res['settings']['accumulation_thresholds'])}%"
              " are alert settings, not legal rules)")
        _coverage(g, "accumulation", sys.stdout)
    else:
        res = trends.market(g, args.every)
        if args.json:
            _print(res)
            _coverage(g, "trends")
            return
        print(f"history from {res['history_start']}, one point per {res['every']}")
        print(f"{'date':<10} {'cos':>4} {'held':>4} {'state':>5} {'state%':>6} {'interl':>6} {'cross':>5}"
              f" {'medHHI':>7} {'officers':>8}")
        for p in res["points"]:
            print(f"{p['date']:<10} {p['companies']:>4} {p['with_holdings']:>4} {p['state_companies']:>5}"
                  f" {f(p['state_mean_percent']):>6} {p['interlocks']:>6} {p['cross_holdings']:>5}"
                  f" {f(p['median_hhi']):>7} {p['officers']:>8}")
        _coverage(g, "trends", sys.stdout)


def _mnt(v) -> str:
    """1234567890 -> '1.23 тэрбум ₮'."""
    if v is None:
        return "-"
    for f, w in ((1e12, "их наяд"), (1e9, "тэрбум"), (1e6, "сая")):
        if abs(v) >= f:
            return f"{v / f:.2f} {w} ₮"
    return f"{v:,.0f} ₮"


def _market_cmd(g, args) -> None:
    """Market data (issue #31, market.py)."""
    from . import market

    if args.fetch:
        res = market.step(g, limit=args.limit)
        print(json.dumps(res, ensure_ascii=False), file=sys.stderr)
    if args.ticker:
        t = g.resolve_ticker(args.ticker)
        row = market.company_market(g, g.company_keys().get(t, "company:" + t))
        if args.json or row is None:
            _print(row)
            return
        print(f"{row['ticker']}: {row['price']} ₮ ({row['price_date']}{', stale' if row['stale'] else ''}) · "
              f"{row['shares_outstanding'] or '?'} shares · market cap {_mnt(row['market_cap'])} "
              f"({row['market_cap_basis'] or '-'})")
        for p in row["series"][-15:]:
            print(f"  {p['date']}  {p['close']:>12,.2f}  {_mnt(p['market_cap'])}")
        return
    res = market.market(g, args.sort)
    if args.json:
        _print(res)
        _coverage(g, "market")
        return
    s = res["summary"]
    print(f"{s['priced']}/{s['companies']} listed companies priced, {s['with_market_cap']} with a market cap, "
          f"{s['stale']} stale (last trade > {s['stale_days']} days before the read), total "
          f"{_mnt(s['total_market_cap'])}, read {s['as_of'] or 'never'}")
    rows = res["items"] if not args.top else res["items"][:args.top]
    for r in rows:
        if r["price"] is None and r["market_cap"] is None:
            continue
        print(f"{r['ticker']:<6} {r['price'] if r['price'] is not None else '-':>12} {r['price_date'] or '':<10}"
              f"{' stale' if r['stale'] else '      '} {_mnt(r['market_cap']):>18} "
              f"{(str(r['weight_percent']) + '%') if r['weight_percent'] is not None else '':>8}  {r['name']}")
    _coverage(g, "market", sys.stdout)


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
    if args.budget is not None:
        opts["budget"] = "auto" if str(args.budget).strip().lower() == "auto" else int(args.budget)
    if args.max_depth is not None:
        opts["max_depth"] = args.max_depth
    if args.seeds:
        opts["seeds_path"] = args.seeds
    max_depth, seeds_path = opts["max_depth"], opts["seeds_path"]
    with _graph() as g:
        budget = discovery.resolve_budget(g, opts)
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


def _holders_cmd(args):
    from . import holder_search
    from .opendatalab import RegistryLease

    opts = holder_search.settings_from_env()
    minutes = opts["minutes"] if args.minutes is None else args.minutes
    quota = args.quota or opts["quota"]
    every = args.every_days or opts["every_days"]
    with _graph() as g:
        if args.dry_run:
            total = g.holder_target_count()
            limit = holder_search.quota_for(total, quota, every)
            due = g.holder_targets(every, keys=args.key)
            print(f"{total} holders are searched every {every} days; {len(due)} are due; "
                  f"this run takes up to {limit or len(due)} of them ({minutes:g} minutes at most):")
            for t in due[:limit or 50][:50]:
                print(f"  {t['kind']:<12} depth {t['depth']}  {t['query']}"
                      f"{'  (last ' + t['searched_at'] + ')' if t['searched_at'] else ''}")
            print(f"{len(g.holding_leads())} companies found by earlier searches wait for discovery")
            return
        if not minutes and minutes != 0:
            minutes = holder_search.DEFAULT_MINUTES
        lease = RegistryLease(g, "odl-holders")
        if not lease.acquire():
            print(f"holder search skipped: {lease.busy_message()}")
            return
        try:
            report = holder_search.search(g, minutes=minutes, quota=quota, every_days=every, keys=args.key,
                                          progress=lease.keep)
        finally:
            lease.release()
    if args.json:
        _print(report)
    else:
        for f in report["found"]:
            if f["holdings"]:
                print(f"{f['name']}: {f['holdings']} holdings, {f['new']} new")
                for c in f["companies"]:
                    print(f"    {c}")
        print(f"searched {report['searched']} of {report['due']} holders due ({report['total']} in all, "
              f"quota {report['quota'] or 'none'}): {report['holdings']} holdings, {report['in_graph']} already in "
              f"the graph, {report['new']} new companies for discovery, {report['unresolved']} unresolved; "
              f"{report['requests']} requests; {report['left']} holders still due")
        if report["stopped"]:
            print(f"stopped: {report['stopped']}", file=sys.stderr)
    sys.exit(1 if report["stopped"] and "time budget" not in report["stopped"] else 0)


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
