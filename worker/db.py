"""Job/results store shared by the Streamlit UI and the Mac worker.

Two backends behind one interface:
  - SupabaseDB when SUPABASE_URL + SUPABASE_SERVICE_KEY are set (production)
  - LocalDB (SQLite) otherwise, so the whole app runs locally with no cloud setup

Keep this module import-light: the Streamlit Cloud app imports it, so no
playwright or other worker-only dependencies here.
"""

import datetime
import json
import os
import sqlite3
import uuid
from pathlib import Path

DEALSCOUT_HOME = Path(os.environ.get("DEALSCOUT_HOME", Path.home() / ".dealscout"))

JOB_FIELDS = (
    "id, query, max_price, max_drive_min, include_seen, status, error, "
    "progress, created_at, started_at, finished_at"
)


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_ts(value):
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        return value
    value = value.replace("Z", "+00:00")
    dt = datetime.datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


class LocalDB:
    """SQLite backend for local dev / running everything on the Mac."""

    def __init__(self, path=None):
        self.path = Path(path) if path else DEALSCOUT_HOME / "local.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    query TEXT NOT NULL,
                    max_price REAL,
                    max_drive_min INTEGER,
                    include_seen INTEGER DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'queued',
                    error TEXT,
                    progress TEXT,
                    created_at TEXT,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT,
                    listing_id TEXT,
                    title TEXT,
                    price REAL,
                    price_text TEXT,
                    location TEXT,
                    url TEXT,
                    image_url TEXT,
                    drive_minutes INTEGER,
                    score INTEGER,
                    verdict TEXT,
                    reason TEXT,
                    flags TEXT,
                    seen_before INTEGER DEFAULT 0,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS worker_heartbeat (
                    id INTEGER PRIMARY KEY,
                    last_seen TEXT
                );
                """
            )

    # -- jobs ------------------------------------------------------------
    def submit_job(self, query, max_price=None, max_drive_min=None, include_seen=False):
        job_id = str(uuid.uuid4())
        with self._conn() as c:
            c.execute(
                "INSERT INTO jobs (id, query, max_price, max_drive_min, include_seen,"
                " status, created_at) VALUES (?,?,?,?,?, 'queued', ?)",
                (job_id, query, max_price, max_drive_min, int(include_seen), utcnow().isoformat()),
            )
        return job_id

    def get_job(self, job_id):
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def recent_jobs(self, limit=15):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def claim_next_job(self):
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            updated = c.execute(
                "UPDATE jobs SET status='running', started_at=? WHERE id=? AND status='queued'",
                (utcnow().isoformat(), row["id"]),
            ).rowcount
            if not updated:
                return None
        job = dict(row)
        job["status"] = "running"
        return job

    def update_job(self, job_id, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    # -- results ---------------------------------------------------------
    def add_results(self, job_id, rows):
        now = utcnow().isoformat()
        with self._conn() as c:
            c.executemany(
                "INSERT INTO results (job_id, listing_id, title, price, price_text,"
                " location, url, image_url, drive_minutes, score, verdict, reason,"
                " flags, seen_before, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        job_id,
                        r.get("listing_id"),
                        r.get("title"),
                        r.get("price"),
                        r.get("price_text"),
                        r.get("location"),
                        r.get("url"),
                        r.get("image_url"),
                        r.get("drive_minutes"),
                        r.get("score"),
                        r.get("verdict"),
                        r.get("reason"),
                        json.dumps(r.get("flags") or []),
                        int(r.get("seen_before") or 0),
                        now,
                    )
                    for r in rows
                ],
            )

    def get_results(self, job_id):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM results WHERE job_id=? ORDER BY score DESC", (job_id,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["flags"] = json.loads(d.get("flags") or "[]")
            out.append(d)
        return out

    # -- settings --------------------------------------------------------
    def set_setting(self, key, value):
        with self._conn() as c:
            c.execute(
                "INSERT INTO settings (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def get_setting(self, key, default=None):
        with self._conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    # -- heartbeat -------------------------------------------------------
    def beat(self):
        with self._conn() as c:
            c.execute(
                "INSERT INTO worker_heartbeat (id, last_seen) VALUES (1, ?)"
                " ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen",
                (utcnow().isoformat(),),
            )

    def worker_last_seen(self):
        with self._conn() as c:
            row = c.execute("SELECT last_seen FROM worker_heartbeat WHERE id=1").fetchone()
        return _parse_ts(row["last_seen"]) if row else None


class SupabaseDB:
    """Supabase (PostgREST) backend. Schema in deploy/schema.sql."""

    def __init__(self, url, service_key):
        from supabase import create_client

        self.client = create_client(url, service_key)

    # -- jobs ------------------------------------------------------------
    def submit_job(self, query, max_price=None, max_drive_min=None, include_seen=False):
        res = (
            self.client.table("jobs")
            .insert(
                {
                    "query": query,
                    "max_price": max_price,
                    "max_drive_min": max_drive_min,
                    "include_seen": include_seen,
                }
            )
            .execute()
        )
        return res.data[0]["id"]

    def get_job(self, job_id):
        res = self.client.table("jobs").select("*").eq("id", job_id).execute()
        return res.data[0] if res.data else None

    def recent_jobs(self, limit=15):
        res = (
            self.client.table("jobs")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data

    def claim_next_job(self):
        res = (
            self.client.table("jobs")
            .select("*")
            .eq("status", "queued")
            .order("created_at")
            .limit(1)
            .execute()
        )
        if not res.data:
            return None
        job = res.data[0]
        upd = (
            self.client.table("jobs")
            .update({"status": "running", "started_at": utcnow().isoformat()})
            .eq("id", job["id"])
            .eq("status", "queued")
            .execute()
        )
        if not upd.data:
            return None  # someone else claimed it
        job["status"] = "running"
        return job

    def update_job(self, job_id, **fields):
        self.client.table("jobs").update(fields).eq("id", job_id).execute()

    # -- results ---------------------------------------------------------
    def add_results(self, job_id, rows):
        if not rows:
            return
        payload = []
        for r in rows:
            payload.append(
                {
                    "job_id": job_id,
                    "listing_id": r.get("listing_id"),
                    "title": r.get("title"),
                    "price": r.get("price"),
                    "price_text": r.get("price_text"),
                    "location": r.get("location"),
                    "url": r.get("url"),
                    "image_url": r.get("image_url"),
                    "drive_minutes": r.get("drive_minutes"),
                    "score": r.get("score"),
                    "verdict": r.get("verdict"),
                    "reason": r.get("reason"),
                    "flags": r.get("flags") or [],
                    "seen_before": bool(r.get("seen_before")),
                }
            )
        self.client.table("results").insert(payload).execute()

    def get_results(self, job_id):
        res = (
            self.client.table("results")
            .select("*")
            .eq("job_id", job_id)
            .order("score", desc=True)
            .execute()
        )
        return res.data

    # -- settings --------------------------------------------------------
    def set_setting(self, key, value):
        self.client.table("settings").upsert({"key": key, "value": value}).execute()

    def get_setting(self, key, default=None):
        res = self.client.table("settings").select("value").eq("key", key).execute()
        return res.data[0]["value"] if res.data else default

    # -- heartbeat -------------------------------------------------------
    def beat(self):
        self.client.table("worker_heartbeat").upsert(
            {"id": 1, "last_seen": utcnow().isoformat()}
        ).execute()

    def worker_last_seen(self):
        res = self.client.table("worker_heartbeat").select("last_seen").eq("id", 1).execute()
        return _parse_ts(res.data[0]["last_seen"]) if res.data else None


def get_db():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if url and key:
        return SupabaseDB(url, key)
    return LocalDB()


def worker_online(db, stale_seconds=90):
    last = db.worker_last_seen()
    if not last:
        return False
    return (utcnow() - last).total_seconds() < stale_seconds
