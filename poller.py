"""Background polling job — auto-posts Cashfree payments to CRM.

No webhook URL needed. Every POLL_INTERVAL_SECONDS this fetches today's
Cashfree payments for each product (ELI/NBL), matches them against the
CRM, and either posts to setCollection (AUTO_LIVE=1) or just logs the
payload (default — same dry-run idea as the dashboard).

State (processed cf_payment_ids + review queue entries) is persisted to
Postgres via db.py so it survives Render redeploys and worker recycles.
Falls back to in-memory only if DATABASE_URL is unset (degraded mode)."""
import json
import os
import threading
import time
from datetime import date

from flask import Blueprint, jsonify

import core
import db

bp = Blueprint("poller", __name__)

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "120"))  # 2 min default

_processed = set()  # cf_payment_ids handled in any tick (loaded from DB at start)
_processed_lock = threading.Lock()
_poller_started = False
_last_tick_at = 0.0
_last_tick_summary = {}


def _auto_live():
    return os.getenv("AUTO_LIVE", "").strip() == "1"


def _load_processed():
    """Hydrate the in-memory dedup set from Postgres on poller startup.
    If DB is unreachable we log it and start empty — the CRM's own UTR
    uniqueness check still prevents double-posting."""
    try:
        ids = db.load_processed()
        _processed.update(ids)
        print(f"[poller] loaded {len(_processed)} processed cf_payment_ids from DB")
    except Exception as e:
        print(f"[poller] FAILED to load processed from DB: {e}")


def _persist_processed(cf_id, product):
    """Insert into poller_processed table. Best-effort — a DB error here
    doesn't abort the tick (in-memory set already has it for this run)."""
    try:
        db.mark_processed(cf_id, product)
    except Exception as e:
        print(f"[poller] FAILED to persist {cf_id} to DB: {e}")


def _log_review(product, reason, row, extras=None):
    """Write one review entry to Postgres. Caller passes the original Cashfree
    row plus any extras (lead info, payload) — all bundled into the JSONB blob."""
    payload = {"row": row}
    if extras:
        payload.update(extras)
    cf_id = (row or {}).get("cf_payment_id")
    try:
        db.log_review(product, reason, cf_id, payload)
    except Exception as e:
        print(f"[poller:{product}] FAILED to write review to DB: {e}")


def _process_row(product, row):
    """Match + decide + (maybe) post for one Cashfree row.
    Returns one of: 'posted', 'dry_run', 'review', 'skip_closed', 'skip_dup'."""
    cf_id = row.get("cf_payment_id")
    if not cf_id:
        return "review"
    with _processed_lock:
        if cf_id in _processed:
            return "skip_dup"
        _processed.add(cf_id)
    _persist_processed(cf_id, product)

    mobile = (row.get("customer_phone") or "").strip()
    try:
        amount = float(row.get("order_amount"))
    except (TypeError, ValueError):
        _log_review(product, "bad order_amount", row)
        return "review"
    if not mobile:
        _log_review(product, "no mobile in CF row", row)
        return "review"

    crm = core.PRODUCTS[product]["crm"]
    try:
        s = core.get_session(product)
        lead = crm.get_latest_lead(s, mobile)
    except Exception as e:
        _log_review(product, f"CRM lookup failed: {e}", row)
        return "review"

    if not lead:
        _log_review(product, "no lead found in CRM", row)
        return "review"

    if lead.get("Status") in crm.CLOSED_STATUSES:
        # Already closed by someone — quiet skip, no review entry needed.
        return "skip_closed"

    try:
        info = crm.get_repay_info(s, lead["LeadID"])
    except Exception as e:
        _log_review(product, f"profile fetch failed: {e}", row,
                    extras={"leadID": lead.get("LeadID")})
        return "review"

    info.update(name=lead.get("Name", ""), status=lead.get("Status", ""),
                mobile=lead.get("Mobile", ""), product=product)

    ok, status, remark, reason = core.decide_action(
        amount, info.get("repay_amount"),
        info.get("no_of_days"), info.get("real_days"))
    if not ok:
        _log_review(product, f"decide_action refused: {reason}", row,
                    extras={"lead": info})
        return "review"

    payload = core.build_payload(product, amount, cf_id, info, status, remark)

    if not _auto_live():
        print(f"[poller:{product} DRY RUN] would POST setCollection for cf_payment_id={cf_id}:")
        print(json.dumps(payload, indent=2))
        return "dry_run"

    print(f"[poller:{product} LIVE] POSTING setCollection for cf_payment_id={cf_id}:")
    print(json.dumps(payload, indent=2))
    try:
        code, body = crm.set_collection(s, payload)
    except Exception as e:
        _log_review(product, f"set_collection threw: {e}", row,
                    extras={"payload": payload})
        return "review"

    if not core.was_accepted(product, code, body):
        print(f"[poller:{product}] CRM REJECTED — HTTP {code}  body={body[:300]!r}")
        _log_review(product, f"CRM rejected: HTTP {code} body={body[:300]}",
                    row, extras={"payload": payload})
        return "review"

    print(f"[poller:{product}] POSTED leadID={info['leadID']} status={status}")
    return "posted"


