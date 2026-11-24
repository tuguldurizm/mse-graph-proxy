"""Full data pipeline: wait for Neo4j -> scrape mse.mn -> load -> hand-curated datasets
(data/manual/) -> Open Data Lab registry -> discovery (registry pages of companies the graph
doesn't have yet, discovery.py) -> market data (prices, market caps: market.py, every 12 h even
when the scrape is skipped) -> check the datasets against the registry -> summary.

Real-data runs never wipe the graph: each load is compared with the previous
one, so people who left and stakes that changed are kept as history. Every
scrape is also archived under data/archive/<date>/ (see csv_io.archive).
The edges a load opened or closed are its changes (history.run_changes). They
are logged, reported in the status, and sent as alerts when ALERT_WEBHOOK_URL
or TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are set (notify.py).

Scheduling: at start-up a scrape is skipped if the last full one is less than
PIPELINE_MIN_INTERVAL_HOURS (default 20) old, so a free instance that keeps
waking up doesn't scrape each time. PIPELINE_INTERVAL_HOURS repeats the run
while the process stays up; it only helps on an instance that doesn't sleep.

Used by the all-in-one Render deployment (`deploy/start.sh`), where it runs in
the background next to the web app. Progress goes to the log and to
`STATUS_FILE`, which the API serves at /api/status.

    python -m mse_graph.pipeline            # real data
    PIPELINE_DEMO=1 python -m mse_graph.pipeline
    PIPELINE_INTERVAL_HOURS=24 python -m mse_graph.pipeline   # run again every 24 hours
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

from . import csv_io
from .manual import load_manual, validate_manual, with_registry_tickers  # noqa: F401

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


def track(g, loaded: dict, ds, source: str) -> None:
    """Report what one load changed and send alerts. Never raises."""
    from . import history, notify

    try:
        changes = history.run_changes(g, loaded["run"], ds)
        sent = notify.send(changes) if changes else {"sent": 0, "channels": [], "errors": []}
        set_status("running", f"{source} changes", changes=len(changes),
                   significant=sum(1 for c in changes if history.is_significant(c)),
                   alerted=sent["sent"] if sent["channels"] else 0)
    except Exception as exc:
        set_status("running", f"{source} change tracking failed", error=str(exc))
        log.error(traceback.format_exc())


def manual_step(g) -> None:
    """Load new or changed hand-curated datasets (data/manual/). Never raises."""
    try:
        for res in load_manual(g):
            set_status("running", f"manual dataset {res['dataset']}: {res['status']}", **res)
    except Exception as exc:
        set_status("running", "manual datasets failed", error=str(exc))
        log.error(traceback.format_exc())


def validation_step(g) -> None:
    """Compare the hand-curated datasets with the registry data in the graph. Never raises."""
    try:
        for rep in validate_manual(g):
            set_status("running", f"manual dataset {rep['dataset']} vs Open Data Lab",
                       companies=rep["companies"], checked=rep["checked"], summary=rep["summary"],
                       details=rep["details"])
    except Exception as exc:
        set_status("running", "manual dataset validation failed", error=str(exc))
        log.error(traceback.format_exc())


def registry_step(g, part, limit, keep_lease=None) -> None:
    """Today's shard of the Open Data Lab registry (plus data/manual/'s companies), then
    discovery. The caller holds the registry lease; `keep_lease()` renews it. Never raises."""
    from . import discovery, opendatalab, scraper

    try:
        from .config import settings

        fetcher = scraper.Fetcher()
        # naming the route also validates ODL_PROXY_URL (a bad value fails this step only)
        route = fetcher.router.describe(settings.odl_base_url)
        set_status("running", "importing Open Data Lab registry", egress=route)
        everything = g.companies_with_reg()
        # today's shard (discovered companies: a seventh even in a full run), plus the
        # companies of data/manual/ (re-checked every run)
        companies = with_registry_tickers(
            discovery.weekly_selection(everything, g.discovered_tickers(), part), everything)
        ods, report = opendatalab.enrich_companies(companies, fetcher=fetcher, limit=limit, progress=keep_lease)
        found = sum(1 for e in report if e["status"] == "ok")
        failed = sum(1 for e in report if e["status"] in ("fetch failed", "skipped"))
        if failed and not found:
            # nothing came back: keep the previous registry data, don't archive an empty run
            set_status("running", "Open Data Lab unreachable", egress=route, failed=failed,
                       error=fetcher.last_error)
        else:
            ods.observed_at = ods.observed_at or date.today().isoformat()
            csv_io.write_dir(ods, "data/opendatalab")
            csv_io.archive(ods, part="opendatalab")
            loaded = g.load(ods)
            set_status("running", "loaded Open Data Lab registry", companies=len(report),
                       found=found, failed=failed, stats=loaded)
            track(g, loaded, ods, "opendatalab")
            # numbers the registry doesn't know rest for a while (DISCOVERY_RETRY_DAYS)
            g.note_registry_checks([{"reg": e["reg_no"], "status": e["status"],
                                     "ticker": e["ticker"]} for e in report])
            discovery_step(g, fetcher, limit, keep_lease)
    except Exception as exc:
        set_status("running", "Open Data Lab import failed", error=str(exc))
        log.error(traceback.format_exc())


def discovery_step(g, fetcher, limit: int | None = None, keep_lease=None) -> None:
    """Grow the graph: read the registry pages of companies it doesn't have yet
    (discovery.py), up to DISCOVERY_BUDGET pages. Never raises."""
    from . import discovery

    opts = discovery.settings_from_env()
    budget = discovery.resolve_budget(g, opts)
    budget = min(budget, limit) if limit else budget
    if budget <= 0:
        set_status("running", "registry discovery off (DISCOVERY_BUDGET=0)")
        return
    try:
        set_status("running", "discovering companies in the Open Data Lab registry", budget=budget,
                   max_depth=opts["max_depth"])
        report, ds = discovery.discover(g, fetcher=fetcher, budget=budget, max_depth=opts["max_depth"],
                                        seeds_path=opts["seeds_path"], retry_days=opts["retry_days"],
                                        progress=keep_lease)
        discovery.save(ds)
        set_status("running", "registry discovery done", **discovery.summary(report))
    except Exception as exc:
        set_status("running", "registry discovery failed", error=str(exc))
        log.error(traceback.format_exc())


def market_step(g, force: bool = False) -> None:
    """Today's prices and market caps from mse.mn (issue #31, market.py), at most every
    MARKET_MIN_INTERVAL_HOURS (12), also when the company scrape is skipped: it is about one
    request per listed company. MARKET_ENABLE=0 turns it off. Never raises."""
    from . import market

    if os.getenv("MARKET_ENABLE", "1") == "0":
        return
    try:
        min_hours = float(os.getenv("MARKET_MIN_INTERVAL_HOURS", "12"))
        since = market.hours_since_read(g)
        if since is not None and since < min_hours and not force and os.getenv("PIPELINE_FORCE") != "1":
            set_status("running", f"skipped market data: last read {since:.1f} h ago "
                                  f"(MARKET_MIN_INTERVAL_HOURS={min_hours:g})")
            return
        set_status("running", "reading market data from mse.mn")
        res = market.step(g)
        set_status("running", "read market data", **{k: v for k, v in res.items() if k != "errors"},
                   errors=res["errors"][:5])
    except Exception as exc:
        set_status("running", "market data failed", error=str(exc))
        log.error(traceback.format_exc())


def frc_step(g, force: bool = False) -> None:
    """FRC's licensed entities and decisions (issue #10, frc.py): every list, FRC_DELAY (5 s) apart, at
    most every FRC_MIN_INTERVAL_HOURS (20). No people: Open Data Lab gives them. FRC_ENABLE=0 turns it
    off. Never raises."""
    from . import frc

    if os.getenv("FRC_ENABLE", "1") == "0":
        return
    try:
        min_hours = float(os.getenv("FRC_MIN_INTERVAL_HOURS", "20"))
        since = frc.hours_since_probe(g)
        if since is not None and since < min_hours and not force and os.getenv("PIPELINE_FORCE") != "1":
            return
        set_status("running", "reading FRC (frc.mn)")
        res = frc.step(g)
        frc.mark_step(g, res)
        log.info("PIPELINE frc: %s", json.dumps({k: v for k, v in res.items() if k != "errors"} |
                                                 {"errors": res["errors"][:3]}, ensure_ascii=False, default=str))
    except Exception as exc:
        log.warning("PIPELINE frc failed: %s", exc)
        log.error(traceback.format_exc())


def nested_repair_step(g) -> None:
    """Once, before the first scrape with the fixed open.mse.mn parser: correct the stakes the
    nested-table bug loaded (layout.repair_nested, 2026-11-24). About 15 requests to open.mse.mn.
    Done when `(:Meta {key: 'mse_nested_repair'})` says so; NESTED_REPAIR=0 turns it off. Never raises."""
    from . import layout

    if os.getenv("NESTED_REPAIR", "1") == "0":
        return
    try:
        res = layout.repair_nested(g)
        if res.get("skipped"):
            return
        log.info("PIPELINE mse repair: %s", json.dumps(res, ensure_ascii=False, default=str))
    except Exception as exc:
        log.warning("PIPELINE mse repair failed: %s", exc)
        log.error(traceback.format_exc())


def keep_awake(url: str, stop: threading.Event, interval: float = 240.0, get=None) -> int:
    """GET <url>/healthz every `interval` seconds until `stop` is set; returns the ping count.

    A free Render instance sleeps after 15 minutes without inbound requests, which
    would cut a run (mse.mn + Open Data Lab, about 45 minutes) short. Render sets
    RENDER_EXTERNAL_URL; the pings go out only while a run is in progress.
    """
    if get is None:
        import httpx

        def get(u):
            return httpx.get(u, timeout=30)
    n = 0
    while not stop.wait(interval):
        try:
            get(url.rstrip("/") + "/healthz")
            n += 1
        except Exception as exc:  # a missed ping is harmless
            log.debug("keep-awake ping failed: %s", exc)
    return n


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


def run_forever() -> None:
    """Run the pipeline, then again every PIPELINE_INTERVAL_HOURS (if set).

    Demo and discovery runs happen once. A failed run is retried at the next interval.
    """
    hours = float(os.getenv("PIPELINE_INTERVAL_HOURS", "0") or 0)
    first = True
    while True:
        run(force=not first)       # scheduled re-runs ignore PIPELINE_MIN_INTERVAL_HOURS
        first = False
        if hours <= 0 or os.getenv("PIPELINE_DEMO") == "1":
            return
        _status["next_run"] = datetime.fromtimestamp(time.time() + hours * 3600, timezone.utc).isoformat(
            timespec="seconds")
        set_status(_status["state"])
        time.sleep(hours * 3600)


def run(force: bool = False) -> None:
    url = os.getenv("RENDER_EXTERNAL_URL", "")
    stop = threading.Event()
    if url and os.getenv("KEEP_AWAKE", "1") != "0":
        threading.Thread(target=keep_awake, args=(url, stop), daemon=True, name="keep-awake").start()
    try:
        _run(force)
    finally:
        stop.set()
        from . import memo
        memo.clear()        # the API's cached analyses (memo.py) describe the graph before this run


def _run(force: bool = False) -> None:
    _status["steps"] = []          # each run reports its own progress
    _status.pop("next_run", None)
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
        done = {k: v for k, v in (getattr(g, "consolidated", None) or {}).items() if v}
        if done:   # one node per legal entity (#35): the first start re-keys every company
            log.info("PIPELINE companies consolidated: %s", json.dumps(done, ensure_ascii=False))
        people = {"rekeyed_or_split": getattr(g, "rekeyed_people", 0), "linked": getattr(g, "adopted", 0)}
        if any(people.values()):   # person identity (#36): the first start splits merged namesakes
            log.info("PIPELINE people resolved: %s, under review: %d", json.dumps(people),
                     len(g.ambiguous_people()))
        if getattr(g, "overridden", None):   # identity overrides file (#37): records re-pointed by hand
            log.info("PIPELINE identity overrides applied: %s", json.dumps(g.overridden))
        typed = getattr(g, "holdings_typed", None)
        if typed and typed.get("stakes_typed"):   # holding types (#39): the first start types every stake
            log.info("PIPELINE holdings typed: %s", json.dumps(typed))
        if os.getenv("PIPELINE_DEMO") != "1":
            nested_repair_step(g)
        min_hours = float(os.getenv("PIPELINE_MIN_INTERVAL_HOURS", "20"))
        since = g.hours_since_last_scrape()
        if os.getenv("PIPELINE_DEMO") == "1":
            if g.has_real_data():
                # the database persists (e.g. Neo4j Aura): never wipe real data and its history
                set_status("running", "demo mode: database already holds real data, left untouched")
            else:
                g.wipe(keep_decisions=True)
                set_status("running", "loaded demo data", stats=g.load(csv_io.read_dir("data/sample")))
        elif since is not None and since < min_hours and os.getenv("PIPELINE_FORCE") != "1" and not force:
            # A free instance restarts whenever it wakes up; with a persistent database
            # there is no need to scrape mse.mn again each time.
            set_status("running", f"skipped scrape: last one {since:.1f} h ago "
                                  f"(PIPELINE_MIN_INTERVAL_HOURS={min_hours:g}, PIPELINE_FORCE=1 to override)")
            manual_step(g)
        else:
            from . import opendatalab, scraper, shard

            set_status("running", "scraping mse.mn")
            limit = int(os.getenv("SCRAPE_LIMIT", "0")) or None
            # PIPELINE_SHARD=auto: one seventh of the companies per run, all of them each week
            part = shard.parse(os.getenv("PIPELINE_SHARD", ""))
            try:
                if os.getenv("MSE_SOURCE") == "legacy" or not part:
                    crawl = scraper.crawl if os.getenv("MSE_SOURCE") == "legacy" else scraper.crawl_open
                    ds = crawl(limit=limit)
                else:
                    ds = scraper.crawl_open(limit=limit, shard=part)
                csv_io.write_dir(ds, "data/scraped")
                csv_io.archive(ds)
                # No wipe: the load compares with the previous scrape and keeps history.
                if g.drop_demo_data():
                    set_status("running", "removed demo data")
                set_status("running", "scraped mse.mn", **ds.summary())
                loaded = g.load(ds)
                set_status("running", "loaded mse.mn", stats=loaded)
                if not limit and not part:
                    g.mark_scrape()
                track(g, loaded, ds, "mse")
                from . import affiliations
                log.info("PIPELINE affiliations: %s", json.dumps(affiliations.summary(g), ensure_ascii=False))
            except Exception as exc:
                set_status("running", "mse.mn scrape failed", error=str(exc))
                log.error(traceback.format_exc())

            # hand-curated datasets go in before the registry import, so it checks their companies
            manual_step(g)

            if os.getenv("ODL_ENABLE", "1") == "0":
                set_status("running", "Open Data Lab skipped (ODL_ENABLE=0)")
            else:
                # opendatalab.mn is never crawled in parallel: the daily job may be reading it now
                lease = opendatalab.RegistryLease(g, "hosted pipeline")
                if not lease.acquire():
                    set_status("running", "Open Data Lab skipped: another run is reading the registry",
                               holder=lease.holder.get("owner"), until=lease.holder.get("until"))
                else:
                    try:
                        registry_step(g, part, limit, lease.keep)
                    finally:
                        lease.release()

        if os.getenv("PIPELINE_DEMO") != "1":
            market_step(g, force=force)
            validation_step(g)
            frc_step(g, force=force)

        stats = g.stats()
        top = [{"name": r["name"], "companies": r["companies"]} for r in g.interlocks()[:10]]
        try:   # data quality (#8) and coverage (#27) after each run: `PIPELINE quality` in the log
            from . import quality
            checked = quality.summary_line(g)
            log.info("PIPELINE quality: %s", json.dumps(checked, ensure_ascii=False))
        except Exception as exc:  # a report must never fail the run
            checked = {"error": str(exc)}
        net = None
        try:   # detected groups (#28): group_id on the nodes, `PIPELINE groups` with the curated score
            from . import groups
            net = groups.Network.read(g)       # read once, for the groups and the centrality (#29)
            found = groups.detect(net=net)
            written = groups.write_communities(g, found)
            written["curated"] = {s["dataset"]: s["score"] for s in groups.curated_score(g, found)}
            log.info("PIPELINE groups: %s", json.dumps(written, ensure_ascii=False))
        except Exception as exc:  # nor may group detection
            log.warning("PIPELINE groups failed: %s", exc)
        try:   # network centrality (#29): centrality_* on the nodes, `PIPELINE centrality` with the top
            from . import centrality
            written = centrality.write(g, centrality.compute(g, net=net))
            log.info("PIPELINE centrality: %s", json.dumps(written, ensure_ascii=False))
        except Exception as exc:  # nor may the centrality
            log.warning("PIPELINE centrality failed: %s", exc)
        try:   # trends over time (#30): accumulation alerts, high turnover, the market now
            from . import trends
            log.info("PIPELINE trends: %s", json.dumps(trends.summary_line(g), ensure_ascii=False))
        except Exception as exc:  # nor the trends
            log.warning("PIPELINE trends failed: %s", exc)
        try:   # officials' declared interests, licences, tenders (#9), read with the registry pages
            from . import officials
            log.info("PIPELINE officials: %s", json.dumps(officials.counts(g), ensure_ascii=False))
        except Exception as exc:  # nor the officials' counts
            log.warning("PIPELINE officials failed: %s", exc)
        try:   # market data (#31): prices, market caps, the largest companies
            from . import market
            log.info("PIPELINE market: %s", json.dumps(market.summary_line(g), ensure_ascii=False))
        except Exception as exc:  # nor the market summary
            log.warning("PIPELINE market failed: %s", exc)
        set_status("done", "finished", stats=stats, top_interlocks=top, quality=checked)
    except Exception as exc:
        set_status("failed", "pipeline crashed", error=str(exc))
        log.error(traceback.format_exc())
    finally:
        g.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run_forever()
