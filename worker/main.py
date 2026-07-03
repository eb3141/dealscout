"""DealScout worker: polls the job queue, runs the scrape→filter→geo→score
pipeline, and pushes results back for the UI.

Pipeline stages are ordered cheapest-first so the single Codex call per
search only sees listings that already passed every free filter.

Usage: python -m worker.main            (reads .env / environment)
"""

import datetime
import time
import traceback

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from worker import db as dbmod
from worker import filters, geo, scorer, scraper

POLL_SECONDS = 4
HEARTBEAT_SECONDS = 30
SCRAPE_LIMIT = 60
CODEX_BATCH_CAP = 30  # max listings per AI call — keeps prompts small and fast


def log(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def process_job(db, job):
    def progress(text):
        db.update_job(job["id"], progress=text)
        log(f"  {text}")

    query = job["query"]
    max_price = job.get("max_price")
    max_drive = job.get("max_drive_min")
    include_seen = bool(job.get("include_seen"))

    market_slug = db.get_setting("market_slug") or None
    junk_keywords = db.get_setting("junk_keywords") or []
    fetch_desc = bool(db.get_setting("fetch_descriptions"))
    home = db.get_setting("home_latlon")  # [lat, lon] saved by the Settings page

    # 1. Scrape (the only Facebook interaction)
    progress("Scraping Facebook Marketplace…")
    listings = scraper.search(
        query,
        max_price=max_price,
        market_slug=market_slug,
        limit=SCRAPE_LIMIT,
        headless=True,
        progress=progress,
    )
    total_found = len(listings)
    progress(f"Found {total_found} listings. Filtering…")
    db.beat()

    # 2. Free filters: seen-before dedupe, then price/junk rules
    already_seen = filters.seen_ids(query)
    if include_seen:
        for l in listings:
            l["seen_before"] = l["listing_id"] in already_seen
    else:
        listings = [l for l in listings if l["listing_id"] not in already_seen]
    listings, _ = filters.rule_filter(listings, max_price=max_price, junk_keywords=junk_keywords)

    # 3. Drive times (cached geocode + routing), then distance cutoff
    if home and listings:
        progress("Computing drive times…")
        for l in listings:
            l["drive_minutes"] = geo.drive_minutes_to_place(tuple(home), l.get("location"))
        if max_drive:
            # Keep unroutable listings (unknown town) rather than silently losing them
            listings = [
                l for l in listings
                if l["drive_minutes"] is None or l["drive_minutes"] <= max_drive
            ]
    db.beat()

    # 4. Cap what the AI sees; optionally enrich the top of the batch
    listings = listings[:CODEX_BATCH_CAP]
    if fetch_desc and listings:
        listings = scraper.enrich_with_descriptions(listings, top_k=10, progress=progress)
        db.beat()

    # 5. One batched Codex call
    if listings:
        progress(f"Scoring {len(listings)} listings with AI…")
        mode = scorer.score_listings(query, listings, max_price=max_price)
        if mode == "fallback":
            log("  WARNING: Codex scoring failed; used rule-based fallback scores")

    db.add_results(job["id"], listings)
    filters.mark_seen(query, listings)
    summary = f"Done: {len(listings)} deals shown (of {total_found} scraped)."
    db.update_job(
        job["id"],
        status="done",
        progress=summary,
        finished_at=dbmod.utcnow().isoformat(),
    )
    log(f"  {summary}")


def run():
    db = dbmod.get_db()
    backend = type(db).__name__
    log(f"DealScout worker started (backend: {backend}). Polling every {POLL_SECONDS}s.")
    last_beat = 0.0
    while True:
        try:
            if time.time() - last_beat > HEARTBEAT_SECONDS:
                db.beat()
                last_beat = time.time()
            job = db.claim_next_job()
            if job:
                log(f"Job {job['id'][:8]}: \"{job['query']}\"")
                try:
                    process_job(db, job)
                except Exception as exc:  # noqa: BLE001 — job errors must not kill the loop
                    log(f"  Job failed: {exc}")
                    traceback.print_exc()
                    db.update_job(
                        job["id"],
                        status="error",
                        error=str(exc)[:500],
                        finished_at=dbmod.utcnow().isoformat(),
                    )
            else:
                time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            log("Worker stopped.")
            break
        except Exception as exc:  # noqa: BLE001 — e.g. transient network to Supabase
            log(f"Loop error (retrying in 15s): {exc}")
            time.sleep(15)


if __name__ == "__main__":
    run()
