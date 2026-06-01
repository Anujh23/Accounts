"""Shared collection logic — used by both the manual dashboard (app.py)
and the auto webhook (webhook.py). Keeps session management, decision
rules, and payload building in one place so both paths stay in sync."""
import json
import threading
import time
from datetime import date

from crm import eli as crm_eli, nbl as crm_nbl
from cashfree import eli as cf_eli, nbl as cf_nbl

ALIVE_TTL = 300         # skip session_alive ping if used in last N seconds
AMOUNT_TOLERANCE = 5.0  # ₹5 round-off — |cf - repay| <= 5 counts as exact close

PRODUCTS = {
    "ELI": {"crm": crm_eli, "cashfree": cf_eli},
    "NBL": {"crm": crm_nbl, "cashfree": cf_nbl},
}
_session_locks = {p: threading.Lock() for p in PRODUCTS}
_sessions      = {p: None for p in PRODUCTS}
_last_alive_at = {p: 0.0  for p in PRODUCTS}


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


def decide_action(cf_amount, repay_amount, no_of_days, real_days):
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


def was_accepted(product, http_code, body):
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


def build_payload(product, cf_amount, reference_no, lead_info, status, remark):
    """Build the /admin/setCollection form payload. NBL stores amounts as
    "65958.00" — passing "65958" silently fails. collectionSource /
    interestAmount are ELI-only fields."""
    payload = {
        "collectedAmount":  f"{cf_amount:.2f}",
        "collectedMode":    "Account",
        "referenceNo":      str(reference_no),
        "collectedDate":    date.today().isoformat(),
        "discountAmount":   "0.00",
        "settlemenAmount":  "0.00",
        "status":           status,
        "remark":           remark,
        "penaltyAmount":    "0.00",
        "loanNo":           lead_info["loanNo"],
        "contactID":        lead_info["contactID"],
        "leadID":           lead_info["leadID"],
    }
    if product == "ELI":
        payload["collectionSource"] = "Collection"
        payload["interestAmount"] = "0.00"
    return payload
