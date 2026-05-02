"""Flask dashboard for ELI / NBL x Cashfree reconciliation."""
import json
import threading
import time
from datetime import date

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request

from crm import eli as crm_eli, nbl as crm_nbl
from cashfree import eli as cf_eli, nbl as cf_nbl
from accounts import bp as accounts_bp

load_dotenv()
app = Flask(__name__)
app.register_blueprint(accounts_bp)

ALIVE_TTL = 300         # skip session_alive ping if used in last N seconds
AMOUNT_TOLERANCE = 5.0  # ₹5 round-off — |cf - repay| <= 5 counts as exact close

# Each product is fully isolated: separate CRM client, separate Cashfree wrapper,
# separate session, separate lock. Credentials never cross between products.
PRODUCTS = {
    "ELI": {"crm": crm_eli, "cashfree": cf_eli},
    "NBL": {"crm": crm_nbl, "cashfree": cf_nbl},
}
_session_locks = {p: threading.Lock() for p in PRODUCTS}
_sessions      = {p: None for p in PRODUCTS}
_last_alive_at = {p: 0.0  for p in PRODUCTS}


def _product(req):
    """Pick the product from request body; default ELI for backwards compat."""
    body = req.get_json(silent=True) or {}
    p = (body.get("product") or "ELI").upper()
    if p not in PRODUCTS:
        raise ValueError(f"unknown product: {p!r}")
    return p


def get_session(product):
    """Return a logged-in CRM session for the given product, re-login lazily."""
    crm = PRODUCTS[product]["crm"]
    with _session_locks[product]:
        now = time.time()
        if _sessions[product] is None:
            print(f"[{product}] logging in")
            _sessions[product] = crm.login()
            _last_alive_at[product] = now
        elif now - _last_alive_at[product] > ALIVE_TTL:
            if not crm.session_alive(_sessions[product]):
                print(f"[{product}] session expired, re-logging in")
                _sessions[product] = crm.login()
            _last_alive_at[product] = now
        return _sessions[product]


def _decide_action(cf_amount, repay_amount, no_of_days, real_days):
    """Return (ok, status, remark, reason).
    |cf - repay| <= ₹5 -> Closed / PAYDAY PRECLOSE (needs days).
    cf  <  repay - 5   -> Part Payment.
    cf  >  repay + 5   -> overpayment, refused.
    """
    if repay_amount is None:
        return False, None, None, "Repayment Amount could not be parsed from CRM profile."
    diff = cf_amount - repay_amount
    if abs(diff) <= AMOUNT_TOLERANCE:
        if no_of_days is None or real_days is None:
            return False, None, None, "No of Days / Real Days could not be parsed."
        if real_days < no_of_days:
            return True, "PAYDAY PRECLOSE", "Payday preclose", None
        return True, "Closed", "Closed via Cashfree reconciliation", None
    if diff < 0:
        return True, "Part Payment", "Part payment", None
    return False, None, None, (f"Overpayment: CF ₹{cf_amount:.2f} > "
                               f"outstanding ₹{repay_amount:.2f}.")


@app.errorhandler(ValueError)
def _value_error(e):
    return jsonify({"error": str(e)}), 400


@app.route("/")
def index():
    return redirect("/accounts/")


@app.route("/autocollection")
def autocollection():
    return render_template("dashboard.html")


@app.route("/api/cashfree", methods=["POST"])
def api_cashfree():
    body = request.get_json()
    product = _product(request)
    cashfree = PRODUCTS[product]["cashfree"]
    _, rows = cashfree.fetch(body["start"], body["end"], enrich_bank_reference=False)
    latest = {}
    for r in rows:
        ph = (r.get("customer_phone") or "").strip()
        if not ph:
            continue
        prev = latest.get(ph)
        if prev is None or r.get("event_time", "") > prev.get("event_time", ""):
            latest[ph] = r
    out = sorted(latest.values(), key=lambda r: r.get("event_time", ""), reverse=True)
    return jsonify({"rows": out, "raw_count": len(rows), "latest_count": len(out),
                    "product": product})


