"""Accounts blueprint — Cashfree / Easebuzz transaction sheet viewer.
Mounted at /accounts/ by the main app. Reads creds from the project-root .env."""
import time
import threading
from datetime import date

from flask import Blueprint, render_template_string, request, jsonify

from cashfree import eli as cf_eli, nbl as cf_nbl, ldr as cf_ldr, cpy as cf_cpy
from cashfree.common import COLUMN_HEADERS as CF_HEADERS

bp = Blueprint("accounts", __name__, url_prefix="/accounts")


class TTLCache:
    """Repeat lookups in the same window return instantly without re-hitting upstream."""
    def __init__(self, ttl=300):
        self.ttl = ttl
        self._store = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            value, ts = item
            if time.time() - ts > self.ttl:
                self._store.pop(key, None)
                return None
            return value

    def set(self, key, value):
        with self._lock:
            self._store[key] = (value, time.time())


cf_cache = TTLCache(ttl=300)
eb_cache = TTLCache(ttl=300)

PRODUCTS = {
    "eli": {"name": "ELI", "services": ["easebuzz", "cashfree"]},
    "nbl": {"name": "NBL", "services": ["cashfree"]},
    "ldr": {"name": "LDR", "services": ["cashfree"]},
    "cpy": {"name": "CPY", "services": ["cashfree"]},
}

CASHFREE_FETCHERS = {
    "eli": cf_eli.fetch,
    "nbl": cf_nbl.fetch,
    "ldr": cf_ldr.fetch,
    "cpy": cf_cpy.fetch,
}

SERVICE_LABELS = {
    "easebuzz": ("Easebuzz", "View Easebuzz transactions"),
    "cashfree": ("Cashfree", "View Cashfree transactions"),
}


def _easebuzz_client():
    """Lazy-import — Playwright is heavy; skip the cost unless someone hits the route."""
    import easebuzz_eli
    return easebuzz_eli.client


