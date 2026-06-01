"""Shared Cashfree reconciliation fetch logic.
Each per-product module supplies its own client_id / client_secret."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Python 3.14 has a thread-safety bug where datetime.strptime triggers a lazy
# import of _strptime that races between threads (poller + dashboard fetch).
# Force the lazy import here, single-threaded at module load, so concurrent
# calls don't see a half-initialized _strptime module.
datetime.strptime("2000-01-01", "%Y-%m-%d")

BANK_REF_WORKERS = 16

_session = requests.Session()
_adapter = HTTPAdapter(
    pool_connections=BANK_REF_WORKERS,
    pool_maxsize=BANK_REF_WORKERS,
    max_retries=Retry(total=2, backoff_factor=0.3,
                      status_forcelist=[429, 500, 502, 503, 504]),
)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def load_env():
    values = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    for key in ("EB_EMAIL", "EB_PASSWORD",
                "ELI_CLIENT_ID", "ELI_CLIENT_SECRET",
                "NBL_CLIENT_ID", "NBL_CLIENT_SECRET",
                "LDR_CLIENT_ID", "LDR_CLIENT_SECRET",
                "CPY_CLIENT_ID", "CPY_CLIENT_SECRET"):
        if key not in values and key in os.environ:
            values[key] = os.environ[key]
    return values


def to_iso_z(date_str, is_end=False):
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if is_end:
        return dt.strftime("%Y-%m-%dT23:59:59Z")
    return dt.strftime("%Y-%m-%dT00:00:00Z")


WANTED_FIELDS = [
    "event_time", "event_settlement_amount", "cf_payment_id",
    "customer_name", "customer_phone", "bank_reference", "customer_email",
    "event_status", "event_amount", "order_amount", "order_id",
    "payment_group", "payment_utr",
]


def fetch_recon(client_id, client_secret, start_date, end_date,
                enrich_bank_reference=True, quiet=False):
    if not client_id or not client_secret:
        raise RuntimeError("Cashfree client_id / client_secret not set")

    start_iso = to_iso_z(start_date, is_end=False)
    end_iso = to_iso_z(end_date, is_end=True)

    url = "https://api.cashfree.com/pg/recon"
    payload = {
        "pagination": {"limit": 100, "cursor": None},
        "filters": {"start_date": start_iso, "end_date": end_iso},
    }
    headers = {
        "x-client-id": client_id,
        "x-client-secret": client_secret,
        "x-api-version": "2023-08-01",
        "Content-Type": "application/json",
    }

    all_records = []
    cursor = None
    page_num = 0
    while True:
        page_num += 1
        payload["pagination"]["cursor"] = cursor
        r = _session.post(url, json=payload, headers=headers, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"Cashfree {r.status_code}: {r.text[:300]}")
        data = r.json()
        batch = data.get("data", [])
        all_records.extend(batch)
        if not quiet:
            print(f"[cashfree] page {page_num}: +{len(batch)} (total {len(all_records)})")
        cursor = data.get("cursor")
        if not cursor:
            break

    rows = []
    enrich_targets = []
    for rec in all_records:
        if rec.get("event_status") != "SUCCESS":
            continue
        if rec.get("payment_group") == "NPCI_SBC":
            continue

        row = {k: rec.get(k, "") for k in WANTED_FIELDS}
        phone = row.get("customer_phone", "")
        if phone and phone.startswith("+91"):
            row["customer_phone"] = phone[3:]
        et = row.get("event_time", "")
        if et and "+" in et:
            row["event_time"] = et.split("+")[0]
        row["bank_reference"] = ""

        rows.append(row)
        if enrich_bank_reference:
            enrich_targets.append(
                (len(rows) - 1, rec.get("order_id"), rec.get("cf_payment_id")))

    if enrich_targets:
        print(f"[cashfree] enriching {len(enrich_targets)} rows "
              f"with bank_reference using {BANK_REF_WORKERS} workers...")
        with ThreadPoolExecutor(max_workers=BANK_REF_WORKERS) as ex:
            futures = {
                ex.submit(_fetch_bank_reference, oid, pid, headers): idx
                for idx, oid, pid in enrich_targets
            }
            for f in futures:
                idx = futures[f]
                try:
                    rows[idx]["bank_reference"] = f.result()
                except Exception as e:
                    print(f"[cashfree] bank_ref worker error row {idx}: {e}")

    rows.sort(key=lambda x: x.get("event_time", ""), reverse=True)
    if not quiet:
        print(f"[cashfree] {len(rows)} SUCCESS rows after filters")
    return WANTED_FIELDS, rows


def _fetch_bank_reference(order_id, cf_payment_id, headers):
    if not order_id or not cf_payment_id:
        return ""
    try:
        url = f"https://api.cashfree.com/pg/orders/{order_id}/payments"
        r = _session.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return ""
        for p in r.json():
            if p.get("cf_payment_id") == cf_payment_id:
                return str(p.get("bank_reference") or "")
    except Exception as e:
        print(f"[cashfree] bank_reference fetch error for {order_id}: {e}")
    return ""


COLUMN_HEADERS = {
    "event_time": "Date",
    "event_settlement_amount": "Settlement Amount",
    "cf_payment_id": "Transaction ID",
    "customer_name": "Name",
    "customer_phone": "Phone",
    "bank_reference": "Bank Reference",
    "customer_email": "Customer Email",
    "event_status": "Status",
    "event_amount": "Event Amount",
    "order_amount": "Order Amount",
    "order_id": "Order ID",
    "payment_group": "Payment Group",
    "payment_utr": "Payment UTR",
}
