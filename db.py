"""Postgres-backed persistence for the poller.

Replaces the local-file approach (processed.jsonl / review_queue.jsonl) so
state survives Render redeploys. Same shape, different storage.

Reads DATABASE_URL from env. Connects lazily and reuses one connection
(thread-safe with a lock). Auto-creates tables on first use.

Schema:
  poller_processed     - cf_payment_id PK, product, processed_at
  poller_review_queue  - id PK, ts, product, reason, cf_payment_id, payload jsonb
"""
import json
import os
import threading

import psycopg2
import psycopg2.extras

_conn = None
_conn_lock = threading.Lock()
_schema_ready = False


def _connect():
    """Open a fresh Postgres connection. Render's connection string already
    contains sslmode, host, db, etc. We rely on autocommit so each statement
    commits immediately — no manual BEGIN/COMMIT bookkeeping in callers."""
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set in environment")
    conn = psycopg2.connect(url, connect_timeout=10)
    conn.autocommit = True
    return conn


def _ensure_schema(conn):
    global _schema_ready
    if _schema_ready:
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS poller_processed (
                cf_payment_id TEXT PRIMARY KEY,
                product       TEXT,
                processed_at  TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS poller_review_queue (
                id            SERIAL PRIMARY KEY,
                ts            TIMESTAMPTZ DEFAULT NOW(),
                product       TEXT,
                reason        TEXT,
                cf_payment_id TEXT,
                payload       JSONB
            );
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_review_queue_ts "
            "ON poller_review_queue (ts DESC);"
        )
    _schema_ready = True


def _get_conn():
    """Return a ready-to-use connection. Reconnects if the cached one
    looks dead. Holds a lock so two threads don't race on reconnect."""
    global _conn
    with _conn_lock:
        if _conn is None or _conn.closed:
            _conn = _connect()
            _ensure_schema(_conn)
            return _conn
        # Cheap ping — if the connection went away, recreate.
        try:
            with _conn.cursor() as cur:
                cur.execute("SELECT 1")
        except Exception:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = _connect()
            _ensure_schema(_conn)
        return _conn


def load_processed():
    """Return all cf_payment_ids already handled. Called once at poller
    startup to warm the in-memory dedup set."""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT cf_payment_id FROM poller_processed")
        return {row[0] for row in cur.fetchall()}


def mark_processed(cf_payment_id, product):
    """Insert (or no-op if already present). ON CONFLICT keeps this safe
    even if two ticks race on the same payment."""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO poller_processed (cf_payment_id, product) "
            "VALUES (%s, %s) ON CONFLICT (cf_payment_id) DO NOTHING",
            (cf_payment_id, product),
        )


def log_review(product, reason, cf_payment_id, payload):
    """Append one entry to the review queue. payload is the dict of row +
    extras (lead, original payload, etc.) — stored as jsonb so we can query
    it later if we want."""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO poller_review_queue (product, reason, cf_payment_id, payload) "
            "VALUES (%s, %s, %s, %s::jsonb)",
            (product, reason, cf_payment_id, json.dumps(payload, ensure_ascii=False)),
        )


def fetch_review_queue(limit=500):
    """Return recent review entries, newest first. Used by the /api/review-queue
    endpoint to drive the dashboard's edge-case view."""
    conn = _get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, ts, product, reason, cf_payment_id, payload "
            "FROM poller_review_queue ORDER BY ts DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "ts": r["ts"].strftime("%Y-%m-%dT%H:%M:%S") if r["ts"] else None,
            "product": r["product"],
            "reason": r["reason"],
            "cf_payment_id": r["cf_payment_id"],
            **(r["payload"] or {}),
        })
    return out