def _poll_once():
    """One tick: fetch today's Cashfree payments per product, process new rows."""
    global _last_tick_at, _last_tick_summary
    today = date.today().isoformat()
    summary = {}
    tick_start = time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[poller] === tick start @ {stamp} ===")
    for product in core.PRODUCTS:
        cashfree = core.PRODUCTS[product]["cashfree"]
        counts = {"posted": 0, "dry_run": 0, "review": 0,
                  "skip_closed": 0, "skip_dup": 0, "rows": 0}
        try:
            _, rows = cashfree.fetch(today, today, enrich_bank_reference=False,
                                     quiet=True)
        except Exception as e:
            print(f"[poller:{product}] fetch failed: {e}")
            summary[product] = {**counts, "error": str(e)}
            continue
        # Dedupe by phone — same logic as dashboard /api/cashfree.
        latest = {}
        for r in rows:
            ph = (r.get("customer_phone") or "").strip()
            if not ph:
                continue
            prev = latest.get(ph)
            if prev is None or r.get("event_time", "") > prev.get("event_time", ""):
                latest[ph] = r
        counts["rows"] = len(latest)
        for row in latest.values():
            outcome = _process_row(product, row)
            counts[outcome] = counts.get(outcome, 0) + 1
        summary[product] = counts
        print(f"[poller:{product}] posted={counts['posted']} "
              f"review={counts['review']} dry_run={counts['dry_run']} "
              f"skip_closed={counts['skip_closed']} skip_dup={counts['skip_dup']} "
              f"rows={counts['rows']}")
    elapsed = time.time() - tick_start
    _last_tick_at = time.time()
    _last_tick_summary = summary
    print(f"[poller] === tick done in {elapsed:.1f}s, next in {POLL_INTERVAL}s ===")


def _poll_loop():
    print(f"[poller] starting, interval={POLL_INTERVAL}s, auto_live={_auto_live()}")
    while True:
        try:
            _poll_once()
        except Exception as e:
            print(f"[poller] tick error: {e}")
        time.sleep(POLL_INTERVAL)


def start_background():
    """Called once at app startup. Idempotent — only starts the thread
    on the first call."""
    global _poller_started
    if _poller_started:
        return
    _poller_started = True
    _load_processed()
    t = threading.Thread(target=_poll_loop, daemon=True, name="cashfree-poller")
    t.start()


@bp.route("/api/review-queue", methods=["GET"])
def review_queue():
    """Review entries from Postgres (newest first) + poller status. Dashboard
    can hit this to show what auto-skipped and needs human attention."""
    try:
        entries = db.fetch_review_queue(limit=500)
        db_error = None
    except Exception as e:
        entries = []
        db_error = str(e)
    return jsonify({
        "entries": entries,
        "count": len(entries),
        "auto_live": _auto_live(),
        "poll_interval_seconds": POLL_INTERVAL,
        "last_tick_at": _last_tick_at,
        "last_tick_summary": _last_tick_summary,
        "processed_in_memory": len(_processed),
        "db_error": db_error,
    })