BASE_CSS = """
    :root {
      --bg: #f7f8fb; --surface: #ffffff; --border: #e8eaf0; --border-strong: #d4d7e0;
      --text: #0f172a; --muted: #64748b; --primary: #4f46e5; --primary-hover: #4338ca;
      --success: #059669; --success-bg: #d1fae5;
      --failure: #dc2626; --failure-bg: #fee2e2;
      --warn: #d97706; --warn-bg: #fef3c7;
      --neutral: #64748b; --neutral-bg: #f1f5f9;
    }
    * { box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; margin: 0; background: var(--bg); color: var(--text); font-size: 14px; line-height: 1.5; }
    a { color: var(--primary); text-decoration: none; }
    a:hover { text-decoration: underline; }

    .topbar { background: var(--surface); border-bottom: 1px solid var(--border); padding: 14px 28px; display: flex; align-items: center; gap: 24px; }
    .topbar .brand { font-weight: 700; font-size: 16px; letter-spacing: -0.01em; color: var(--text); display: flex; align-items: center; gap: 10px; }
    .topbar .brand:hover { text-decoration: none; }
    .topbar .brand .dot { width: 10px; height: 10px; border-radius: 50%; background: linear-gradient(135deg, var(--primary), #8b5cf6); }
    .topbar nav { display: flex; gap: 16px; font-size: 14px; }
    .topbar nav a { color: var(--muted); }
    .topbar nav a.active { color: var(--text); font-weight: 600; }

    .container { max-width: 1400px; margin: 0 auto; padding: 32px 28px; }
    h1 { font-size: 24px; font-weight: 700; letter-spacing: -0.02em; margin: 0 0 4px 0; }
    .subtitle { color: var(--muted); margin-bottom: 24px; font-size: 14px; }

    .tile-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; margin-top: 8px; }
    .tile { display: block; padding: 22px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface); transition: all 0.15s; position: relative; }
    .tile:hover { border-color: var(--primary); box-shadow: 0 4px 12px rgba(79, 70, 229, 0.08); transform: translateY(-1px); text-decoration: none; }
    .tile .icon { width: 40px; height: 40px; border-radius: 10px; background: linear-gradient(135deg, #eef2ff, #e0e7ff); display: flex; align-items: center; justify-content: center; font-size: 18px; margin-bottom: 14px; color: var(--primary); font-weight: 700; }
    .tile h2 { margin: 0 0 4px 0; font-size: 17px; color: var(--text); font-weight: 600; }
    .tile p { margin: 0; font-size: 13px; color: var(--muted); }
    .tile .arrow { position: absolute; right: 22px; top: 22px; color: var(--muted); transition: transform 0.15s; }
    .tile:hover .arrow { color: var(--primary); transform: translateX(2px); }

    .card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 20px; }

    form.filters { display: flex; gap: 12px; align-items: end; flex-wrap: wrap; margin: 0; }
    label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 6px; font-weight: 500; }
    input[type=date], input[type=text], select { padding: 9px 12px; font-size: 14px; border: 1px solid var(--border-strong); border-radius: 8px; background: var(--surface); color: var(--text); transition: border-color 0.15s; }
    input:focus, select:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.12); }
    button { padding: 10px 20px; background: var(--primary); color: white; border: 0; cursor: pointer; font-size: 14px; border-radius: 8px; font-weight: 500; transition: background 0.15s; }
    button:hover { background: var(--primary-hover); }

    .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 20px; }
    .stat { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 18px; }
    .stat .label { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; font-weight: 500; }
    .stat .value { font-size: 22px; font-weight: 700; margin-top: 4px; letter-spacing: -0.01em; }
    .stat .value.amt { color: var(--text); }

    .err { background: var(--failure-bg); color: var(--failure); padding: 14px 16px; border-radius: 8px; margin-bottom: 20px; border: 1px solid #fecaca; font-size: 14px; }

    .table-wrap { overflow: auto; max-height: 70vh; background: var(--surface); border: 1px solid var(--border); border-radius: 12px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    thead th { background: var(--surface); border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 1; }
    th, td { padding: 11px 14px; text-align: left; white-space: nowrap; }
    th { font-weight: 600; color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }
    tbody tr { border-bottom: 1px solid var(--border); }
    tbody tr:last-child { border-bottom: 0; }
    tbody tr:hover { background: #fafbff; }

    .pill { display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }
    .pill::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
    .pill.success { color: var(--success); background: var(--success-bg); }
    .pill.failure, .pill.userCancelled { color: var(--failure); background: var(--failure-bg); }
    .pill.pending, .pill.initiated { color: var(--warn); background: var(--warn-bg); }
    .pill.dropped { color: var(--neutral); background: var(--neutral-bg); }

    .amt { text-align: right; font-variant-numeric: tabular-nums; font-weight: 500; }
    .empty { text-align: center; padding: 60px 20px; color: var(--muted); }

    .loading-bar { position: fixed; top: 0; left: 0; right: 0; height: 3px; background: var(--primary); transform: scaleX(0); transform-origin: left; transition: opacity 0.3s; z-index: 1000; opacity: 0; }
    .loading-bar.active { opacity: 1; animation: loading 1.4s ease-in-out infinite; }
    @keyframes loading { 0% { transform: scaleX(0); transform-origin: left; } 50% { transform: scaleX(0.6); transform-origin: left; } 100% { transform: scaleX(1); transform-origin: right; } }
    button:disabled { background: var(--muted); cursor: not-allowed; }
    .result-zone.stale { opacity: 0.55; transition: opacity 0.2s; pointer-events: none; }
"""

SHARED_SCRIPT = """
  const bar = document.getElementById('loading-bar');
  const showBar = () => bar && bar.classList.add('active');
  const hideBar = () => bar && bar.classList.remove('active');

  const form = document.querySelector('form.filters');
  if (form) {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = form.querySelector('button[type=submit]');
      const result = document.getElementById('result-zone');
      const params = new URLSearchParams(new FormData(form));
      const url = form.getAttribute('action') + '?' + params.toString();
      history.pushState({}, '', url);

      showBar();
      btn.disabled = true;
      if (result) result.classList.add('stale');

      try {
        const resp = await fetch(url, {credentials: 'same-origin'});
        const html = await resp.text();
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const newResult = doc.getElementById('result-zone');
        if (result && newResult) result.replaceWith(newResult);
      } catch (err) {
        console.error('Lookup failed:', err);
      } finally {
        hideBar();
        btn.disabled = false;
      }
    });
  }

  document.addEventListener('input', (e) => {
    if (e.target.id !== 'search') return;
    const tbody = document.querySelector('#data tbody');
    if (!tbody) return;
    const q = e.target.value.trim().toLowerCase();
    tbody.querySelectorAll('tr').forEach(r => {
      r.style.display = (!q || r.textContent.toLowerCase().includes(q)) ? '' : 'none';
    });
  });
  document.addEventListener('dblclick', (e) => {
    const td = e.target.closest('#data td');
    if (!td) return;
    navigator.clipboard.writeText(td.textContent.trim());
    const orig = td.style.background;
    td.style.background = 'var(--success-bg)';
    setTimeout(() => { td.style.background = orig; }, 300);
  });
"""

