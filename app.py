from fasthtml.common import *
from starlette.responses import RedirectResponse
import snowflake.connector
from datetime import date
import os, json

# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------
print("Connecting to Snowflake (OAuth browser auth may be required)...")
_CACHED_DATA = None
_CUSTOMER_DETAIL = {}  # keyed by account_number (ENTITYID)

def _get_connection():
    private_key_path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH")
    if private_key_path:
        from cryptography.hazmat.primitives import serialization
        with open(private_key_path, "rb") as f:
            p_key = serialization.load_pem_private_key(f.read(), password=None)
        pkb = p_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return snowflake.connector.connect(
            account=os.environ.get("SNOWFLAKE_ACCOUNT", "PP13258-ITERABLE"),
            user=os.environ.get("SNOWFLAKE_USER", "shawn.skillman@iterable.com"),
            private_key=pkb,
            warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "BILLING_PIPE"),
        )
    return snowflake.connector.connect(connection_name='pp13258-iterable')

def _query_snowflake(conn=None):
    own_conn = conn is None
    if own_conn: conn = _get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            c.ENTITYID       AS account_number,
            c.COMPANYNAME    AS customer_name,
            t.TRANID         AS invoice_number,
            t.TRANDATE::DATE AS invoice_date,
            t.FOREIGNTOTAL   AS invoice_amount,
            t.DUEDATE::DATE  AS due_date,
            t.FOREIGNAMOUNTUNPAID AS invoice_balance,
            DATEDIFF('day', t.DUEDATE, CURRENT_DATE()) AS days_past_due,
            CASE
                WHEN DATEDIFF('day', t.DUEDATE, CURRENT_DATE()) BETWEEN 31  AND 60  THEN '31-60'
                WHEN DATEDIFF('day', t.DUEDATE, CURRENT_DATE()) BETWEEN 61  AND 90  THEN '61-90'
                WHEN DATEDIFF('day', t.DUEDATE, CURRENT_DATE()) BETWEEN 91  AND 120 THEN '91-120'
                WHEN DATEDIFF('day', t.DUEDATE, CURRENT_DATE()) > 120               THEN '120+'
            END AS aging_bucket
        FROM FIVETRAN_DB.NETSUITE_SUITE.TRANSACTION t
        JOIN FIVETRAN_DB.NETSUITE_SUITE.CUSTOMER c ON t.ENTITY = c.ID
        WHERE t.TYPE = 'CustInvc'
          AND t._FIVETRAN_DELETED = false
          AND t.FOREIGNAMOUNTUNPAID > 0
          AND DATEDIFF('day', t.DUEDATE, CURRENT_DATE()) >= 31
        ORDER BY c.ENTITYID::INT, t.TRANDATE
    """)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    if own_conn: conn.close()
    return [dict(zip(cols, r)) for r in rows]

def _query_customer_details(conn=None):
    """Fetch addresses, org ID, SFDC account ID, and contacts for all AR customers."""
    own_conn = conn is None
    if own_conn: conn = _get_connection()
    cur = conn.cursor()
    # Get customer metadata + addresses
    cur.execute("""
        SELECT
            c.ENTITYID AS account_number,
            c.COMPANYNAME AS customer_name,
            c.CUSTENTITY_ITR_ORG_ID AS org_id,
            sa.ID AS sfdc_account_id,
            -- Bill-to
            ba.ATTENTION AS bill_attention, ba.ADDRESSEE AS bill_addressee,
            ba.ADDR1 AS bill_addr1, ba.ADDR2 AS bill_addr2,
            ba.CITY AS bill_city, ba.STATE AS bill_state, ba.ZIP AS bill_zip, ba.COUNTRY AS bill_country,
            -- Ship-to
            sha.ATTENTION AS ship_attention, sha.ADDRESSEE AS ship_addressee,
            sha.ADDR1 AS ship_addr1, sha.ADDR2 AS ship_addr2,
            sha.CITY AS ship_city, sha.STATE AS ship_state, sha.ZIP AS ship_zip, sha.COUNTRY AS ship_country
        FROM FIVETRAN_DB.NETSUITE_SUITE.CUSTOMER c
        LEFT JOIN FIVETRAN_DB.NETSUITE_SUITE.CUSTOMERADDRESSBOOKENTITYADDRESS ba
            ON c.DEFAULTBILLINGADDRESS = ba.NKEY AND ba._FIVETRAN_DELETED = false
        LEFT JOIN FIVETRAN_DB.NETSUITE_SUITE.CUSTOMERADDRESSBOOKENTITYADDRESS sha
            ON c.DEFAULTSHIPPINGADDRESS = sha.NKEY AND sha._FIVETRAN_DELETED = false
        LEFT JOIN FIVETRAN_DB.FT_SALESFORCE.ACCOUNT sa
            ON TRIM(UPPER(c.COMPANYNAME)) = TRIM(UPPER(sa.NAME)) AND sa.IS_DELETED = false
        WHERE c._FIVETRAN_DELETED = false
          AND c.ENTITYID IN (
              SELECT DISTINCT c2.ENTITYID
              FROM FIVETRAN_DB.NETSUITE_SUITE.TRANSACTION t
              JOIN FIVETRAN_DB.NETSUITE_SUITE.CUSTOMER c2 ON t.ENTITY = c2.ID
              WHERE t.TYPE = 'CustInvc' AND t._FIVETRAN_DELETED = false
                AND t.FOREIGNAMOUNTUNPAID > 0
                AND DATEDIFF('day', t.DUEDATE, CURRENT_DATE()) >= 31
          )
    """)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    details = {}
    sfdc_ids = set()
    acct_sfdc_ids = {}  # account_number -> set of all matching SFDC IDs
    for r in rows:
        d = dict(zip(cols, r))
        acct = d['ACCOUNT_NUMBER']
        if acct not in details:
            details[acct] = d
            details[acct]['contacts'] = []
            acct_sfdc_ids[acct] = set()
        sid = d.get('SFDC_ACCOUNT_ID')
        if sid:
            sfdc_ids.add(sid)
            acct_sfdc_ids[acct].add(sid)
            # Keep first non-None SFDC ID for display
            if not details[acct].get('SFDC_ACCOUNT_ID'):
                details[acct]['SFDC_ACCOUNT_ID'] = sid

    # Fetch contacts for all matched SFDC accounts
    if sfdc_ids:
        id_list = ",".join([f"'{sid}'" for sid in sfdc_ids])
        cur.execute(f"""
            SELECT ACCOUNT_ID, NAME, TITLE, EMAIL, PHONE
            FROM FIVETRAN_DB.FT_SALESFORCE.CONTACT
            WHERE ACCOUNT_ID IN ({id_list})
              AND IS_DELETED = false
            ORDER BY NAME
        """)
        contact_rows = cur.fetchall()
        # Map contacts to account_numbers (using ALL matching SFDC IDs per customer)
        sfdc_to_acct = {}
        for acct, sids in acct_sfdc_ids.items():
            for sid in sids:
                sfdc_to_acct.setdefault(sid, []).append(acct)
        for cr in contact_rows:
            sid = cr[0]
            for acct in sfdc_to_acct.get(sid, []):
                contact = {
                    'name': cr[1] or '', 'title': cr[2] or '',
                    'email': cr[3] or '', 'phone': cr[4] or ''
                }
                # Deduplicate by email (or name if no email)
                key = contact['email'] or contact['name']
                existing_keys = {(c['email'] or c['name']) for c in details[acct]['contacts']}
                if key not in existing_keys:
                    details[acct]['contacts'].append(contact)

    if own_conn: conn.close()
    return details

_startup_conn = _get_connection()
_CACHED_DATA = _query_snowflake(_startup_conn)
_startup_conn.close()
print(f"Loaded {len(_CACHED_DATA)} invoices from NetSuite.")

# Customer detail is lazy-loaded on first drawer open to avoid Cloud Run startup timeout
_CUSTOMER_DETAIL = None

def _ensure_customer_detail():
    global _CUSTOMER_DETAIL
    if _CUSTOMER_DETAIL is None:
        print("Loading customer detail (first drawer open)...")
        _CUSTOMER_DETAIL = _query_customer_details()
        print(f"Loaded detail for {len(_CUSTOMER_DETAIL)} customers.")

# Load notes from CSV export
_NOTES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'notes_data.json')
_NOTES = {}
if os.path.exists(_NOTES_PATH):
    with open(_NOTES_PATH) as f:
        _NOTES = json.load(f)
    print(f"Loaded notes for {len(_NOTES)} invoices.")

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------
CSS = """
:root {
    --bg: #0f1117; --surface: #1a1d27; --surface2: #242836;
    --border: #2e3348; --text: #e4e6f0; --text-muted: #8b8fa3;
    --accent: #6c8cff; --green: #3dd68c; --yellow: #f5c542;
    --orange: #f59e42; --red: #f54242;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--bg) !important; color: var(--text) !important; line-height: 1.5;
}
.container { max-width: 1600px; margin: 0 auto; padding: 24px; }
.app-header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 16px 24px; background: var(--surface); border-bottom: 1px solid var(--border);
}
.app-header h1 { font-size: 20px; font-weight: 600; color: var(--text); }
.app-header .subtitle { color: var(--text-muted); font-size: 13px; margin-top: 2px; }
.header-right { text-align: right; }
.header-count { color: var(--text-muted); font-size: 13px; margin-right: 16px; }
.header-total { font-size: 20px; font-weight: 700; color: var(--accent); }
.refresh-btn {
    display: inline-block; margin-left: 16px; padding: 6px 14px;
    background: var(--surface2); color: var(--accent); border: 1px solid var(--border);
    border-radius: 8px; text-decoration: none; font-size: 13px; font-weight: 500;
}
.refresh-btn:hover { background: var(--border); }

