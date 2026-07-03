"""Facebook Marketplace scraper using a persistent logged-in browser profile.

Design constraints:
  - Single page, sequential, human-paced (2-5s waits) — one search per job,
    ~60 listings max. This keeps the footprint indistinguishable from Elliott
    browsing Marketplace himself.
  - Parse listing cards from the DOM anchor text (resilient to class-name
    churn since FB obfuscates classes; the /marketplace/item/ href is stable).

Run `python -m worker.fb_login` once to create the logged-in profile.
"""

import os
import random
import re
import time
from pathlib import Path
from urllib.parse import urlencode

DEALSCOUT_HOME = Path(os.environ.get("DEALSCOUT_HOME", Path.home() / ".dealscout"))
USER_DATA_DIR = DEALSCOUT_HOME / "fb_profile"

ITEM_ID_RE = re.compile(r"/marketplace/item/(\d+)")
PRICE_LINE_RE = re.compile(r"^(?:free|(?:[A-Z]{1,3}\s?)?\$\s?[\d,]+(?:\.\d{2})?)$", re.IGNORECASE)
LOCATION_LINE_RE = re.compile(r"^[\w\s.'\-]+,\s*[A-Z]{2}$")

EXTRACT_CARDS_JS = """
() => Array.from(document.querySelectorAll('a[href*="/marketplace/item/"]')).map(a => ({
    href: a.href,
    text: a.innerText || '',
    img: (a.querySelector('img') || {}).src || null,
}))
"""


class NotLoggedInError(Exception):
    pass


def _sleep(lo=2.0, hi=5.0):
    time.sleep(random.uniform(lo, hi))


def build_search_url(query, max_price=None, market_slug=None):
    base = (
        f"https://www.facebook.com/marketplace/{market_slug.strip('/')}/search"
        if market_slug
        else "https://www.facebook.com/marketplace/search"
    )
    params = {"query": query, "exact": "false"}
    if max_price:
        params["maxPrice"] = int(max_price)
    return f"{base}/?{urlencode(params)}"


def _parse_card(card):
    m = ITEM_ID_RE.search(card["href"])
    if not m:
        return None
    listing_id = m.group(1)
    lines = [l.strip() for l in card["text"].splitlines() if l.strip()]
    if not lines:
        return None

    # Discounted listings show two price lines (current + struck-through
    # original) — the first is the real price, the rest must not leak into
    # the title.
    price_lines = []
    location = None
    rest = []
    for line in lines:
        if PRICE_LINE_RE.match(line):
            price_lines.append(line)
        elif LOCATION_LINE_RE.match(line):
            location = line  # keep the last match; FB puts location below title
        else:
            rest.append(line)
    price_text = price_lines[0] if price_lines else None

    # Title: first non-price non-location line; fall back to the longest one.
    title = rest[0] if rest else None
    if title and len(title) < 4 and len(rest) > 1:
        title = max(rest, key=len)
    if not title:
        return None

    from worker.filters import parse_price

    return {
        "listing_id": listing_id,
        "title": title,
        "price_text": price_text,
        "price": parse_price(price_text),
        "location": location,
        "url": f"https://www.facebook.com/marketplace/item/{listing_id}/",
        "image_url": card["img"],
    }


def _check_logged_in(page):
    url = page.url.lower()
    if "login" in url or "checkpoint" in url:
        raise NotLoggedInError(
            "Facebook redirected to a login page. Run `python -m worker.fb_login` "
            "on the Mac to refresh the session."
        )


def _dismiss_dialogs(page):
    # Anonymous/expired sessions get a login modal; Escape usually clears
    # informational popups. Best effort only.
    try:
        page.keyboard.press("Escape")
        close = page.locator('[aria-label="Close"]').first
        if close.is_visible(timeout=1500):
            close.click(timeout=1500)
    except Exception:
        pass


def search(query, max_price=None, market_slug=None, limit=60, headless=True, progress=None):
    """Scrape Marketplace search results. Returns a list of listing dicts."""
    from playwright.sync_api import sync_playwright

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    url = build_search_url(query, max_price=max_price, market_slug=market_slug)
    listings, seen = [], set()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(USER_DATA_DIR),
            headless=headless,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            _sleep(3, 5)
            _check_logged_in(page)
            _dismiss_dialogs(page)

            stagnant = 0
            for _ in range(10):  # scroll rounds
                cards = page.evaluate(EXTRACT_CARDS_JS)
                before = len(seen)
                for card in cards:
                    parsed = _parse_card(card)
                    if parsed and parsed["listing_id"] not in seen:
                        seen.add(parsed["listing_id"])
                        listings.append(parsed)
                if progress:
                    progress(f"Scraping… {len(listings)} listings found")
                if len(listings) >= limit:
                    break
                stagnant = stagnant + 1 if len(seen) == before else 0
                if stagnant >= 2:
                    break  # page stopped yielding new cards
                page.mouse.wheel(0, 2600)
                _sleep(1.5, 3.0)
        finally:
            ctx.close()

    return listings[:limit]


def fetch_description(page, listing_url):
    """Best-effort description grab from a listing page (already-open page)."""
    page.goto(listing_url, wait_until="domcontentloaded", timeout=45_000)
    _sleep(2, 4)
    try:
        see_more = page.get_by_text("See more", exact=True).first
        if see_more.is_visible(timeout=1200):
            see_more.click(timeout=1200)
    except Exception:
        pass
    desc = page.evaluate(
        """() => {
            const meta = document.querySelector('meta[name="description"]');
            return meta ? meta.content : null;
        }"""
    )
    return (desc or "").strip()[:600] or None


def enrich_with_descriptions(listings, top_k=10, headless=True, progress=None):
    """Visit the top-K listing pages for descriptions. Optional, human-paced."""
    from playwright.sync_api import sync_playwright

    targets = listings[:top_k]
    if not targets:
        return listings
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(USER_DATA_DIR),
            headless=headless,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            for i, listing in enumerate(targets):
                if progress:
                    progress(f"Reading listing details… {i + 1}/{len(targets)}")
                try:
                    listing["description"] = fetch_description(page, listing["url"])
                except Exception:
                    listing["description"] = None
        finally:
            ctx.close()
    return listings