HOME = """
<!doctype html>
<html><head><title>Accounts</title><style>""" + BASE_CSS + """
  .tile.primary { border-left: 3px solid var(--primary); }
  .tile.primary .icon { background: linear-gradient(135deg, var(--primary), #8b5cf6); color: white; }
</style></head>
<body>
  <div class="container">
    <h1>Accounts</h1>
    <div class="subtitle">Reconciliation flow + payment gateway transactions</div>
    <div class="tile-grid">
      <a class="tile primary" href="/autocollection">
        <div class="icon">AC</div>
        <h2>Auto Collection</h2>
        <p>Reconciliation: ELI, NBL · Cashfree → CRM closure</p>
        <span class="arrow">→</span>
      </a>
      {% for slug, p in products.items() %}
        <a class="tile" href="/accounts/{{ slug }}">
          <div class="icon">{{ p.name[:1] }}</div>
          <h2>{{ p.name }}</h2>
          <p>{{ p.services | length }} {{ 'service' if p.services|length == 1 else 'services' }}: {{ p.services | join(', ') | title }}</p>
          <span class="arrow">→</span>
        </a>
      {% endfor %}
    </div>
  </div>
</body></html>
"""

PRODUCT_HOME = """
<!doctype html>
<html><head><title>{{ product.name }}</title><style>""" + BASE_CSS + """</style></head>
<body>
  <div class="container">
    <h1>{{ product.name }}</h1>
    <div class="subtitle">Select a payment gateway</div>
    <div class="tile-grid">
      {% for svc in product.services %}
        <a class="tile" href="/accounts/{{ slug }}/{{ svc }}">
          <div class="icon">{{ labels[svc][0][:1] }}</div>
          <h2>{{ labels[svc][0] }}</h2>
          <p>{{ labels[svc][1] }}</p>
          <span class="arrow">→</span>
        </a>
      {% endfor %}
    </div>
  </div>
</body></html>
"""

CASHFREE_PAGE = """
<!doctype html>
<html><head><title>{{ product.name }} · Cashfree</title><style>""" + BASE_CSS + """</style></head>
<body>
  <div class="loading-bar" id="loading-bar"></div>
  <div class="container">
    <h1>{{ product.name }} · Cashfree</h1>
    <div class="subtitle">Reconciliation export · double-click any cell to copy · <a href="/accounts/{{ slug }}">← {{ product.name }}</a></div>

    <div class="card">
      <form class="filters" method="get" action="/accounts/{{ slug }}/cashfree">
        <div><label>Start date</label><input type="date" name="start" value="{{ start }}" required></div>
        <div><label>End date</label><input type="date" name="end" value="{{ end }}" required></div>
        <div><label>Search</label><input type="text" id="search" placeholder="filter rows..." autocomplete="off"></div>
        <button type="submit">Lookup</button>
      </form>
    </div>

    <div id="result-zone" class="result-zone">
      {% if error %}<div class="err">{{ error }}</div>{% endif %}

      {% if rows is not none %}
        {% if rows %}
        <div class="table-wrap">
          <table id="data">
            <thead>
              <tr>{% for c in columns %}<th>{{ headers[c] }}</th>{% endfor %}</tr>
            </thead>
            <tbody>
              {% for r in rows %}
                <tr>
                  {% for c in columns %}
                    {% if c == 'event_status' %}
                      <td><span class="pill {{ r[c] | lower }}">{{ r[c] | lower }}</span></td>
                    {% elif c in ('event_amount', 'order_amount', 'event_settlement_amount') %}
                      <td class="amt">₹{{ '{:,.2f}'.format(r[c] or 0) }}</td>
                    {% else %}
                      <td>{{ r[c] }}</td>
                    {% endif %}
                  {% endfor %}
                </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
        {% else %}
          <div class="card empty">No transactions found for this date range.</div>
        {% endif %}
      {% endif %}
    </div>
  </div>

  <script>""" + SHARED_SCRIPT + """</script>
</body></html>
"""

