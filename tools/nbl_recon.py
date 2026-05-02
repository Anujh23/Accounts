"""Probe NBL admin endpoints to check if shape matches ELI.
Reads NBL_USERNAME / NBL_PASSWORD from .env. Optional NBL_TEST_MOBILE
to test search + profile fetch.

Run from project root:  python tools/nbl_recon.py"""
import os
import re
import sys
import requests
from dotenv import load_dotenv

# Make project root importable so we can use cashfree.common and crm.eli
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
load_dotenv()
USER = os.getenv("NBL_USERNAME")
PASS = os.getenv("NBL_PASSWORD")
TEST_MOBILE = os.getenv("NBL_TEST_MOBILE")

if not USER or not PASS:
    print("ERROR: NBL_USERNAME or NBL_PASSWORD missing from .env")
    raise SystemExit(1)

BASE = "https://app.nextbigloan.co.in/admin"
s = requests.Session()
s.headers.update({
    "User-Agent": "Mozilla/5.0",
    "X-Requested-With": "XMLHttpRequest",
})


def hit(method, path, **kw):
    r = s.request(method, f"{BASE}/{path}", timeout=20, allow_redirects=False, **kw)
    print(f"  [{method}] /{path:40s} HTTP {r.status_code}  len={len(r.text)}  "
          f"loc={r.headers.get('Location','-')}")
    return r


print("=== 1. fetch login page (NBL uses /admin/index, not /admin/loginindex) ===")
r = hit("GET", "index")
if r.status_code != 200:
    print(f"  -> /index didn't return 200, trying /")
    r = hit("GET", "")
if r.status_code == 200:
    with open("nbl_login_page.html", "w", encoding="utf-8") as f:
        f.write(r.text)
    print("  -> saved nbl_login_page.html")
    # Discover login form: action URL + every input/select name
    forms = re.findall(r'<form[^>]*action=["\']([^"\']+)["\'][^>]*>(.*?)</form>', r.text, re.S | re.I)
    print(f"  -> {len(forms)} form(s) found on page")
    for i, (action, body) in enumerate(forms):
        names = re.findall(r'<(?:input|select|textarea)[^>]*name=["\']([^"\']+)["\']', body, re.I)
        types = re.findall(r'<input[^>]*type=["\']([^"\']+)["\']', body, re.I)
        print(f"    form[{i}] action={action!r}  inputs={names}  types={types}")

print("=== 2. doLogin (NBL uses employeeID, not userName) ===")
r = hit("POST", "login/doLogin", data={
    "employeeID": USER,
    "password":   PASS,
})
if r.status_code in (302, 303) and "login=false" in (r.headers.get("Location") or ""):
    print("  -> login REJECTED. Likely bad creds (field names verified from form).")

print("=== 3. dashboard (logged-in check) ===")
r = hit("GET", "dashboard")
logged_in = r.status_code == 200 and "logout" in r.text.lower()
print(f"  -> looks logged in: {logged_in}")
if r.status_code == 200:
    with open("nbl_dashboard.html", "w", encoding="utf-8") as f:
        f.write(r.text)
    print("  -> saved nbl_dashboard.html")

if not TEST_MOBILE:
    print()
    print("=== 3b. no NBL_TEST_MOBILE — pulling one from NBL Cashfree (today, then last 7 days) ===")
    from datetime import date, timedelta
    from cashfree.common import fetch_recon
    today = date.today()
    candidates = []
    for label, start, end in [
        ("today",      today,                    today),
        ("last 7 days", today - timedelta(days=6), today),
    ]:
        try:
            _, rows = fetch_recon(
                client_id=os.getenv("NBL_CLIENT_ID"),
                client_secret=os.getenv("NBL_CLIENT_SECRET"),
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                enrich_bank_reference=False,
            )
            print(f"  {label}: {len(rows)} CF rows")
            candidates = [r for r in rows if (r.get("customer_phone") or "").strip()]
            if candidates:
                break
        except Exception as e:
            print(f"  {label}: ERROR {e}")
    if not candidates:
        print("  -> no Cashfree payments to grab a mobile from. Set NBL_TEST_MOBILE manually.")
        raise SystemExit(0)
    pick = candidates[0]
    TEST_MOBILE = pick["customer_phone"].strip()
    print(f"  -> using mobile {TEST_MOBILE} (CF customer: {pick.get('customer_name')!r}, "
          f"Rs.{pick.get('order_amount')}, txn {pick.get('cf_payment_id')})")

print("=== 4. search by mobile ===")
r = hit("POST", "disbursedDataSearch", data={"searchData": TEST_MOBILE})
if r.status_code != 200:
    print("  -> search did not return 200, stopping")
    raise SystemExit(1)
with open("nbl_search.html", "w", encoding="utf-8") as f:
    f.write(r.text)
print("  -> saved nbl_search.html")

# Try to extract a LeadID from the search response (same heuristic as ELI parser)
leadids = re.findall(r"<td[^>]*>\s*(\d{5,7})\s*</td>", r.text)
print(f"  -> candidate LeadIDs spotted: {leadids[:5]}")
if not leadids:
    print("  -> no LeadID found in search HTML; share nbl_search.html for inspection")
    raise SystemExit(0)

lead_id = leadids[0]
print(f"=== 5. profile/{lead_id} ===")
r = hit("GET", f"profile/{lead_id}")
if r.status_code == 200:
    with open(f"nbl_profile_{lead_id}.html", "w", encoding="utf-8") as f:
        f.write(r.text)
    print(f"  -> saved nbl_profile_{lead_id}.html")
    # Probe with the live ELI parser regexes (single source of truth)
    from crm.eli import _PROFILE_FIELDS
    extras = [
        ("setCollection form", re.compile(r'<form[^>]*id=["\']collectionAdd["\']'), None),
        ("Part Payment opt",   re.compile(r'<option value="Part Payment">'),       None),
        ("Closed opt",         re.compile(r'<option value="Closed">'),             None),
        ("PAYDAY PRECLOSE opt",re.compile(r'<option value="PAYDAY PRECLOSE">'),    None),
    ]
    print()
    print("  Field probes (does ELI's regex hit NBL HTML?):")
    for key, rx, _ in list(_PROFILE_FIELDS) + extras:
        m = rx.search(r.text)
        val = m.group(1) if (m and m.lastindex) else ("(found)" if m else "MISSING")
        print(f"    {key:22s} -> {val}")
