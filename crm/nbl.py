"""NBL CRM client: login, search, profile parse, setCollection.
Reads only NBL_USERNAME / NBL_PASSWORD — never ELI_*."""
import os
import re
import requests
from html.parser import HTMLParser

BASE        = "https://app.nextbigloan.co.in/admin"
LOGIN_PAGE  = f"{BASE}/index"
LOGIN_POST  = f"{BASE}/login/doLogin"
SEARCH_URL  = f"{BASE}/disbursedDataSearch"
PROFILE_URL = f"{BASE}/profile/{{}}"
SETCOLL_URL = f"{BASE}/setCollection"
DASH_URL    = f"{BASE}/dashboard"

CLOSED_STATUSES = {
    "Closed", "Settled to Closed", "PAYDAY PRECLOSE", "EMI PRECLOSE",
    "Settlement", "Bad Debts", "Write Off", "Death Case",
}

# NBL search has 36 columns and a different order than ELI: no "Product Name",
# no "Enach Details", "Disbursed By Bank", "Disbursal Time", etc. Order verified
# from <thead> in /admin/disbursedDataSearch response.
HEADERS_ORDER = [
    "LeadID", "Loan No", "Branch", "Loan Type", "Name",
    "Credit By", "PD By", "Gender", "DOB", "Email", "Mobile", "Aadhar No",
    "Pancard", "Loan Amount", "Tenure", "ROI", "Repay Date", "Account No",
    "Account Type", "Bank IFSC", "Bank", "Bank Branch", "Cheque No",
    "Disbursal Refrence No", "Disbursal Date", "Admin Fee", "Monthly Income",
    "Cibil", "PlatForm Fee", "Convininece Fee", "CreditRisk Analisys Fee",
    "GST Fee", "UTM Source", "Status", "Leads coming date", "Fresh & Repeated",
]


class _RowExtractor(HTMLParser):
    """Pulls <td> text from each <tr> inside <tbody>."""
    def __init__(self):
        super().__init__()
        self.in_tbody = self.in_tr = self.in_td = False
        self.cur, self.rows = [], []

    def handle_starttag(self, tag, attrs):
        if tag == "tbody":
            self.in_tbody = True
        elif tag == "tr" and self.in_tbody:
            self.in_tr, self.cur = True, []
        elif tag == "td" and self.in_tr:
            self.in_td = True
            self.cur.append("")

    def handle_endtag(self, tag):
        if tag == "td" and self.in_td:
            self.in_td = False
        elif tag == "tr" and self.in_tr:
            self.in_tr = False
            if self.cur:
                self.rows.append([c.strip() for c in self.cur])
        elif tag == "tbody":
            self.in_tbody = False

    def handle_data(self, data):
        if self.in_td:
            self.cur[-1] += data


def login():
    user = os.getenv("NBL_USERNAME")
    pwd  = os.getenv("NBL_PASSWORD")
    if not user or not pwd:
        raise RuntimeError("NBL_USERNAME / NBL_PASSWORD missing from .env")
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest",
    })
    s.get(LOGIN_PAGE, timeout=15)
    # NBL uses 'employeeID' as the login field (ELI uses 'userName').
    s.post(LOGIN_POST, data={
        "employeeID": user,
        "password":   pwd,
    }, timeout=15)
    return s


def session_alive(s):
    try:
        r = s.get(DASH_URL, timeout=10, allow_redirects=False)
        return r.status_code == 200
    except Exception:
        return False


def search(s, query):
    """Search CRM by any value (loan no, mobile, name, etc.).
    The /disbursedDataSearch endpoint does a multi-field search on `searchData`."""
    r = s.post(SEARCH_URL, data={"searchData": query}, timeout=30)
    r.raise_for_status()
    p = _RowExtractor()
    p.feed(r.text)
    return [dict(zip(HEADERS_ORDER, row)) for row in p.rows]


def search_by_mobile(s, mobile):
    return search(s, mobile)


def get_latest_lead(s, mobile):
    rows = search_by_mobile(s, mobile)
    if not rows:
        return None
    rows.sort(key=lambda r: int(r.get("LeadID") or 0), reverse=True)
    return rows[0]


# Profile-page scalars: (key, regex, cast). Default values live in get_repay_info.
# Amount regexes require at least one digit so an empty "Rs." doesn't get
# parsed as ".". Disbursal date may have HH:MM:SS (ELI) or just DD-MM-YYYY (NBL).
_AMT = r"(\d+(?:\.\d+)?)"
_PROFILE_FIELDS = [
    ("repay_amount",   re.compile(rf"Repayment\s+Amount\s*:\s*Rs\.?\s*{_AMT}", re.I), float),
    ("paid_amount",    re.compile(rf"Paid\s+Amount\s*:\s*Rs\.?\s*{_AMT}",      re.I), float),
    ("loan_disbursed", re.compile(rf"Loan\s+Disbursed\s*:\s*Rs\.?\s*{_AMT}",   re.I), float),
    ("no_of_days",     re.compile(r"No\s+of\s+Days\s*:\s*(\d+)\s*Days", re.I), int),
    ("real_days",      re.compile(r"Real\s+Days\s*:\s*(\d+)\s*Days",   re.I), int),
    ("penalty_days",   re.compile(r"Penalty\s+[Dd]ays\s*:\s*(\d+)\s*Days", re.I), int),
    ("disbursal_date", re.compile(r"Disbursal\s+date\s*:\s*(\d{2}-\d{2}-\d{4}(?:\s+\d{2}:\d{2}:\d{2})?)", re.I), str),
]
_RE_FORM    = re.compile(r"<form[^>]*id=[\"']collectionAdd[\"'].*?</form>", re.S)
_RE_CONTACT = re.compile(r"name=[\"']contactID[\"'][^>]*value=[\"']([^\"']+)[\"']")
_RE_LOANNO  = re.compile(r"name=[\"']loanNo[\"'][^>]*value=[\"']([^\"']+)[\"']")


def get_repay_info(s, lead_id):
    """Parse the profile page. repay_amount is the dynamic outstanding
    (Loan Details > Repayment Amount); other fields are informational."""
    r = s.get(PROFILE_URL.format(lead_id), timeout=20)
    r.raise_for_status()
    html = r.text
    info = {
        "leadID": str(lead_id),
        "contactID": None,
        "loanNo": None,
        "paid_amount": 0.0,
        "repay_amount": None,
        "loan_disbursed": None,
        "no_of_days": None,
        "real_days": None,
        "penalty_days": None,
        "disbursal_date": None,
    }
    for key, rx, cast in _PROFILE_FIELDS:
        m = rx.search(html)
        if m and m.group(1):
            info[key] = cast(m.group(1))
    fm = _RE_FORM.search(html)
    if fm:
        body = fm.group(0)
        cm = _RE_CONTACT.search(body)
        if cm:
            info["contactID"] = cm.group(1)
        lm = _RE_LOANNO.search(body)
        if lm:
            info["loanNo"] = lm.group(1)
    return info


def set_collection(s, payload):
    r = s.post(SETCOLL_URL, data=payload, timeout=30)
    return r.status_code, r.text