EASEBUZZ_PAGE = """
<!doctype html>
<html><head><title>ELI · Easebuzz</title><style>""" + BASE_CSS + """</style></head>
<body>
  <div class="loading-bar" id="loading-bar"></div>
  <div class="container">
    <h1>ELI · Easebuzz</h1>
    <div class="subtitle">Transaction history · <a href="/accounts/eli">← ELI</a></div>

    <div class="card">
      <form class="filters" method="get" action="/accounts/eli/easebuzz">
        <div><label>Start date</label><input type="date" name="start" value="{{ start }}" required></div>
        <div><label>End date</label><input type="date" name="end" value="{{ end }}" required></div>
        <div>
          <label>Status</label>
          <select name="status">
            {% for v, lbl in [('','All'), ('success','Success'), ('failure','Failure'),
                              ('initiated','Initiated'), ('dropped','Dropped'),
                              ('userCancelled','User Cancelled')] %}
              <option value="{{ v }}" {% if status == v %}selected{% endif %}>{{ lbl }}</option>
            {% endfor %}
          </select>
        </div>
        <button type="submit">Lookup</button>
      </form>
    </div>

    <div id="result-zone" class="result-zone">
      {% if error %}<div class="err">{{ error }}</div>{% endif %}

      {% if rows is not none %}
        {% if rows %}
        <div class="table-wrap">
          <table id="data">
            <thead><tr><th>Date</th><th>Txn ID</th><th>Customer</th><th>Phone</th><th>Status</th><th class="amt">Amount</th></tr></thead>
            <tbody>
              {% for r in rows %}
                <tr>
                  <td>{{ r.txn_date }}</td>
                  <td>{{ r.peb_transaction_id }}</td>
                  <td>{{ r.customer_name }}</td>
                  <td>{{ r.customer_phone }}</td>
                  <td><span class="pill {{ r.peb_transaction_status }}">{{ r.peb_transaction_status }}</span></td>
                  <td class="amt">₹{{ '{:,.2f}'.format(r.amount_tdr) }}</td>
                </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
        {% else %}
          <div class="card empty">No transactions found for this date range.</div>
        {% endif %}
      {% endif %}
    </div>
  </div>

  <script>""" + SHARED_SCRIPT + """</script>
</body></html>
"""


@bp.route("/")
def home():
    return render_template_string(HOME, products=PRODUCTS)


@bp.route("/<slug>")
def product_home(slug):
    if slug not in PRODUCTS:
        return f"Unknown product: {slug}", 404
    return render_template_string(PRODUCT_HOME,
                                  slug=slug,
                                  product=PRODUCTS[slug],
                                  labels=SERVICE_LABELS)


@bp.route("/<slug>/cashfree")
def product_cashfree(slug):
    if slug not in PRODUCTS or "cashfree" not in PRODUCTS[slug]["services"]:
        return f"{slug} does not have Cashfree", 404

    today = date.today().isoformat()
    start = request.args.get("start", today)
    end = request.args.get("end", today)
    bypass = request.args.get("refresh") == "1"

    columns, rows, error = [], None, None
    if request.args.get("start") and request.args.get("end"):
        cache_key = (slug, start, end)
        cached = None if bypass else cf_cache.get(cache_key)
        if cached is not None:
            columns, rows = cached
            print(f"[cache] cashfree HIT {cache_key} ({len(rows)} rows)")
        else:
            try:
                columns, rows = CASHFREE_FETCHERS[slug](start, end)
                cf_cache.set(cache_key, (columns, rows))
            except Exception as e:
                error = str(e)
                columns = []

    return render_template_string(CASHFREE_PAGE,
                                  slug=slug,
                                  product=PRODUCTS[slug],
                                  start=start, end=end,
                                  columns=columns, rows=rows,
                                  headers=CF_HEADERS, error=error)


@bp.route("/eli/easebuzz")
def eli_easebuzz_view():
    today = date.today().isoformat()
    start = request.args.get("start", today)
    end = request.args.get("end", today)
    status = request.args.get("status", "")
    bypass = request.args.get("refresh") == "1"

    rows, error = None, None
    if request.args.get("start") and request.args.get("end"):
        cache_key = ("eli", start, end, status)
        cached = None if bypass else eb_cache.get(cache_key)
        if cached is not None:
            rows = cached
            print(f"[cache] easebuzz HIT {cache_key} ({len(rows)} rows)")
        else:
            try:
                rows = _easebuzz_client().fetch_transactions(start, end, status=status)
                eb_cache.set(cache_key, rows)
            except Exception as e:
                error = str(e)

    return render_template_string(EASEBUZZ_PAGE, start=start, end=end,
                                  status=status, rows=rows, error=error)


@bp.route("/api/easebuzz/eli")
def api_easebuzz_eli():
    start = request.args.get("start")
    end = request.args.get("end")
    status = request.args.get("status", "")
    if not start or not end:
        return jsonify({"error": "start and end required"}), 400
    try:
        rows = _easebuzz_client().fetch_transactions(start, end, status=status)
        return jsonify({"count": len(rows), "rows": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/cashfree/<slug>")
def api_cashfree(slug):
    if slug not in CASHFREE_FETCHERS:
        return jsonify({"error": f"unknown product: {slug}"}), 404
    start = request.args.get("start")
    end = request.args.get("end")
    if not start or not end:
        return jsonify({"error": "start and end required"}), 400
    try:
        columns, rows = CASHFREE_FETCHERS[slug](start, end)
        return jsonify({"count": len(rows), "columns": columns, "rows": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
