"""DealScout — personal Facebook Marketplace deal finder.

Cloud UI: submits search jobs to the shared store (Supabase in production,
local SQLite in dev) and renders scored results. The scraping/scoring happens
on the Mac worker (worker/main.py).
"""

import datetime
import hashlib
import html
import os
import time
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="DealScout", page_icon="🔎", layout="wide")

# Make secrets available to worker.db before importing it (Streamlit Cloud
# stores them in st.secrets; locally .streamlit/secrets.toml or env is used).
try:
    for _key in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY"):
        if _key in st.secrets:
            os.environ[_key] = st.secrets[_key]
except FileNotFoundError:
    pass  # no secrets file in local dev — LocalDB mode

from worker import db as dbmod  # noqa: E402
from worker import geo  # noqa: E402

APP_DIR = Path(__file__).parent
POLL_EVERY_SECONDS = 3


@st.cache_resource
def get_db():
    return dbmod.get_db()


def inject_css():
    css = (APP_DIR / "ui" / "style.css").read_text()
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


# ---------------------------------------------------------------- password
def password_hash_expected():
    try:
        if "PASSWORD_SHA256" in st.secrets:
            return st.secrets["PASSWORD_SHA256"]
    except FileNotFoundError:
        pass
    return os.environ.get("DEALSCOUT_PASSWORD_SHA256")


def require_password():
    expected = password_hash_expected()
    if not expected:
        st.error(
            "No password configured. Set PASSWORD_SHA256 in Streamlit secrets "
            "(generate with: `python -c \"import hashlib;print(hashlib.sha256("
            "b'yourpassword').hexdigest())\"`)."
        )
        st.stop()
    if st.session_state.get("authed"):
        return

    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.markdown(
            '<div class="ds-login"><div class="logo">🔎</div>'
            "<h2>DealScout</h2><p>Private deal finder — enter password</p></div>",
            unsafe_allow_html=True,
        )
        with st.form("login"):
            pw = st.text_input("Password", type="password", label_visibility="collapsed")
            ok = st.form_submit_button("Unlock", use_container_width=True)
        if ok:
            if hashlib.sha256(pw.encode()).hexdigest() == expected:
                st.session_state["authed"] = True
                st.rerun()
            st.error("Wrong password")
    st.stop()


# ---------------------------------------------------------------- rendering
def fmt_ago(ts):
    if not ts:
        return "never"
    delta = dbmod.utcnow() - dbmod._parse_ts(ts)
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h ago"


FLAG_STYLES = {
    "scam_risk": ("scam", "⚠ scam risk"),
    "overpriced": ("warn", "overpriced"),
    "great_value": ("value", "great value"),
    "incomplete_info": ("warn", "sparse info"),
}


def render_worker_status(db):
    online = dbmod.worker_online(db)
    cls = "online" if online else "offline"
    label = "Scraper online" if online else "Scraper offline — wake the Mac"
    st.markdown(
        f'<div class="ds-status {cls}"><span class="dot"></span>{label}</div>',
        unsafe_allow_html=True,
    )
    return online