.metrics-row { display: grid; grid-template-columns: repeat(5, 1fr); gap: 16px; margin: 24px 0; }
.metric-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px; cursor: pointer; transition: border-color 0.2s, box-shadow 0.2s;
}
.metric-card:hover { border-color: var(--accent); }
.metric-card.active { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.metric-card .label {
    font-size: 11px; color: var(--text-muted); text-transform: uppercase;
    letter-spacing: 0.5px; margin-bottom: 4px;
}
.metric-card .value { font-size: 26px; font-weight: 700; margin: 4px 0; }
.metric-card .sub { font-size: 13px; color: var(--text-muted); }
.c-total .value { color: var(--accent); }
.c-31 .value { color: var(--yellow); }
.c-61 .value { color: var(--orange); }
.c-91 .value { color: var(--red); }
.c-120 .value { color: #ff6b6b; }

.panels { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }
.panel {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px;
}
.panel h3 { font-size: 14px; margin-bottom: 16px; color: var(--text-muted); }

.bar-chart { display: flex; gap: 16px; align-items: flex-end; height: 140px; padding: 0 12px; }
.bar-col {
    display: flex; flex-direction: column; align-items: center; flex: 1; gap: 4px;
    cursor: pointer; opacity: 1; transition: opacity 0.2s;
}
.bar-col.dimmed { opacity: 0.3; }
.bar-col:hover { opacity: 1 !important; }
.bar-val { font-size: 12px; font-weight: 600; color: var(--text); }
.bar { width: 100%; border-radius: 6px 6px 0 0; min-height: 4px; transition: opacity 0.2s; }
.bar-lbl { font-size: 11px; color: var(--text-muted); margin-top: 4px; }

.top-list { list-style: none; }
.top-list li {
    display: flex; justify-content: space-between; align-items: center;
    padding: 7px 0; border-bottom: 1px solid var(--border); font-size: 13px;
}
.top-list li:last-child { border-bottom: none; }
.top-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 240px; }
.top-amt { font-weight: 600; font-variant-numeric: tabular-nums; white-space: nowrap; }

.filter-bar { display: flex; gap: 12px; align-items: center; margin-bottom: 16px; flex-wrap: wrap; }
.filter-bar input, .filter-bar select {
    background: var(--surface2); border: 1px solid var(--border);
    color: var(--text); padding: 8px 14px; border-radius: 8px;
    font-size: 14px; outline: none;
}
.filter-bar input:focus, .filter-bar select:focus { border-color: var(--accent); }
.filter-bar input { min-width: 280px; }

.badge {
    display: inline-block; padding: 3px 10px; border-radius: 20px;
    font-size: 11px; font-weight: 600; white-space: nowrap;
}
.b-31  { background: rgba(245,197,66,0.15); color: var(--yellow); }
.b-61  { background: rgba(245,158,66,0.15); color: var(--orange); }
.b-91  { background: rgba(245,66,66,0.15);  color: var(--red); }
.b-120 { background: rgba(255,107,107,0.15); color: #ff6b6b; }

.table-wrap {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; overflow: auto; max-height: 70vh;
}
table { width: 100%; border-collapse: collapse; font-size: 13px; }
thead { position: sticky; top: 0; z-index: 10; }
th {
    background: var(--surface2) !important; padding: 10px 12px; text-align: left;
    font-weight: 600; font-size: 11px; text-transform: uppercase;
    letter-spacing: 0.5px; color: var(--text-muted) !important;
    border-bottom: 2px solid var(--border); cursor: pointer; user-select: none;
    white-space: nowrap;
}
th.r { text-align: right; }
th:hover { color: var(--text) !important; }
th .arrow { font-size: 10px; margin-left: 4px; opacity: 0.4; }
th.sorted .arrow { opacity: 1; color: var(--accent); }
tr { border-bottom: 1px solid var(--border); }
tbody tr:hover { background: var(--surface2); }
tbody tr.selected { background: rgba(108,140,255,0.12); border-left: 3px solid var(--accent); }
td { padding: 10px 12px; white-space: nowrap; color: var(--text); }
td.r { text-align: right; font-variant-numeric: tabular-nums; }
td.cust { max-width: 240px; overflow: hidden; text-overflow: ellipsis; cursor: pointer; }
tfoot td {
    padding: 10px 12px; font-weight: 700; color: var(--accent);
    border-top: 2px solid var(--border); background: var(--surface2);
    position: sticky; bottom: 0; z-index: 5;
}
tfoot td.r { text-align: right; font-variant-numeric: tabular-nums; }
td.notes-cell { text-align: center; cursor: pointer; width: 40px; }
.notes-icon { font-size: 16px; opacity: 0.85; transition: opacity 0.2s; }
.notes-icon:hover { opacity: 1; }
.notes-icon.has-notes { opacity: 1; }

/* Notes modal */
.notes-overlay {
    display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    background: rgba(0,0,0,0.6); z-index: 1000; justify-content: center; align-items: center;
}
.notes-overlay.open { display: flex; }
.notes-modal {
    background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
    padding: 24px; width: 560px; max-width: 90vw; max-height: 80vh;
    display: flex; flex-direction: column;
}
.notes-modal h4 { font-size: 15px; margin-bottom: 12px; color: var(--text); }
.notes-modal .inv-label { font-size: 12px; color: var(--text-muted); margin-bottom: 12px; }
.notes-body {
    flex: 1; overflow-y: auto; white-space: pre-wrap; font-size: 13px;
    line-height: 1.6; color: var(--text); padding: 12px;
    background: var(--surface2); border-radius: 8px; margin-bottom: 12px;
    max-height: 400px;
}
.notes-textarea {
    width: 100%; min-height: 100px; background: var(--surface2); border: 1px solid var(--border);
    color: var(--text); border-radius: 8px; padding: 12px; font-size: 13px;
    font-family: inherit; resize: vertical; outline: none; margin-bottom: 12px;
}
.notes-textarea:focus { border-color: var(--accent); }
.notes-actions { display: flex; gap: 8px; justify-content: flex-end; }
.notes-btn {
    padding: 8px 16px; border-radius: 8px; border: 1px solid var(--border);
    background: var(--surface2); color: var(--text); font-size: 13px; cursor: pointer;
}
.notes-btn:hover { background: var(--border); }
.notes-btn.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
.notes-btn.primary:hover { opacity: 0.9; }

/* Customer detail drawer */
.drawer-overlay {
    display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    background: rgba(0,0,0,0.5); z-index: 900;
}
.drawer-overlay.open { display: block; }
.drawer {
    position: fixed; top: 0; right: -520px; width: 500px; height: 100%;
    background: var(--surface); border-left: 1px solid var(--border);
    z-index: 901; overflow-y: auto; transition: right 0.3s ease;
    box-shadow: -4px 0 24px rgba(0,0,0,0.4);
}
.drawer.open { right: 0; }
.drawer-header {
    display: flex; justify-content: space-between; align-items: flex-start;
    padding: 20px 24px; border-bottom: 1px solid var(--border);
    position: sticky; top: 0; background: var(--surface); z-index: 5;
}
.drawer-header h3 { font-size: 16px; font-weight: 600; color: var(--text); margin-bottom: 4px; }
.drawer-header .drawer-sub { font-size: 12px; color: var(--text-muted); }
.drawer-close {
    background: none; border: none; color: var(--text-muted); font-size: 20px;
    cursor: pointer; padding: 4px 8px; line-height: 1;
}
.drawer-close:hover { color: var(--text); }
.drawer-body { padding: 0; }
.drawer-summary {
    display: flex; gap: 24px; padding: 16px 24px; border-bottom: 1px solid var(--border);
    align-items: baseline;
}
.drawer-bal { font-size: 22px; font-weight: 700; color: var(--accent); }
.drawer-inv-amt { font-size: 13px; color: var(--text-muted); margin-top: 2px; }
.drawer-meta {
    display: grid; grid-template-columns: 1fr 1fr; gap: 8px;
    padding: 16px 24px; border-bottom: 1px solid var(--border);
}
.drawer-meta-item .dm-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); margin-bottom: 2px; }
.drawer-meta-item .dm-value { font-size: 13px; color: var(--text); font-weight: 500; }

