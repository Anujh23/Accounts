"""CPY Cashfree wrapper. Reads only CPY_CLIENT_ID / CPY_CLIENT_SECRET."""
import os
from cashfree.common import fetch_recon


def fetch(start_date, end_date, enrich_bank_reference=True):
    cid = os.getenv("CPY_CLIENT_ID")
    sec = os.getenv("CPY_CLIENT_SECRET")
    if not cid or not sec:
        raise RuntimeError("CPY_CLIENT_ID / CPY_CLIENT_SECRET missing from .env")
    return fetch_recon(
        client_id=cid,
        client_secret=sec,
        start_date=start_date,
        end_date=end_date,
        enrich_bank_reference=enrich_bank_reference,
    )