def render_results(results):
    if not results:
        st.info("No deals matched. Try loosening the price cap or drive time — "
                "or everything matching was already shown in a previous search "
                "(tick “include previously seen”).")
        return
    cards = []
    for r in results:
        score = r.get("score") or 0
        score_cls = "hi" if score >= 75 else ("mid" if score >= 50 else "lo")
        price = html.escape(r.get("price_text") or "—")
        title = html.escape(r.get("title") or "Untitled")
        loc = html.escape(r.get("location") or "Location unknown")
        drive = r.get("drive_minutes")
        meta = f"📍 {loc}" + (f" &nbsp;·&nbsp; 🚗 ~{drive} min" if drive is not None else "")
        reason = html.escape(r.get("reason") or "")
        verdict = html.escape(r.get("verdict") or "")
        chips = ""
        for flag in r.get("flags") or []:
            cls, label = FLAG_STYLES.get(flag, ("warn", flag))
            chips += f'<span class="ds-chip {cls}">{html.escape(label)}</span>'
        if r.get("seen_before"):
            chips += '<span class="ds-chip seen">seen before</span>'
        img = (
            f'<img class="thumb" src="{html.escape(r["image_url"], quote=True)}" loading="lazy">'
            if r.get("image_url")
            else '<div class="thumb-empty">🛒</div>'
        )
        url = html.escape(r.get("url") or "#", quote=True)
        cards.append(
            f'<div class="ds-card">{img}<div class="body">'
            f'<div class="toprow"><span class="price">{price}</span>'
            f'<span class="ds-score {score_cls}">{score} · {verdict}</span></div>'
            f'<div class="title">{title}</div>'
            f'<div class="meta">{meta}</div>'
            f'<div class="reason">{reason}</div>'
            f'<div>{chips}</div>'
            f'<div class="footer"><a class="fb-link" href="{url}" target="_blank">'
            f"Open on Facebook ↗</a></div>"
            f"</div></div>"
        )
    st.markdown(f'<div class="ds-grid">{"".join(cards)}</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------- sidebar
def render_sidebar(db):
    with st.sidebar:
        st.markdown("### ⚙️ Settings")
        home_addr = db.get_setting("home_address") or ""
        with st.form("settings"):
            addr = st.text_input(
                "Home address",
                value=home_addr,
                help="Used for drive-time estimates. Street address or town + state.",
            )
            market_slug = st.text_input(
                "Marketplace city slug (optional)",
                value=db.get_setting("market_slug") or "",
                help="e.g. 'atlanta' for facebook.com/marketplace/atlanta. "
                "Leave blank to use your Facebook account's default area.",
            )
            junk = st.text_area(
                "Junk keywords (one per line)",
                value="\n".join(db.get_setting("junk_keywords") or []),
                help="Listings whose title contains any of these are dropped for free.",
            )
            fetch_desc = st.checkbox(
                "Fetch descriptions for top 10 (slower, better AI scoring)",
                value=bool(db.get_setting("fetch_descriptions")),
            )
            if st.form_submit_button("Save settings", use_container_width=True):
                db.set_setting("market_slug", market_slug.strip() or None)
                db.set_setting(
                    "junk_keywords", [k.strip() for k in junk.splitlines() if k.strip()]
                )
                db.set_setting("fetch_descriptions", fetch_desc)
                if addr.strip() and addr.strip() != home_addr:
                    with st.spinner("Geocoding address…"):
                        latlon = geo.geocode(addr.strip())
                    if latlon:
                        db.set_setting("home_address", addr.strip())
                        db.set_setting("home_latlon", list(latlon))
                        st.success(f"Home set ({latlon[0]:.3f}, {latlon[1]:.3f})")
                    else:
                        st.error("Couldn't geocode that address — try 'Town, ST'.")
                elif addr.strip():
                    db.set_setting("home_address", addr.strip())
                st.toast("Settings saved")
        if db.get_setting("home_latlon"):
            st.caption(f"🏠 {db.get_setting('home_address')}")
        else:
            st.warning("Set a home address to get drive times.")

        st.divider()
        st.markdown("### 🕘 Past searches")
        done_jobs = [j for j in db.recent_jobs(15) if j["status"] in ("done", "error")]
        for j in done_jobs[:8]:
            label = f"“{j['query']}” · {fmt_ago(j.get('created_at'))}"
            if st.button(label, key=f"hist-{j['id']}", use_container_width=True):
                st.session_state["active_job"] = j["id"]
                st.rerun()


# ---------------------------------------------------------------- main
def main():
    inject_css()
    require_password()
    db = get_db()

    st.markdown(
        '<div class="ds-hero"><h1>Deal<span class="accent">Scout</span></h1>'
        "<p>Tell me what you want — I’ll scrape Marketplace, score the deals, "
        "and tell you how far the drive is.</p></div>",
        unsafe_allow_html=True,
    )
    worker_up = render_worker_status(db)
    render_sidebar(db)

    with st.form("search"):
        query = st.text_input(
            "What are you looking for?",
            placeholder="e.g. kayak, mini excavator, free firewood, herman miller chair…",
        )
        c1, c2, c3 = st.columns(3)
        max_price = c1.number_input("Max price ($)", min_value=0, value=0, step=25,
                                    help="0 = no cap")
        max_drive = c2.number_input("Max drive (minutes)", min_value=0, value=45, step=5,
                                    help="0 = no limit")
        include_seen = c3.checkbox("Include previously seen", value=False)
        submitted = st.form_submit_button("🔎 Find deals", use_container_width=True,
                                          type="primary")

    if submitted and query.strip():
        if not worker_up:
            st.error("The scraper on your Mac is offline — wake it up and try again.")
        else:
            job_id = db.submit_job(
                query.strip(),
                max_price=float(max_price) or None,
                max_drive_min=int(max_drive) or None,
                include_seen=include_seen,
            )
            st.session_state["active_job"] = job_id
            st.rerun()

    job_id = st.session_state.get("active_job")
    if not job_id:
        return
    job = db.get_job(job_id)
    if not job:
        return

    st.markdown(f"#### Results for “{html.escape(job['query'])}”")
    if job["status"] in ("queued", "running"):
        msg = job.get("progress") or (
            "Waiting for the scraper to pick this up…" if job["status"] == "queued"
            else "Working…"
        )
        with st.status(msg, state="running"):
            st.caption("Searches usually take 30–90 seconds.")
        time.sleep(POLL_EVERY_SECONDS)
        st.rerun()
    elif job["status"] == "error":
        st.error(f"Search failed: {job.get('error') or 'unknown error'}")
    else:
        st.caption(job.get("progress") or "")
        render_results(db.get_results(job_id))


main()