/* Accordion */
.accordion-item { border-bottom: 1px solid var(--border); }
.accordion-header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 14px 24px; cursor: pointer; user-select: none;
    font-size: 13px; font-weight: 600; color: var(--text);
    transition: background 0.15s;
}
.accordion-header:hover { background: var(--surface2); }
.accordion-header .acc-icon { font-size: 12px; color: var(--text-muted); transition: transform 0.2s; }
.accordion-item.open .accordion-header .acc-icon { transform: rotate(180deg); }
.accordion-header .acc-badge { font-size: 11px; color: var(--text-muted); font-weight: 400; margin-left: 8px; }
.accordion-body {
    max-height: 0; overflow: hidden; transition: max-height 0.3s ease;
    background: var(--bg);
}
.accordion-item.open .accordion-body { max-height: 2000px; }
.accordion-content { padding: 16px 24px; }

/* Address cards */
.addr-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.addr-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 14px;
}
.addr-card .addr-type {
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px;
    color: var(--accent); font-weight: 600; margin-bottom: 8px;
}
.addr-card .addr-line { font-size: 13px; color: var(--text); line-height: 1.5; }
.addr-card .addr-attn { font-size: 12px; color: var(--text-muted); margin-top: 6px; }

/* Contact list */
.contact-row {
    display: grid; grid-template-columns: 1.2fr 1.2fr 1.5fr 1fr;
    gap: 8px; padding: 10px 0; border-bottom: 1px solid var(--border);
    font-size: 12px; align-items: center;
}
.contact-row:last-child { border-bottom: none; }
.contact-row.contact-header { font-weight: 600; color: var(--text-muted); text-transform: uppercase; font-size: 10px; letter-spacing: 0.5px; }
.contact-name { color: var(--text); font-weight: 500; }
.contact-title { color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.contact-email { color: var(--accent); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.contact-email a { color: var(--accent); text-decoration: none; }
.contact-email a:hover { text-decoration: underline; }
.contact-phone { color: var(--text-muted); white-space: nowrap; }
.no-data { color: var(--text-muted); font-size: 13px; font-style: italic; padding: 8px 0; }
"""

# ---------------------------------------------------------------------------
# JS -- all interactive behavior
# ---------------------------------------------------------------------------
JS = """
var activeBucket = null;
var sortCol = -1;
var sortAsc = true;

function fm(n) {
    return '$' + n.toFixed(2).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ',');
}

function getVisibleRows() {
    return Array.from(document.querySelectorAll('#ar-table tbody tr')).filter(
        function(r) { return r.style.display !== 'none'; }
    );
}

function updateTotals() {
    var rows = getVisibleRows();
    var totalAmt = 0, totalBal = 0, count = 0;
    rows.forEach(function(r) {
        var cells = r.querySelectorAll('td');
        totalAmt += parseFloat(cells[4].getAttribute('data-val') || 0);
        totalBal += parseFloat(cells[6].getAttribute('data-val') || 0);
        count++;
    });
    var foot = document.getElementById('tfoot-total');
    if (foot) {
        foot.querySelector('.tf-count').textContent = count + ' invoices';
        foot.querySelector('.tf-amt').textContent = fm(totalAmt);
        foot.querySelector('.tf-bal').textContent = fm(totalBal);
    }
    var hc = document.getElementById('header-count');
    var ht = document.getElementById('header-total');
    if (hc) hc.textContent = count + ' invoices';
    if (ht) ht.textContent = fm(totalBal);
}

function updateTop10() {
    var rows = getVisibleRows();
    var custs = {};
    rows.forEach(function(r) {
        var cells = r.querySelectorAll('td');
        var name = cells[1].textContent;
        var bal = parseFloat(cells[6].getAttribute('data-val') || 0);
        custs[name] = (custs[name] || 0) + bal;
    });
    var sorted = Object.entries(custs).sort(function(a, b) { return b[1] - a[1]; }).slice(0, 10);
    var ul = document.getElementById('top10-list');
    ul.innerHTML = '';
    sorted.forEach(function(pair) {
        var li = document.createElement('li');
        li.innerHTML = '<span class="top-name">' + pair[0] + '</span><span class="top-amt">' + fm(pair[1]) + '</span>';
        ul.appendChild(li);
    });
}

function applyFilters() {
    var s = document.getElementById('search').value.toLowerCase();
    var b = document.getElementById('bucket-filter').value;
    var bucket = activeBucket || b;
    document.querySelectorAll('#ar-table tbody tr').forEach(function(row) {
        var text = row.textContent.toLowerCase();
        var rb = row.getAttribute('data-bucket');
        var ms = !s || text.indexOf(s) >= 0;
        var mb = !bucket || rb === bucket;
        row.style.display = (ms && mb) ? '' : 'none';
    });
    updateTotals();
    updateTop10();
}

function filterByBucket(bucket) {
    if (activeBucket === bucket) {
        activeBucket = null;
    } else {
        activeBucket = bucket;
    }
    document.querySelectorAll('.metric-card').forEach(function(c) {
        var cb = c.getAttribute('data-bucket');
        if (activeBucket && cb === activeBucket) {
            c.classList.add('active');
        } else {
            c.classList.remove('active');
        }
    });
    document.querySelectorAll('.bar-col').forEach(function(col) {
        var bb = col.getAttribute('data-bucket');
        if (activeBucket && bb !== activeBucket) {
            col.classList.add('dimmed');
        } else {
            col.classList.remove('dimmed');
        }
    });
    document.getElementById('bucket-filter').value = activeBucket || '';
    applyFilters();
}

function sortTable(colIdx) {
    if (sortCol === colIdx) {
        sortAsc = !sortAsc;
    } else {
        sortCol = colIdx;
        sortAsc = true;
    }
    document.querySelectorAll('#ar-table thead th').forEach(function(th, i) {
        var arrow = th.querySelector('.arrow');
        if (!arrow) return;
        th.classList.remove('sorted');
        arrow.textContent = '\\u2195';
        if (i === colIdx) {
            th.classList.add('sorted');
            arrow.textContent = sortAsc ? '\\u2191' : '\\u2193';
        }
    });
    var tbody = document.querySelector('#ar-table tbody');
    var rows = Array.from(tbody.querySelectorAll('tr'));
    rows.sort(function(a, b) {
        var ca = a.querySelectorAll('td')[colIdx];
        var cb = b.querySelectorAll('td')[colIdx];
        var va = ca.getAttribute('data-val');
        var vb = cb.getAttribute('data-val');
        if (va !== null && vb !== null) {
            var na = Number(va), nb = Number(vb);
            if (!isNaN(na) && !isNaN(nb)) { va = na; vb = nb; }
        } else {
            va = ca.textContent.toLowerCase();
            vb = cb.textContent.toLowerCase();
        }
        if (va < vb) return sortAsc ? -1 : 1;
        if (va > vb) return sortAsc ? 1 : -1;
        return 0;
    });
    rows.forEach(function(r) { tbody.appendChild(r); });
}

// Notes
var notesData = {};
var userNotes = {};

function initNotes(data) { notesData = data; }

function getNotes(inv) {
    return userNotes[inv] || notesData[inv] || '';
}

function openNotes(inv, custName) {
    var overlay = document.getElementById('notes-overlay');
    var existing = getNotes(inv);
    document.getElementById('notes-inv-label').textContent = inv + ' - ' + custName;
    var body = document.getElementById('notes-body');
    var ta = document.getElementById('notes-textarea');
    if (existing) {
        body.textContent = existing;
        body.style.display = '';
        ta.value = '';
        ta.placeholder = 'Add additional notes...';
    } else {
        body.style.display = 'none';
        ta.value = '';
        ta.placeholder = 'Add notes for this invoice...';
    }
    overlay.setAttribute('data-inv', inv);
    overlay.classList.add('open');
}

function closeNotes() {
    document.getElementById('notes-overlay').classList.remove('open');
}

function saveNotes() {
    var overlay = document.getElementById('notes-overlay');
    var inv = overlay.getAttribute('data-inv');
    var ta = document.getElementById('notes-textarea');
    var newNote = ta.value.trim();
    if (!newNote) { closeNotes(); return; }
    var existing = getNotes(inv);
    var ts = new Date().toLocaleDateString('en-US', {month:'numeric',day:'numeric',year:'2-digit'});
    var full = ts + ' - ' + newNote;
    if (existing) {
        userNotes[inv] = full + '\\n---\\n' + existing;
    } else {
        userNotes[inv] = full;
    }
    var icon = document.querySelector('tr[data-inv="' + inv + '"] .notes-icon');
    if (icon) { icon.innerHTML = '&#128221;'; icon.classList.add('has-notes'); }
    closeNotes();
}

// Customer detail drawer
function openDrawer(acctNum, custName) {
    // Highlight the row
    document.querySelectorAll('#ar-table tbody tr.selected').forEach(function(r) { r.classList.remove('selected'); });
    document.querySelectorAll('#ar-table tbody tr').forEach(function(r) {
        if (r.querySelector('td') && r.querySelector('td').textContent.trim() === acctNum) {
            r.classList.add('selected');
        }
    });
    // Update drawer header with customer name
    var dh = document.getElementById('drawer-cust-name');
    if (dh) dh.textContent = custName || '';
    var da = document.getElementById('drawer-acct-num');
    if (da) da.textContent = 'Acct #' + acctNum;
    // Fetch drawer content via HTMX-like fetch
    var drawer = document.getElementById('customer-drawer');
    var overlay = document.getElementById('drawer-overlay');
    var body = document.getElementById('drawer-body');
    body.innerHTML = '<div style="padding:24px;color:var(--text-muted);">Loading...</div>';
    drawer.classList.add('open');
    overlay.classList.add('open');
    fetch('/customer-detail/' + encodeURIComponent(acctNum))
        .then(function(r) { return r.text(); })
        .then(function(html) { body.innerHTML = html; });
}

function closeDrawer() {
    document.getElementById('customer-drawer').classList.remove('open');
    document.getElementById('drawer-overlay').classList.remove('open');
    document.querySelectorAll('#ar-table tbody tr.selected').forEach(function(r) { r.classList.remove('selected'); });
}

function toggleAccordion(el) {
    var item = el.parentElement;
    item.classList.toggle('open');
}

document.addEventListener('DOMContentLoaded', function() {
    updateTotals();
});
"""

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app, rt = fast_app(
    hdrs=[Style(CSS), Script(JS)],
    pico=False,
    live=True,
)


def fm(val):
    if val is None: return "$0.00"
    return f"${val:,.2f}"

def badge(bucket):
    c = {'31-60':'b-31','61-90':'b-61','91-120':'b-91','120+':'b-120'}.get(bucket,'')
    l = {'31-60':'31-60 days','61-90':'61-90 days','91-120':'91-120 days','120+':'120+ days'}.get(bucket, bucket or '')
    return Span(l, cls=f"badge {c}")


def _esc(s):
    """Escape a string for safe embedding in JS/HTML onclick handlers."""
    if not s: return ''
    return str(s).replace("\\", "\\\\").replace("'", "\\'").replace('"', '&quot;')


@rt("/customer-detail/{acct_num}")
def get_customer_detail(acct_num: str):
    _ensure_customer_detail()
    d = _CUSTOMER_DETAIL.get(acct_num, {})
    if not d:
        return Div(Div("No detail available for this account.", cls="no-data", style="padding:24px;"))

    # Compute AR totals for this customer from cached invoice data
    cust_invoices = [r for r in (_CACHED_DATA or []) if str(r['ACCOUNT_NUMBER']) == acct_num]
    cust_balance = sum(r['INVOICE_BALANCE'] for r in cust_invoices)
    cust_inv_amount = sum(r['INVOICE_AMOUNT'] for r in cust_invoices)
    cust_inv_count = len(cust_invoices)

    # Build summary section (balance + invoice amount)
    summary = Div(
        Div(
            Div(fm(cust_balance), cls="drawer-bal"),
            Div(f"Outstanding balance ({cust_inv_count} invoices)", cls="drawer-inv-amt"),
            cls="drawer-summary-col"
        ),
        Div(
            Div(fm(cust_inv_amount), cls="drawer-bal", style="color: var(--text);"),
            Div("Original invoice total", cls="drawer-inv-amt"),
            cls="drawer-summary-col"
        ),
        cls="drawer-summary"
    )

    # Build meta section
    meta_items = []
    org_id = d.get('ORG_ID') or ''
    sfdc_id = d.get('SFDC_ACCOUNT_ID') or ''
    if org_id:
        meta_items.append(Div(Div("Iterable Org ID", cls="dm-label"), Div(str(org_id), cls="dm-value"), cls="drawer-meta-item"))
    if sfdc_id:
        meta_items.append(Div(Div("SFDC Account ID", cls="dm-label"), Div(str(sfdc_id), cls="dm-value"), cls="drawer-meta-item"))
    if not meta_items:
        meta_items.append(Div(Div("Iterable Org ID", cls="dm-label"), Div("—", cls="dm-value"), cls="drawer-meta-item"))
        meta_items.append(Div(Div("SFDC Account ID", cls="dm-label"), Div("—", cls="dm-value"), cls="drawer-meta-item"))

    # Address section
    def addr_card(label, prefix):
        a1 = d.get(f'{prefix}_ADDR1') or ''
        a2 = d.get(f'{prefix}_ADDR2') or ''
        city = d.get(f'{prefix}_CITY') or ''
        state = d.get(f'{prefix}_STATE') or ''
        zipcode = d.get(f'{prefix}_ZIP') or ''
        country = d.get(f'{prefix}_COUNTRY') or ''
        attn = d.get(f'{prefix}_ATTENTION') or ''
        addressee = d.get(f'{prefix}_ADDRESSEE') or ''
        if not a1 and not city:
            return Div(
                Div(label, cls="addr-type"),
                Div("No address on file", cls="no-data"),
                cls="addr-card"
            )
        lines = []
        if addressee: lines.append(addressee)
        if a1: lines.append(a1)
        if a2: lines.append(a2)
        csz = f"{city}, {state} {zipcode}".strip().strip(',')
        if csz: lines.append(csz)
        if country and country != 'US': lines.append(country)
        return Div(
            Div(label, cls="addr-type"),
            Div(*[Div(ln, cls="addr-line") for ln in lines]),
            Div(f"Attn: {attn}", cls="addr-attn") if attn else "",
            cls="addr-card"
        )

    addr_section = Div(
        Div(
            Div(
                Span("Addresses"),
                cls="accordion-header",
                onclick="toggleAccordion(this)"
            ),
            Div(
                Div(
                    Div(
                        addr_card("Bill To", "BILL"),
                        addr_card("Ship To", "SHIP"),
                        cls="addr-grid"
                    ),
                    cls="accordion-content"
                ),
                cls="accordion-body"
            ),
            cls="accordion-item open"
        )
    )

    # Contacts section
    contacts = d.get('contacts', [])
    if contacts:
        contact_rows = [
            Div(
                Div("Name", cls="contact-name"),
                Div("Title", cls="contact-title"),
                Div("Email", cls="contact-email"),
                Div("Phone", cls="contact-phone"),
                cls="contact-row contact-header"
            )
        ]
        for ct in contacts[:50]:
            email_el = A(ct['email'], href=f"mailto:{ct['email']}") if ct['email'] else Span("—")
            contact_rows.append(Div(
                Div(ct['name'] or '—', cls="contact-name"),
                Div(ct['title'] or '—', cls="contact-title"),
                Div(email_el, cls="contact-email"),
                Div(ct['phone'] or '—', cls="contact-phone"),
                cls="contact-row"
            ))
        contact_content = Div(*contact_rows, cls="accordion-content")
    else:
        contact_content = Div(Div("No SFDC contacts linked to this account.", cls="no-data"), cls="accordion-content")

    contact_section = Div(
        Div(
            Div(
                Span("Contacts"),
                Span(f"({len(contacts)})", cls="acc-badge"),
                cls="accordion-header",
                onclick="toggleAccordion(this)"
            ),
            Div(contact_content, cls="accordion-body"),
            cls="accordion-item open"
        )
    )

    return Div(
        summary,
        Div(*meta_items, cls="drawer-meta"),
        addr_section,
        contact_section,
    )


@rt("/")
def get():
    data = _CACHED_DATA or []
    today = date.today().strftime("%B %d, %Y")

    buckets = {'31-60': [], '61-90': [], '91-120': [], '120+': []}
    for r in data:
        b = r['AGING_BUCKET']
        if b in buckets: buckets[b].append(r)

    totals = {b: sum(r['INVOICE_BALANCE'] for r in rs) for b, rs in buckets.items()}
    counts = {b: len(rs) for b, rs in buckets.items()}
    grand_total = sum(totals.values())
    grand_count = sum(counts.values())
    total_inv_amount = sum(r['INVOICE_AMOUNT'] for r in data)

    cust_totals = {}
    for r in data:
        n = r['CUSTOMER_NAME']
        cust_totals[n] = cust_totals.get(n, 0) + r['INVOICE_BALANCE']
    top10 = sorted(cust_totals.items(), key=lambda x: -x[1])[:10]

    def pct(b):
        return f"{totals.get(b,0)/grand_total*100:.1f}" if grand_total else "0.0"
    mx = max(totals.values()) if totals.values() else 1
    bc = {'31-60':'#f5c542','61-90':'#f59e42','91-120':'#f54242','120+':'#ff6b6b'}

    def th_sort(label, idx, right=False):
        cls = "r" if right else ""
        return Th(
            Span(label), Span("\u2195", cls="arrow"),
            cls=cls, onclick=f"sortTable({idx})"
        )

    return Div(
        # Header
        Div(
            Div(
                H1("AR Aging for Collections"),
                Div(f"Live from NetSuite via Snowflake  \u00b7  {today}", cls="subtitle"),
            ),
            Div(
                Span(f"{grand_count} invoices", cls="header-count", id="header-count"),
                Span(fm(grand_total), cls="header-total", id="header-total"),
                A("Refresh", href="/refresh", cls="refresh-btn"),
                cls="header-right"
            ),
            cls="app-header"
        ),

        Div(
            # KPI cards
            Div(
                Div(Div("Total Outstanding", cls="label"), Div(fm(grand_total), cls="value"), Div(f"{grand_count} invoices past due 31+ days", cls="sub"),
                    cls="metric-card c-total", data_bucket="", onclick="filterByBucket('')",
                    title=f"Total unpaid AR balance across all aging buckets.\n{grand_count} invoices | {fm(grand_total)} outstanding\nOriginal invoice total: {fm(total_inv_amount)}\nClick to clear filters and show all."),
                Div(Div("31-60 Days", cls="label"), Div(fm(totals.get('31-60',0)), cls="value"), Div(f"{counts.get('31-60',0)} invoices", cls="sub"),
                    cls="metric-card c-31", data_bucket="31-60", onclick="filterByBucket('31-60')",
                    title=f"Invoices 31-60 days past due date.\n{counts.get('31-60',0)} invoices | {fm(totals.get('31-60',0))} outstanding\n{pct('31-60')}% of total AR balance\nClick to filter table to this bucket."),
                Div(Div("61-90 Days", cls="label"), Div(fm(totals.get('61-90',0)), cls="value"), Div(f"{counts.get('61-90',0)} invoices", cls="sub"),
                    cls="metric-card c-61", data_bucket="61-90", onclick="filterByBucket('61-90')",
                    title=f"Invoices 61-90 days past due date.\n{counts.get('61-90',0)} invoices | {fm(totals.get('61-90',0))} outstanding\n{pct('61-90')}% of total AR balance\nClick to filter table to this bucket."),
                Div(Div("91-120 Days", cls="label"), Div(fm(totals.get('91-120',0)), cls="value"), Div(f"{counts.get('91-120',0)} invoices", cls="sub"),
                    cls="metric-card c-91", data_bucket="91-120", onclick="filterByBucket('91-120')",
                    title=f"Invoices 91-120 days past due date.\n{counts.get('91-120',0)} invoices | {fm(totals.get('91-120',0))} outstanding\n{pct('91-120')}% of total AR balance\nClick to filter table to this bucket."),
                Div(Div("120+ Days", cls="label"), Div(fm(totals.get('120+',0)), cls="value"), Div(f"{counts.get('120+',0)} invoices", cls="sub"),
                    cls="metric-card c-120", data_bucket="120+", onclick="filterByBucket('120+')",
                    title=f"Invoices more than 120 days past due date.\n{counts.get('120+',0)} invoices | {fm(totals.get('120+',0))} outstanding\n{pct('120+')}% of total AR balance\nClick to filter table to this bucket."),
                cls="metrics-row"
            ),

            # Chart + Top 10
            Div(
                Div(
                    H3("Balance by Aging Bucket"),
                    Div(
                        *[Div(
                            Div(fm(totals.get(b,0)), cls="bar-val"),
                            Div(style=f"height:{max(int(totals.get(b,0)/mx*120),4)}px;background:{bc[b]};", cls="bar"),
                            Div(b, cls="bar-lbl"),
                            cls="bar-col", data_bucket=b, onclick=f"filterByBucket('{b}')"
                        ) for b in ['31-60','61-90','91-120','120+']],
                        cls="bar-chart"
                    ),
                    cls="panel"
                ),
                Div(
                    H3("Top 10 Customers by Outstanding Balance"),
                    Ul(
                        *[Li(
                            Span(n, cls="top-name"),
                            Span(fm(a), cls="top-amt")
                        ) for n, a in top10],
                        cls="top-list", id="top10-list"
                    ),
                    cls="panel"
                ),
                cls="panels"
            ),

            # Filters
            Div(
                Input(id="search", placeholder="Search customer or invoice...", oninput="applyFilters()"),
                Select(
                    Option("All Buckets", value=""),
                    Option("31-60 Days", value="31-60"),
                    Option("61-90 Days", value="61-90"),
                    Option("91-120 Days", value="91-120"),
                    Option("120+ Days", value="120+"),
                    id="bucket-filter", onchange="activeBucket=this.value;applyFilters()"
                ),
                cls="filter-bar"
            ),

            # Table
            Div(
                Table(
                    Thead(Tr(
                        th_sort("Acct #", 0),
                        th_sort("Customer", 1),
                        th_sort("Invoice #", 2),
                        th_sort("Inv Date", 3),
                        th_sort("Inv Amount", 4, right=True),
                        th_sort("Due Date", 5),
                        th_sort("Balance", 6, right=True),
                        th_sort("Days", 7, right=True),
                        th_sort("Bucket", 8),
                        Th("Notes", style="text-align:center;cursor:default;"),
                    )),
                    Tbody(*[
                        Tr(
                            Td(str(r['ACCOUNT_NUMBER']), data_val=str(r['ACCOUNT_NUMBER'])),
                            Td(str(r['CUSTOMER_NAME']), cls="cust",
                               onclick=f"openDrawer('{_esc(str(r['ACCOUNT_NUMBER']))}', '{_esc(str(r['CUSTOMER_NAME']))}')"),
                            Td(str(r['INVOICE_NUMBER'])),
                            Td(str(r['INVOICE_DATE']), data_val=str(r['INVOICE_DATE'])),
                            Td(fm(r['INVOICE_AMOUNT']), cls="r", data_val=str(r['INVOICE_AMOUNT'])),
                            Td(str(r['DUE_DATE']), data_val=str(r['DUE_DATE'])),
                            Td(fm(r['INVOICE_BALANCE']), cls="r", data_val=str(r['INVOICE_BALANCE'])),
                            Td(str(r['DAYS_PAST_DUE']), cls="r", data_val=str(r['DAYS_PAST_DUE'])),
                            Td(badge(r['AGING_BUCKET'])),
                            Td(
                                Span(
                                    NotStr("&#128221;") if _NOTES.get(r['INVOICE_NUMBER']) else NotStr("&#128203;"),
                                    cls="notes-icon" + (" has-notes" if _NOTES.get(r['INVOICE_NUMBER']) else ""),
                                ),
                                cls="notes-cell",
                                onclick=f"openNotes('{r['INVOICE_NUMBER']}', '{_esc(str(r['CUSTOMER_NAME']))}')",
                            ),
                            data_bucket=r['AGING_BUCKET'],
                            data_inv=r['INVOICE_NUMBER']
                        ) for r in data
                    ]),
                    Tfoot(Tr(
                        Td(""),
                        Td(Span(f"{grand_count} invoices", cls="tf-count"), style="font-weight:700;"),
                        Td(""), Td(""),
                        Td(fm(total_inv_amount), cls="r tf-amt"),
                        Td(""),
                        Td(fm(grand_total), cls="r tf-bal"),
                        Td(""), Td(""), Td(""),
                        id="tfoot-total"
                    )),
                    id="ar-table"
                ),
                cls="table-wrap"
            ),
            cls="container"
        ),
        # Notes modal
        Div(
            Div(
                H4("Collection Notes"),
                Div("", id="notes-inv-label", cls="inv-label"),
                Div("", id="notes-body", cls="notes-body"),
                Textarea(id="notes-textarea", cls="notes-textarea", placeholder="Add notes..."),
                Div(
                    Button("Cancel", cls="notes-btn", onclick="closeNotes()"),
                    Button("Save Note", cls="notes-btn primary", onclick="saveNotes()"),
                    cls="notes-actions"
                ),
                cls="notes-modal"
            ),
            id="notes-overlay", cls="notes-overlay", onclick="if(event.target===this)closeNotes()"
        ),
        # Customer detail drawer
        Div(id="drawer-overlay", cls="drawer-overlay", onclick="closeDrawer()"),
        Div(
            Div(
                Div(
                    H3("", id="drawer-cust-name", style="margin:0;"),
                    Div("", id="drawer-acct-num", cls="drawer-sub"),
                    cls="drawer-header-left"
                ),
                Button(NotStr("&times;"), cls="drawer-close", onclick="closeDrawer()"),
                cls="drawer-header"
            ),
            Div(id="drawer-body", cls="drawer-body"),
            id="customer-drawer", cls="drawer"
        ),
        # Inject notes data
        Script(f"initNotes({json.dumps(_NOTES)});"),
    )


@rt("/refresh")
def refresh_data():
    global _CACHED_DATA, _CUSTOMER_DETAIL
    _CACHED_DATA = _query_snowflake()
    _CUSTOMER_DETAIL = None  # will lazy-load on next drawer open
    return RedirectResponse("/", status_code=303)


serve(host="0.0.0.0", port=int(os.environ.get("PORT", 5099)))
