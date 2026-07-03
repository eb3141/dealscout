# DealScout 🔎

Personal Facebook Marketplace deal finder. Type what you want; a worker on the
Mac scrapes Marketplace with your logged-in session, filters listings for free
(price rules, junk keywords, already-seen dedupe, drive-time cutoff), then makes
**one** Codex CLI call to score the survivors as deals. Results render as cards
in a password-protected Streamlit app. Runtime cost: **$0/month**.

```
Phone/laptop ──▶ Streamlit Cloud UI (password, stable URL)
                      │  jobs / results
                      ▼
                Supabase free Postgres
                      ▲  polls every 4s
                      │
                Mac worker (Playwright + Codex CLI, launchd)
```

With no `SUPABASE_URL` configured, both the UI and worker fall back to a shared
local SQLite file (`~/.dealscout/local.sqlite`) — everything runs on the Mac
alone for testing.

## One-time setup

### 1. Mac worker
```bash
cd ~/Desktop/dealscout
python3 -m venv .venv && .venv/bin/pip install -r requirements-worker.txt
.venv/bin/playwright install chromium
.venv/bin/python -m worker.fb_login     # log into Facebook once in the window
```

### 2. Supabase (free tier)
1. https://supabase.com → New project (any name/region).
2. SQL Editor → paste `deploy/schema.sql` → Run.
3. Project Settings → API: copy the **URL** and the **service_role key**.
4. Put them in `~/Desktop/dealscout/.env`:
   ```
   SUPABASE_URL=https://xxxx.supabase.co
   SUPABASE_SERVICE_KEY=eyJ...
   ```

### 3. Streamlit Cloud
1. https://share.streamlit.io → New app → this repo, `streamlit_app.py`.
2. App settings → Secrets:
   ```toml
   SUPABASE_URL = "https://xxxx.supabase.co"
   SUPABASE_SERVICE_KEY = "eyJ..."
   PASSWORD_SHA256 = "<sha256 of your password>"
   ```
   Generate the hash: `python3 -c "import hashlib;print(hashlib.sha256(b'yourpassword').hexdigest())"`
3. After deploy, put the app URL into `.github/workflows/keep-alive.yml`
   (or set an `APP_URL` repository variable) so the 6-hourly ping keeps it awake.

### 4. Worker as a service (starts on login, restarts on crash)
```bash
cp deploy/com.elliott.dealscout-worker.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.elliott.dealscout-worker.plist
tail -f /tmp/dealscout-worker.log
```

## Daily use
Open the app URL → enter password → type what you want, set max price and max
drive minutes → Find deals. Searches take 30–90s. The scraper-status pill shows
red when the Mac is asleep. Set your home address once in Settings (sidebar)
to get drive times. Repeated searches hide listings you've already seen unless
you tick “include previously seen”.

## Efficiency notes
- Scoring uses the **Codex CLI** (ChatGPT subscription) — no API bills, no
  Anthropic credits. One batched call per search, capped at 30 listings.
- Geocoding (Nominatim) and routing (OSRM demo server) are free and cached
  forever in `~/.dealscout/geo.sqlite`.
- Scraping is human-paced (2–5s waits, one page, ≤60 listings) to stay
  low-profile on Facebook. If FB logs the session out, the worker reports it
  in the UI; rerun `python -m worker.fb_login`.

## Local dev (no cloud at all)
```bash
.venv/bin/python -m worker.main                 # terminal 1
.venv/bin/python -m streamlit run streamlit_app.py   # terminal 2
```
Local password lives in `.streamlit/secrets.toml` (gitignored).
