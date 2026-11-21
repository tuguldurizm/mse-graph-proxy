"""Full data pipeline: wait for Neo4j -> scrape mse.mn -> load -> Open Data Lab -> summary.

Real-data runs never wipe the graph: each load is compared with the previous
one, so people who left and stakes that changed are kept as history. Every
scrape is also archived under data/archive/<date>/ (see csv_io.archive).

Used by the all-in-one Render deployment (`deploy/start.sh`), where it runs in
the background next to the web app. Progress goes to the log and to
`STATUS_FILE`, which the API serves at /api/status.

    python -m mse_graph.pipeline            # real data
    PIPELINE_DEMO=1 python -m mse_graph.pipeline
"""
from __future__ import annotations

import json
import logging
import os
import time
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

from . import csv_io

STATUS_FILE = Path(os.getenv("PIPELINE_STATUS_FILE", "/tmp/pipeline_status.json"))
log = logging.getLogger("pipeline")
_status: dict = {"state": "starting", "steps": []}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def set_status(state: str, step: str | None = None, **extra) -> None:
    _status["state"] = state
    _status["updated"] = _now()
    if step:
        _status["steps"].append({"at": _now(), "step": step, **extra})
        log.info("PIPELINE %s: %s %s", state, step, json.dumps(extra, ensure_ascii=False) if extra else "")
    STATUS_FILE.write_text(json.dumps(_status, ensure_ascii=False, indent=1))


def read_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text())
    except (OSError, ValueError):
        return {"state": "unknown"}


def wait_for_neo4j(timeout: int = 600):
    from .graph import Graph

    from .config import settings

    deadline = time.time() + timeout
    attempt = 0
    while True:
        g = Graph()
        try:
            g.driver.verify_connectivity()
            if attempt:
                log.info("connected to neo4j at %s after %d retries", settings.neo4j_uri, attempt)
            return g
        except Exception as exc:
            g.close()
            # say why (wrong password, paused Aura instance, ...) instead of retrying silently
            if attempt % 10 == 0:
                log.warning("neo4j at %s not reachable (attempt %d): %s: %s",
                            settings.neo4j_uri, attempt + 1, type(exc).__name__, exc)
            attempt += 1
            if time.time() > deadline:
                raise RuntimeError(f"Neo4j did not start: {type(exc).__name__}: {exc}")
            time.sleep(3)


def run() -> None:
    if os.getenv("DISCOVER") == "1":
        set_status("running", "discovering site structure")
        from . import discover
        if os.getenv("DISCOVER_PASS", "1") == "2":
            urls = [u for u in os.getenv("DISCOVER_URLS", "").split(",") if u.strip()]
            discover.run_explore(urls or None)
        else:
            discover.run()
        os.environ["PIPELINE_DEMO"] = "1"   # discovery-only run: don't crawl, show demo data
    set_status("running", "waiting for neo4j")
    g = wait_for_neo4j()
    try:
        g.init_schema()
        min_hours = float(os.getenv("PIPELINE_MIN_INTERVAL_HOURS", "20"))
        since = g.hours_since_last_scrape()
        if os.getenv("PIPELINE_DEMO") == "1":
            if g.has_real_data():
                # the database persists (e.g. Neo4j Aura): never wipe real data and its history
                set_status("running", "demo mode: database already holds real data, left untouched")
            else:
                g.wipe()
                set_status("running", "loaded demo data", stats=g.load(csv_io.read_dir("data/sample")))
        elif since is not None and since < min_hours and os.getenv("PIPELINE_FORCE") != "1":
            # A free instance restarts whenever it wakes up; with a persistent database
            # there is no need to scrape mse.mn again each time.
            set_status("running", f"skipped scrape: last one {since:.1f} h ago "
                                  f"(PIPELINE_MIN_INTERVAL_HOURS={min_hours:g}, PIPELINE_FORCE=1 to override)")
        else:
            from . import opendatalab, scraper

            set_status("running", "scraping mse.mn")
            limit = int(os.getenv("SCRAPE_LIMIT", "0")) or None
            try:
                crawl = scraper.crawl if os.getenv("MSE_SOURCE") == "legacy" else scraper.crawl_open
                ds = crawl(limit=limit)
                csv_io.write_dir(ds, "data/scraped")
                csv_io.archive(ds)
                # No wipe: the load compares with the previous scrape and keeps history.
                if g.drop_demo_data():
                    set_status("running", "removed demo data")
                set_status("running", "scraped mse.mn", **ds.summary())
                set_status("running", "loaded mse.mn", stats=g.load(ds))
                if not limit:
                    g.mark_scrape()
            except Exception as exc:
                set_status("running", "mse.mn scrape failed", error=str(exc))
                log.error(traceback.format_exc())

            set_status("running", "importing Open Data Lab")
            if os.getenv("ODL_ENABLE") != "1":
                set_status("running", "Open Data Lab skipped (set ODL_ENABLE=1)")
            else:
                try:
                    ods, report = opendatalab.auto_import(lookup=g.company_lookup())
                    for e in report:
                        log.info("ODL %-40s %s  %s", e["status"], e["dataset"][:60], e["url"])
                    ods.observed_at = ods.observed_at or date.today().isoformat()
                    csv_io.write_dir(ods, "data/opendatalab")
                    csv_io.archive(ods, part="opendatalab")
                    set_status("running", "loaded Open Data Lab", datasets=len(report),
                               imported=sum(1 for e in report if e["status"].startswith("imported")),
                               stats=g.load(ods))
                except Exception as exc:
                    set_status("running", "Open Data Lab import failed", error=str(exc))
                    log.error(traceback.format_exc())

        stats = g.stats()
        top = [{"name": r["name"], "companies": r["companies"]} for r in g.interlocks()[:10]]
        set_status("done", "finished", stats=stats, top_interlocks=top)
    except Exception as exc:
        set_status("failed", "pipeline crashed", error=str(exc))
        log.error(traceback.format_exc())
    finally:
        g.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run()
