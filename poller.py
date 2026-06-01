"""Background polling job — auto-posts Cashfree payments to CRM.

No webhook URL needed. Every POLL_INTERVAL_SECONDS this fetches today's
Cashfree payments for each product (ELI/NBL), matches them against the
CRM, and either posts to setCollection (AUTO_LIVE=1) or just logs the
payload (default — same dry-run idea as the dashboard).

Uses the existing API credentials (*_CLIENT_ID/*_CLIENT_SECRET +
CRM USERNAME/PASSWORD). Nothing extra to configure.

Duplicate guard: relies on the CRM closed-status check — a loan that's
already Closed (whether by this poller, a human in the dashboard, or
the CRM team directly) won't be touched again."""
import json
import os
import threading
import time
from datetime import date
from pathlib import Path

from flask import Blueprint, jsonify

import core

bp = Blueprint("poller", __name__)

REVIEW_LOG = Path(__file__).resolve().parent / "review_queue.jsonl"
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "120"))  # 2 min default

_processed = set()  # cf_payment_ids handled in this process — avoid re-work
_processed_lock = threading.Lock()
_poller_started = False
_last_tick_at = 0.0
_last_tick_summary = {}


def _auto_live():
    return os.getenv("AUTO_LIVE", "").strip() == "1"


def _log_review(product, reason, row, extras=None):
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "product": product,
        "reason": reason,
        "row": row,
    }
    if extras:
        entry.update(extras)
    try:
        with REVIEW_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[poller:{product}] FAILED to write review queue: {e}")


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
        # Already closed by someone (manual dashboard, CRM team, or earlier
        # poll tick that posted before the process restart). Quiet skip —
        # this is the expected steady-state for handled payments.
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
    for product in core.PRODUCTS:
        cashfree = core.PRODUCTS[product]["cashfree"]
        counts = {"posted": 0, "dry_run": 0, "review": 0,
                  "skip_closed": 0, "skip_dup": 0, "rows": 0}
        try:
            _, rows = cashfree.fetch(today, today, enrich_bank_reference=False)
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
        if counts["posted"] or counts["dry_run"] or counts["review"]:
            print(f"[poller:{product}] tick summary: {counts}")
    _last_tick_at = time.time()
    _last_tick_summary = summary


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
    t = threading.Thread(target=_poll_loop, daemon=True, name="cashfree-poller")
    t.start()


@bp.route("/api/review-queue", methods=["GET"])
def review_queue():
    """JSONL review log (newest first) + poller status. Dashboard can hit
    this to show what auto-skipped and needs human attention."""
    entries = []
    if REVIEW_LOG.exists():
        with REVIEW_LOG.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
        entries.reverse()
    return jsonify({
        "entries": entries,
        "count": len(entries),
        "auto_live": _auto_live(),
        "poll_interval_seconds": POLL_INTERVAL,
        "last_tick_at": _last_tick_at,
        "last_tick_summary": _last_tick_summary,
        "processed_in_memory": len(_processed),
    })