@app.route("/api/match", methods=["POST"])
def api_match():
    product = _product(request)
    crm = PRODUCTS[product]["crm"]
    mobile = request.get_json()["mobile"]
    s = get_session(product)
    lead = crm.get_latest_lead(s, mobile)
    if not lead:
        return jsonify({"ok": False, "reason": f"no lead found in {product}"})
    if lead.get("Status") in crm.CLOSED_STATUSES:
        return jsonify({"ok": False, "reason": f"already {lead['Status']}",
                        "leadID": lead["LeadID"]})
    info = crm.get_repay_info(s, lead["LeadID"])
    info.update(name=lead.get("Name", ""), status=lead.get("Status", ""),
                mobile=lead.get("Mobile", ""), product=product)
    return jsonify({"ok": True, "lead": info})


@app.route("/api/update", methods=["POST"])
def api_update():
    p = request.get_json()
    product = _product(request)
    crm = PRODUCTS[product]["crm"]
    dry_run = p.get("dry_run", True)
    try:
        cf_amount = float(p["amount"])
    except (TypeError, ValueError):
        return jsonify({"status": 400, "body": "Invalid amount.", "dry_run": dry_run}), 400
    ok, status, remark, reason = _decide_action(
        cf_amount, p.get("repay_amount"), p.get("no_of_days"), p.get("real_days"))
    if not ok:
        return jsonify({
            "status": 400,
            "body": f"Refusing to post: {reason} Inspect the loan in CRM manually.",
            "dry_run": dry_run,
        }), 400
    # Amounts as 2-decimal strings (NBL stores as "65958.00" in collection log;
    # passing "65958" silently fails). collectionSource / interestAmount only
    # exist in ELI's form — NBL form doesn't have them.
    payload = {
        "collectedAmount":  f"{cf_amount:.2f}",
        "collectedMode":    "Account",
        "referenceNo":      str(p["referenceNo"]),
        "collectedDate":    date.today().isoformat(),
        "discountAmount":   "0.00",
        "settlemenAmount":  "0.00",
        "status":           status,
        "remark":           remark,
        "penaltyAmount":    "0.00",
        "loanNo":           p["loanNo"],
        "contactID":        p["contactID"],
        "leadID":           p["leadID"],
    }
    if product == "ELI":
        payload["collectionSource"] = "Collection"
        payload["interestAmount"] = "0.00"
    if dry_run:
        print(f"[{product} DRY RUN] would POST setCollection:")
        print(json.dumps(payload, indent=2))
        return jsonify({
            "status": 200, "success": True,
            "body": f"DRY RUN ({product}) — payload logged to terminal, NOT posted to CRM.",
            "payload": payload,
            "dry_run": True,
        })
    print(f"[{product} LIVE] POSTING setCollection:")
    print(json.dumps(payload, indent=2))
    code, body = crm.set_collection(get_session(product), payload)
    success = _was_accepted(product, code, body)
    if not success:
        print(f"[{product}] CRM REJECTED — HTTP {code}  body={body[:300]!r}")
    return jsonify({"status": code, "success": success, "body": body[:2000],
                    "payload": payload, "dry_run": False, "product": product})


def _was_accepted(product, http_code, body):
    """ELI returns JSON like {"status":1}; NBL returns a bare number where >=1 means success.
    HTTP 200 alone doesn't mean the CRM saved the collection."""
    if http_code != 200:
        return False
    text = (body or "").strip()
    if product == "ELI":
        try:
            return json.loads(text).get("status") == 1
        except Exception:
            return False
    if product == "NBL":
        try:
            return int(text) >= 1
        except ValueError:
            return False
    return False


if __name__ == "__main__":
    for p in PRODUCTS:
        try:
            get_session(p)
            print(f"[{p}] startup login OK")
        except Exception as e:
            print(f"[{p}] startup login FAILED (will retry on first request): {e}")
    app.run(debug=False, port=5000, threaded=True)
