"""Full data pipeline: wait for Neo4j -> scrape mse.mn -> load -> Open Data Lab -> summary.

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
from datetime import datetime, timezone
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

    deadline = time.time() + timeout
    while True:
        g = Graph()
        try:
            g.driver.verify_connectivity()
            return g
        except Exception as exc:
            g.close()
            if time.time() > deadline:
                raise RuntimeError(f"Neo4j did not start: {exc}")
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
        if os.getenv("PIPELINE_DEMO") == "1":
            g.wipe()
            set_status("running", "loaded demo data", stats=g.load(csv_io.read_dir("data/sample")))
        else:
            from . import opendatalab, scraper

            set_status("running", "scraping mse.mn")
            limit = int(os.getenv("SCRAPE_LIMIT", "0")) or None
            try:
                ds = scraper.crawl(limit=limit)
                csv_io.write_dir(ds, "data/scraped")
                g.wipe()
                set_status("running", "scraped mse.mn", **ds.summary())
                set_status("running", "loaded mse.mn", stats=g.load(ds))
            except Exception as exc:
                set_status("running", "mse.mn scrape failed", error=str(exc))
                log.error(traceback.format_exc())

            set_status("running", "importing Open Data Lab")
            try:
                ods, report = opendatalab.auto_import(lookup=g.company_lookup())
                for e in report:
                    log.info("ODL %-40s %s  %s", e["status"], e["dataset"][:60], e["url"])
                csv_io.write_dir(ods, "data/opendatalab")
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
