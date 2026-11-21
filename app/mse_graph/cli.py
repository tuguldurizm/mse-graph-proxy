"""Command line entry point.

    python -m mse_graph.cli scrape   [--limit N] [--out data/scraped]
    python -m mse_graph.cli parse-html <dir> [--out data/scraped]
    python -m mse_graph.cli load     <csv-dir> [--wipe]
    python -m mse_graph.cli stats
    python -m mse_graph.cli interlocks | common-owners | board-overlap
    python -m mse_graph.cli owners   <TICKER>
    python -m mse_graph.cli path     <name-or-ticker> <name-or-ticker>
    python -m mse_graph.cli odl-search <query>
    python -m mse_graph.cli odl-import <file-or-url> [--kind auto|shareholders|executives|companies] [--load]

Set MSE_PROXY_URL / MSE_PROXY_TOKEN to route all downloads through the fetch proxy.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

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


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mse_graph")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scrape", help="crawl mse.mn and write CSVs")
    p.add_argument("--limit", type=int)
    p.add_argument("--out", default="data/scraped")
    p.add_argument("--load", action="store_true", help="also load into Neo4j")

    p = sub.add_parser("parse-html", help="parse saved company HTML pages into CSVs")
    p.add_argument("dir")
    p.add_argument("--out", default="data/scraped")

    p = sub.add_parser("load", help="load a CSV directory into Neo4j")
    p.add_argument("dir")
    p.add_argument("--wipe", action="store_true", help="delete the existing graph first")

    sub.add_parser("stats")
    sub.add_parser("interlocks")
    sub.add_parser("common-owners")
    sub.add_parser("board-overlap")
    p = sub.add_parser("owners", help="look-through (ultimate) owners of a company")
    p.add_argument("ticker")
    p = sub.add_parser("path", help="shortest connection between two entities")
    p.add_argument("a")
    p.add_argument("b")

    p = sub.add_parser("odl-search", help="list Open Data Lab Mongolia datasets")
    p.add_argument("query", nargs="?", default="")
    p = sub.add_parser("odl-import", help="import an Open Data Lab dataset file (CSV/XLSX/JSON)")
    p.add_argument("src", help="local path or URL")
    p.add_argument("--kind", default="auto", choices=["auto", "shareholders", "executives", "companies"])
    p.add_argument("--map", action="append", default=[], metavar="FIELD=Header",
                   help="override column mapping, e.g. --map holder_name='Эзэмшигч'")
    p.add_argument("--out", default="data/opendatalab")
    p.add_argument("--load", action="store_true", help="also load into Neo4j")

    p = sub.add_parser("odl-auto", help="search Open Data Lab and import ownership/officer tables")
    p.add_argument("queries", nargs="*", help="search terms (default: built-in Mongolian/English list)")
    p.add_argument("--out", default="data/opendatalab")
    p.add_argument("--load", action="store_true")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")

    if args.cmd in ("scrape", "parse-html"):
        from . import scraper
        ds = scraper.crawl(limit=args.limit) if args.cmd == "scrape" else scraper.parse_html_dir(args.dir)
        csv_io.write_dir(ds, args.out)
        print(f"wrote {args.out}: {ds.summary()}")
        if getattr(args, "load", False):
            with _graph() as g:
                _print(g.load(ds))
        return

    if args.cmd == "odl-search":
        from . import opendatalab
        for d in opendatalab.search(args.query):
            print(f"\n{d['title']}  [{d['organization']}]  id={d['id']}")
            for r in d["resources"]:
                print(f"   {r['format'] or '?':5} {r['name']}\n         {r['url']}")
        return

    if args.cmd == "odl-auto":
        from . import opendatalab
        g = _graph() if args.load else None
        try:
            ds, report = opendatalab.auto_import(args.queries or None,
                                                 lookup=g.company_lookup() if g else None)
            for e in report:
                print(f"{e['status']:<40} {e['dataset'][:50]}  {e['url']}")
            csv_io.write_dir(ds, args.out)
            print(f"wrote {args.out}: {ds.summary()}")
            if g:
                _print(g.load(ds))
        finally:
            if g:
                g.close()
        return

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
            if args.wipe:
                g.wipe()
            _print(g.load(csv_io.read_dir(args.dir)))
        elif args.cmd == "stats":
            _print(g.stats())
        elif args.cmd == "interlocks":
            _print(g.interlocks())
        elif args.cmd == "common-owners":
            _print(g.common_owners())
        elif args.cmd == "board-overlap":
            _print(g.board_overlap())
        elif args.cmd == "owners":
            _print(g.ultimate_owners(args.ticker))
        elif args.cmd == "path":
            _print(g.shortest_path(_resolve(g, args.a), _resolve(g, args.b)))


if __name__ == "__main__":
    main()
