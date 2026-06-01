"""Flask dashboard for ELI / NBL x Cashfree reconciliation."""
import json

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request

import core
from accounts import bp as accounts_bp
from poller import bp as poller_bp, start_background as start_poller

load_dotenv()
app = Flask(__name__)
app.register_blueprint(accounts_bp)
app.register_blueprint(poller_bp)
# Auto-post poller runs in a background thread. Idempotent — safe to
# call here at module import (gunicorn -w 1 means a single worker, so
# this fires exactly once).
start_poller()


def _product(req):
    """Pick the product from request body; default ELI for backwards compat."""
    body = req.get_json(silent=True) or {}
    p = (body.get("product") or "ELI").upper()
    if p not in core.PRODUCTS:
        raise ValueError(f"unknown product: {p!r}")
    return p


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
    cashfree = core.PRODUCTS[product]["cashfree"]
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
    crm = core.PRODUCTS[product]["crm"]
    mobile = request.get_json()["mobile"]
    s = core.get_session(product)
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
    crm = core.PRODUCTS[product]["crm"]
    dry_run = p.get("dry_run", True)
    try:
        cf_amount = float(p["amount"])
    except (TypeError, ValueError):
        return jsonify({"status": 400, "body": "Invalid amount.", "dry_run": dry_run}), 400
    ok, status, remark, reason = core.decide_action(
        cf_amount, p.get("repay_amount"), p.get("no_of_days"), p.get("real_days"))
    if not ok:
        return jsonify({
            "status": 400,
            "body": f"Refusing to post: {reason} Inspect the loan in CRM manually.",
            "dry_run": dry_run,
        }), 400
    lead_info = {
        "loanNo":    p["loanNo"],
        "contactID": p["contactID"],
        "leadID":    p["leadID"],
    }
    payload = core.build_payload(product, cf_amount, p["referenceNo"],
                                 lead_info, status, remark)
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
    code, body = crm.set_collection(core.get_session(product), payload)
    success = core.was_accepted(product, code, body)
    if not success:
        print(f"[{product}] CRM REJECTED — HTTP {code}  body={body[:300]!r}")
    return jsonify({"status": code, "success": success, "body": body[:2000],
                    "payload": payload, "dry_run": False, "product": product})


if __name__ == "__main__":
    for p in core.PRODUCTS:
        try:
            core.get_session(p)
            print(f"[{p}] startup login OK")
        except Exception as e:
            print(f"[{p}] startup login FAILED (will retry on first request): {e}")
    app.run(debug=False, port=5000, threaded=True)
