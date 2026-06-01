"""ELI Cashfree wrapper. Reads only ELI_CLIENT_ID / ELI_CLIENT_SECRET."""
import os
from cashfree.common import fetch_recon


def fetch(start_date, end_date, enrich_bank_reference=True, quiet=False):
    cid = os.getenv("ELI_CLIENT_ID")
    sec = os.getenv("ELI_CLIENT_SECRET")
    if not cid or not sec:
        raise RuntimeError("ELI_CLIENT_ID / ELI_CLIENT_SECRET missing from .env")
    return fetch_recon(
        client_id=cid,
        client_secret=sec,
        start_date=start_date,
        end_date=end_date,
        enrich_bank_reference=enrich_bank_reference,
        quiet=quiet,
    )
