"""ELI Easebuzz client. Reads EB_EMAIL / EB_PASSWORD from .env.
Uses persistent Playwright session — all API calls go through page.evaluate()
so they share the browser's TLS fingerprint and cookie jar (bypasses bot detection)."""
import glob
import json
import os
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ENV_FILE = Path(__file__).resolve().parent / ".env"


def _find_chromium_binary():
    """Find installed Chromium binary. Playwright 1.49+ prefers
    chromium-headless-shell which often isn't installed on Render free tier;
    fall back to the regular chromium binary that IS installed."""
    candidates = [
        "/opt/render/.cache/ms-playwright/chromium-*/chrome-linux64/chrome",
        "/opt/render/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
        os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome"),
        os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome"),
    ]
    for pattern in candidates:
        matches = sorted(glob.glob(pattern), reverse=True)
        if matches:
            print(f"[easebuzz] using chromium binary: {matches[0]}", flush=True)
            return matches[0]
    # Diagnostic: list what's actually in the cache directory
    for cache_dir in ("/opt/render/.cache/ms-playwright",
                      os.path.expanduser("~/.cache/ms-playwright")):
        if os.path.exists(cache_dir):
            print(f"[easebuzz] no match. {cache_dir} contents:",
                  os.listdir(cache_dir), flush=True)
        else:
            print(f"[easebuzz] cache dir not found: {cache_dir}", flush=True)
    return None


def _load_env():
    """Read .env file if present, then fall back to OS env (for Render etc.)."""
    values = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    for key in ("EB_EMAIL", "EB_PASSWORD", "EB_EMAIL_ELI", "EB_PASSWORD_ELI"):
        if not values.get(key) and os.environ.get(key):
            values[key] = os.environ[key]
    return values


class AuthError(Exception):
    pass


class EasebuzzClient:
    def __init__(self, email, password):
        self._email = email
        self._password = password
        self._lock = threading.Lock()
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None

    def _ensure_started(self):
        if self._pw is None:
            self._pw = sync_playwright().start()

    def _login(self):
        try:
            self._login_inner()
        except Exception as e:
            import traceback
            print(f"[easebuzz login] FAILED: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
            raise

    def _login_inner(self):
        if not self._email or not self._password:
            raise RuntimeError("Email / password not set")

        self._ensure_started()
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass

        print(f"[easebuzz login] {self._email}...")
        launch_kwargs = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-features=TranslateUI,site-per-process",
                "--disable-blink-features=AutomationControlled",
                "--no-zygote",
            ],
        }
        # If chromium-headless-shell isn't installed (Render free tier issue),
        # fall back to the full chromium binary in headless mode.
        chrome_bin = _find_chromium_binary()
        if chrome_bin:
            launch_kwargs["executable_path"] = chrome_bin
        self._browser = self._pw.chromium.launch(**launch_kwargs)
        self._ctx = self._browser.new_context(
            viewport={"width": 1280, "height": 720},
        )
        self._page = self._ctx.new_page()
        # Block analytics/tracking — saves bandwidth & ~30% page load time
        self._ctx.route(
            "**/*",
            lambda route: route.abort()
            if any(x in route.request.url for x in (
                "googletagmanager", "google-analytics", "doubleclick",
                "facebook.com", "cloudflareinsights"))
            else route.continue_(),
        )

        self._page.goto("https://dashboard.easebuzz.in/merchant/login/",
                        wait_until="networkidle", timeout=30000)
        self._page.wait_for_timeout(2000)
        self._page.locator('input[name="email"]').fill(self._email)
        self._page.locator('input[name="password"]').fill(self._password)
        self._page.locator('input[name="password"]').press("Enter")
        self._page.wait_for_url(
            lambda u: "/login" not in u and "/dashboard" in u, timeout=15000)
        self._page.wait_for_timeout(3000)

        # Mint drstoken via /get_drs_login/ → redirect_url
        drs = self._page.evaluate("""
            async () => {
                const csrf = document.cookie.match(/csrftoken=([^;]+)/);
                const r = await fetch('/merchant/api/v1/get_drs_login/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json',
                              'X-CSRFToken': csrf ? csrf[1] : ''},
                    body: '{}', credentials: 'include'
                });
                return await r.json();
            }
        """)
        if drs.get("redirect_url"):
            self._page.goto(drs["redirect_url"], timeout=15000)
            self._page.wait_for_timeout(2000)

        self._page.goto("https://dashboard.easebuzz.in/transaction",
                        wait_until="domcontentloaded", timeout=15000)
        self._page.wait_for_timeout(2500)

        names = {c["name"] for c in self._ctx.cookies()}
        if "easetoken" not in names or "drstoken" not in names:
            raise RuntimeError(f"Missing cookies after login: {names}")
        print("[easebuzz login] success")

    def fetch_transactions(self, start, end, status="", page_size=500,
                           max_pages=50):
        with self._lock:
            if self._page is None:
                self._login()
            try:
                return self._fetch_all_pages(start, end, status, page_size, max_pages)
            except AuthError:
                print("[easebuzz] auth expired, re-logging in")
                self._login()
                return self._fetch_all_pages(start, end, status, page_size, max_pages)

    def _fetch_all_pages(self, start, end, status, page_size, max_pages):
        all_rows = []
        cursor = None
        for i in range(max_pages):
            payload = self._call_api(start, end, status, page_size, cursor)
            data = payload.get("data") or {}
            rows = data.get("transaction_history", []) if isinstance(data, dict) else []
            all_rows.extend(rows)
            cursor = payload.get("next")
            print(f"[easebuzz] page {i+1}: +{len(rows)} (total {len(all_rows)}) "
                  f"next={'yes' if cursor else 'no'}")
            if not cursor or not rows:
                break
        return all_rows

    def _call_api(self, start, end, status, page_size, cursor=None):
        # Guard: page must be on dashboard.easebuzz.in for relative fetch
        # to hit the right backend (else nginx routes to drs.easebuzz.in → 405).
        if not self._page.url.startswith("https://dashboard.easebuzz.in"):
            print(f"[easebuzz] page on wrong origin ({self._page.url!r}), "
                  f"reparking on /transaction", flush=True)
            self._page.goto("https://dashboard.easebuzz.in/transaction",
                            wait_until="domcontentloaded", timeout=15000)
            self._page.wait_for_timeout(1500)

        body = {
            "merchant_id": 0, "page": cursor, "page_size": page_size,
            "columns": ("customer_phone,txn_date,customer_name,"
                        "peb_transaction_status,amount_tdr,peb_transaction_id"),
            "filter_transaction_status": status, "filter_transaction_type": "",
            "retry_view": True,
            "date_range": {"start_date": start, "end_date": end},
        }
        result = self._page.evaluate(
            """
            async (body) => {
                const r = await fetch(
                    '/merchant/api/v1/getMerchantTransactions/', {
                        method: 'POST', credentials: 'include',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(body),
                    });
                return {status: r.status, body: await r.text()};
            }
            """, body)
        if result["status"] == 401:
            raise AuthError()
        if result["status"] != 200:
            raise RuntimeError(f"Easebuzz API {result['status']}: {result['body'][:200]}")
        return json.loads(result["body"])


def _make_client():
    env = _load_env()
    email = env.get("EB_EMAIL_ELI") or env.get("EB_EMAIL")
    password = env.get("EB_PASSWORD_ELI") or env.get("EB_PASSWORD")
    return EasebuzzClient(email, password)


client = _make_client()
