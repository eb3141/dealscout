"""Free, rule-based stages of the pipeline: price parsing, junk filtering,
seen-listing dedupe, and the fallback score used when Codex is unavailable.

These run BEFORE any AI call so Codex only ever sees promising listings.
"""

import os
import re
import sqlite3
import time
from pathlib import Path

DEALSCOUT_HOME = Path(os.environ.get("DEALSCOUT_HOME", Path.home() / ".dealscout"))
SEEN_PATH = DEALSCOUT_HOME / "seen.sqlite"

PRICE_RE = re.compile(r"^(?:free|(?:[A-Z]{1,3}\s?)?\$[\d,]+(?:\.\d{2})?)$", re.IGNORECASE)


def parse_price(text):
    """'$1,234' -> 1234.0, 'Free' -> 0.0, anything else -> None."""
    if not text:
        return None
    text = text.strip()
    if text.lower() == "free":
        return 0.0
    m = re.search(r"\$\s*([\d,]+(?:\.\d{2})?)", text)
    return float(m.group(1).replace(",", "")) if m else None


def _norm_query(query):
    return " ".join(query.lower().split())


def _seen_conn():
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SEEN_PATH, timeout=15)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen ("
        " query_norm TEXT, listing_id TEXT, first_seen REAL,"
        " PRIMARY KEY (query_norm, listing_id))"
    )
    return conn


def seen_ids(query):
    with _seen_conn() as c:
        rows = c.execute(
            "SELECT listing_id FROM seen WHERE query_norm=?", (_norm_query(query),)
        ).fetchall()
    return {r[0] for r in rows}


def mark_seen(query, listings):
    now = time.time()
    with _seen_conn() as c:
        c.executemany(
            "INSERT OR IGNORE INTO seen (query_norm, listing_id, first_seen) VALUES (?,?,?)",
            [(_norm_query(query), l["listing_id"], now) for l in listings],
        )


def rule_filter(listings, max_price=None, junk_keywords=None):
    """Drop listings that obviously don't qualify. Returns (kept, dropped_count)."""
    junk = [k.strip().lower() for k in (junk_keywords or []) if k.strip()]
    kept = []
    for l in listings:
        title = (l.get("title") or "").lower()
        if not title:
            continue
        if any(k in title for k in junk):
            continue
        price = l.get("price")
        if max_price is not None and price is not None and price > max_price:
            continue
        kept.append(l)
    return kept, len(listings) - len(kept)


def rule_score(listing, max_price=None):
    """Fallback score (0-100) when AI scoring is unavailable: price headroom
    under budget is the main signal we can compute without market knowledge."""
    price = listing.get("price")
    score = 50
    if price == 0:
        score = 85  # free stuff is usually worth a look
    elif price is not None and max_price:
        headroom = 1 - (price / max_price)
        score = int(50 + 40 * max(0.0, min(1.0, headroom)))
    if listing.get("drive_minutes") is not None and listing["drive_minutes"] <= 20:
        score = min(100, score + 5)
    return score
