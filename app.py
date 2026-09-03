from fasthtml.common import *
from starlette.responses import RedirectResponse
import snowflake.connector
from datetime import date
import os, json, threading

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

# Customer detail loads in a background thread so the app starts fast
# but drawer data is ready by the time someone clicks
_CUSTOMER_DETAIL = None
_CUSTOMER_DETAIL_LOADING = False

def _load_customer_detail_bg():
    global _CUSTOMER_DETAIL, _CUSTOMER_DETAIL_LOADING
    _CUSTOMER_DETAIL_LOADING = True
    try:
        print("Background: loading customer detail...")
        _CUSTOMER_DETAIL = _query_customer_details()
        print(f"Background: loaded detail for {len(_CUSTOMER_DETAIL)} customers.")
    except Exception as e:
        print(f"Background: failed to load customer detail: {e}")
    finally:
        _CUSTOMER_DETAIL_LOADING = False

# Start background load immediately after startup
threading.Thread(target=_load_customer_detail_bg, daemon=True).start()

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
.header-total { font-size: 20px; font-weight: 700; color: var(--accent); display: inline; }
.header-inv-amt { font-size: 11px; color: var(--text-muted); margin-left: 4px; display: inline; }
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

/* Easter egg modal */
.help-btn {
    display: inline-flex; align-items: center; justify-content: center;
    width: 32px; height: 32px; border-radius: 50%; border: 1px solid var(--border);
    background: var(--surface2); color: var(--text-muted); font-size: 14px; font-weight: 700;
    cursor: pointer; margin-left: 12px; transition: all 0.2s; vertical-align: middle;
}
.help-btn:hover { border-color: var(--accent); color: var(--accent); background: rgba(108,140,255,0.1); }

.ee-overlay {
    display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    background: rgba(0,0,0,0.7); z-index: 2000; justify-content: center; align-items: flex-start;
    overflow-y: auto; padding: 40px 20px;
}
.ee-overlay.open { display: flex; }
.ee-modal {
    background: var(--surface); border: 1px solid var(--border); border-radius: 16px;
    width: 900px; max-width: 95vw; padding: 0; position: relative;
    box-shadow: 0 24px 80px rgba(0,0,0,0.5);
}
.ee-header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 20px 28px; border-bottom: 1px solid var(--border);
}
.ee-header h2 { font-size: 18px; font-weight: 600; color: var(--text); }
.ee-close {
    background: none; border: none; color: var(--text-muted); font-size: 22px;
    cursor: pointer; padding: 4px 8px; line-height: 1;
}
.ee-close:hover { color: var(--text); }
.ee-tabs {
    display: flex; gap: 0; border-bottom: 1px solid var(--border);
}
.ee-tab {
    padding: 12px 24px; font-size: 13px; font-weight: 500; color: var(--text-muted);
    cursor: pointer; border-bottom: 2px solid transparent; transition: all 0.2s;
    background: none; border-top: none; border-left: none; border-right: none;
}
.ee-tab:hover { color: var(--text); background: var(--surface2); }
.ee-tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.ee-content { padding: 24px 28px; }
.ee-panel { display: none; }
.ee-panel.active { display: block; }
.ee-diagram-wrap {
    background: #f5f5f5; border-radius: 10px; padding: 16px; margin-bottom: 20px;
    overflow-x: auto;
}
.ee-diagram-wrap svg { width: 100%; height: auto; display: block; }
.ee-walkthrough h3 { font-size: 15px; font-weight: 600; color: var(--accent); margin: 20px 0 8px 0; }
.ee-walkthrough h3:first-child { margin-top: 0; }
.ee-walkthrough p { font-size: 13px; color: var(--text); line-height: 1.7; margin-bottom: 12px; }
.ee-walkthrough ul { padding-left: 20px; margin-bottom: 12px; }
.ee-walkthrough li { font-size: 13px; color: var(--text); line-height: 1.7; margin-bottom: 4px; }
.ee-walkthrough .ee-highlight { color: var(--accent); font-weight: 600; }
.ee-walkthrough .ee-muted { color: var(--text-muted); font-style: italic; }
.ee-walkthrough code {
    background: var(--surface2); padding: 2px 6px; border-radius: 4px;
    font-size: 12px; color: var(--yellow);
}

/* Theme toggle */
.theme-toggle {
    display: inline-flex; align-items: center; gap: 6px;
    margin-left: 12px; padding: 5px 12px; border-radius: 8px;
    border: 1px solid var(--border); background: var(--surface2);
    color: var(--text-muted); font-size: 12px; font-weight: 500;
    cursor: pointer; transition: all 0.2s; vertical-align: middle;
    font-family: inherit;
}
.theme-toggle:hover { border-color: var(--accent); color: var(--accent); }
.theme-toggle .toggle-icon { font-size: 14px; }

/* Iterable brand theme */
[data-theme="iterable"] {
    --bg: #f5f0eb; --surface: #ffffff; --surface2: #ede8e2;
    --border: #d0c8bf; --text: #160f29; --text-muted: #5f6577;
    --accent: #005a72; --green: #399d89; --yellow: #b8860b;
    --orange: #c76a15; --red: #cc2936;
}
[data-theme="iterable"] body {
    font-family: 'Poppins', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}
[data-theme="iterable"] .app-header {
    background: linear-gradient(135deg, #005a72 0%, #2c8798 100%);
    border-bottom: none;
}
[data-theme="iterable"] .app-header h1 { color: #ffffff; font-family: 'Spectral', Georgia, serif; font-weight: 600; }
[data-theme="iterable"] .app-header .subtitle { color: rgba(255,255,255,0.75); }
[data-theme="iterable"] .header-count { color: rgba(255,255,255,0.7); }
[data-theme="iterable"] .header-total { color: #d5ff9f; }
[data-theme="iterable"] .header-inv-amt { color: rgba(255,255,255,0.65); }
[data-theme="iterable"] .refresh-btn {
    background: rgba(255,255,255,0.15); color: #ffffff; border-color: rgba(255,255,255,0.3);
}
[data-theme="iterable"] .refresh-btn:hover { background: rgba(255,255,255,0.25); }
[data-theme="iterable"] .help-btn {
    background: rgba(255,255,255,0.15); color: rgba(255,255,255,0.8); border-color: rgba(255,255,255,0.3);
}
[data-theme="iterable"] .help-btn:hover { color: #ffffff; border-color: #ffffff; background: rgba(255,255,255,0.25); }
[data-theme="iterable"] .theme-toggle {
    background: rgba(255,255,255,0.15); color: rgba(255,255,255,0.8); border-color: rgba(255,255,255,0.3);
}
[data-theme="iterable"] .theme-toggle:hover { color: #ffffff; border-color: #ffffff; }

/* Iterable metric cards */
[data-theme="iterable"] .metric-card { box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
[data-theme="iterable"] .metric-card:hover { border-color: #2c8798; }
[data-theme="iterable"] .metric-card.active { border-color: #2c8798; box-shadow: 0 0 0 1px #2c8798; }
[data-theme="iterable"] .c-total .value { color: #005a72; }
[data-theme="iterable"] .c-31 .value { color: #b8860b; }
[data-theme="iterable"] .c-61 .value { color: #c76a15; }
[data-theme="iterable"] .c-91 .value { color: #cc2936; }
[data-theme="iterable"] .c-120 .value { color: #a01a28; }
[data-theme="iterable"] .metric-card .label { color: #005a72; font-weight: 500; }
[data-theme="iterable"] .metric-card .value { font-family: 'Spectral', Georgia, serif; }

/* Iterable badges */
[data-theme="iterable"] .b-31  { background: rgba(184,134,11,0.12); color: #8b6914; }
[data-theme="iterable"] .b-61  { background: rgba(199,106,21,0.12); color: #9a5210; }
[data-theme="iterable"] .b-91  { background: rgba(204,41,54,0.12); color: #a01a28; }
[data-theme="iterable"] .b-120 { background: rgba(160,26,40,0.12); color: #7d1420; }

/* Iterable panels and table */
[data-theme="iterable"] .panel { box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
[data-theme="iterable"] .panel h3 { color: #005a72; font-weight: 600; text-transform: uppercase; font-size: 12px; letter-spacing: 0.5px; }
[data-theme="iterable"] .table-wrap { box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
[data-theme="iterable"] th { background: #ede8e2 !important; color: #005a72 !important; }
[data-theme="iterable"] th:hover { color: #160f29 !important; }
[data-theme="iterable"] th.sorted .arrow { color: #005a72; }
[data-theme="iterable"] tbody tr:hover { background: #f5f0eb; }
[data-theme="iterable"] tbody tr.selected { background: rgba(0,90,114,0.08); border-left: 3px solid #005a72; }
[data-theme="iterable"] tfoot td { color: #005a72; background: #ede8e2; }

/* Iterable drawer and modals */
[data-theme="iterable"] .drawer { box-shadow: -4px 0 24px rgba(0,0,0,0.12); }
[data-theme="iterable"] .drawer-bal { color: #005a72; }
[data-theme="iterable"] .addr-card .addr-type { color: #2c8798; }
[data-theme="iterable"] .contact-email a { color: #005a72; }
[data-theme="iterable"] .drawer-overlay { background: rgba(22,15,41,0.3); }
[data-theme="iterable"] .notes-overlay { background: rgba(22,15,41,0.4); }
[data-theme="iterable"] .notes-btn.primary { background: #005a72; border-color: #005a72; }
[data-theme="iterable"] .ee-overlay { background: rgba(22,15,41,0.4); }
[data-theme="iterable"] .ee-modal { box-shadow: 0 24px 80px rgba(0,0,0,0.15); }
[data-theme="iterable"] .ee-tab.active { color: #005a72; border-bottom-color: #005a72; }
[data-theme="iterable"] .ee-walkthrough h3 { color: #005a72; }
[data-theme="iterable"] .ee-walkthrough code { color: #b8860b; }

/* Iterable filter bar */
[data-theme="iterable"] .filter-bar input, [data-theme="iterable"] .filter-bar select {
    background: #ffffff; border-color: #d0c8bf;
}
[data-theme="iterable"] .filter-bar input:focus, [data-theme="iterable"] .filter-bar select:focus { border-color: #005a72; }
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
    var hi = document.getElementById('header-inv-amt');
    if (hc) hc.textContent = count + ' invoices';
    if (ht) ht.textContent = fm(totalBal);
    if (hi) {
        hi.textContent = 'Inv Amt: ' + fm(totalAmt);
        hi.parentElement.title = 'Balance Due: ' + fm(totalBal) + '\\nInvoice Amount: ' + fm(totalAmt);
    }
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
        .then(function(html) {
            body.innerHTML = html;
            // Auto-retry if still loading
            if (html.indexOf('Loading customer data') >= 0) {
                setTimeout(function() {
                    if (drawer.classList.contains('open')) {
                        fetch('/customer-detail/' + encodeURIComponent(acctNum))
                            .then(function(r) { return r.text(); })
                            .then(function(h) { body.innerHTML = h; });
                    }
                }, 5000);
            }
        });
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

// Easter egg modal
function openHelp() {
    document.getElementById('ee-overlay').classList.add('open');
    switchEETab('integration');
}
function closeHelp() {
    document.getElementById('ee-overlay').classList.remove('open');
}
function switchEETab(tab) {
    document.querySelectorAll('.ee-tab').forEach(function(t) { t.classList.remove('active'); });
    document.querySelectorAll('.ee-panel').forEach(function(p) { p.classList.remove('active'); });
    document.querySelector('.ee-tab[data-tab="' + tab + '"]').classList.add('active');
    document.getElementById('ee-' + tab).classList.add('active');
}

// Theme toggle
function toggleTheme() {
    var html = document.documentElement;
    var current = html.getAttribute('data-theme');
    var icon = document.getElementById('theme-icon');
    var label = document.getElementById('theme-label');
    if (current === 'iterable') {
        html.removeAttribute('data-theme');
        if (icon) icon.textContent = '\\ud83c\\udf19';
        if (label) label.textContent = 'Theme';
        try { localStorage.setItem('ar-theme', 'dark'); } catch(e) {}
    } else {
        html.setAttribute('data-theme', 'iterable');
        if (icon) icon.textContent = '\\u2600\\ufe0f';
        if (label) label.textContent = 'Theme';
        try { localStorage.setItem('ar-theme', 'iterable'); } catch(e) {}
    }
}

document.addEventListener('DOMContentLoaded', function() {
    // Restore saved theme
    try {
        var saved = localStorage.getItem('ar-theme');
        if (saved === 'iterable') {
            document.documentElement.setAttribute('data-theme', 'iterable');
            var icon = document.getElementById('theme-icon');
            if (icon) icon.textContent = '\\u2600\\ufe0f';
        }
    } catch(e) {}
    updateTotals();
});
"""

# ---------------------------------------------------------------------------
# SVG Diagrams (diagram-design style)
# ---------------------------------------------------------------------------
def _dp_integration_svg():
    return '''<svg viewBox="0 0 840 420" xmlns="http://www.w3.org/2000/svg" role="img">
  <title>Data Platform Integration</title>
  <defs>
    <marker id="arr" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0,8 3,0 6" fill="#4f5d75"/></marker>
    <marker id="arr-a" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0,8 3,0 6" fill="#eb6c36"/></marker>
    <marker id="arr-l" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0,8 3,0 6" fill="#2e5aa8"/></marker>
  </defs>
  <rect width="840" height="420" fill="#f5f5f5" rx="8"/>
  <!-- Title -->
  <text x="420" y="32" text-anchor="middle" font-family="Georgia,serif" font-size="16" fill="#2d3142" font-weight="400">Data Platform Integration</text>
  <text x="420" y="48" text-anchor="middle" font-family="monospace" font-size="8" fill="#4f5d75" letter-spacing="1">DP INTEGRATION · AR COLLECTIONS</text>

  <!-- Sources column -->
  <text x="80" y="76" text-anchor="middle" font-family="sans-serif" font-size="9" fill="#4f5d75" font-weight="600" letter-spacing="1">SOURCES</text>
  <!-- NetSuite -->
  <rect x="16" y="92" width="128" height="56" rx="6" fill="#fff" stroke="#2e3348" stroke-width="1"/>
  <text x="80" y="114" text-anchor="middle" font-family="sans-serif" font-size="11" fill="#2d3142" font-weight="600">NetSuite</text>
  <text x="80" y="128" text-anchor="middle" font-family="monospace" font-size="8" fill="#4f5d75">ERP · invoices</text>
  <text x="80" y="140" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">customers · addresses</text>
  <!-- Salesforce -->
  <rect x="16" y="164" width="128" height="56" rx="6" fill="#fff" stroke="#2e3348" stroke-width="1"/>
  <text x="80" y="186" text-anchor="middle" font-family="sans-serif" font-size="11" fill="#2d3142" font-weight="600">Salesforce</text>
  <text x="80" y="200" text-anchor="middle" font-family="monospace" font-size="8" fill="#4f5d75">CRM · accounts</text>
  <text x="80" y="212" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">contacts</text>

  <!-- Platform zone -->
  <rect x="196" y="68" width="448" height="280" rx="8" fill="none" stroke="#2e3348" stroke-width="1" stroke-dasharray="6,3"/>
  <text x="420" y="86" text-anchor="middle" font-family="monospace" font-size="8" fill="#4f5d75" font-weight="600" letter-spacing="2">DATA PLATFORM</text>

  <!-- Fivetran -->
  <rect x="220" y="108" width="128" height="56" rx="6" fill="#fff" stroke="#eb6c36" stroke-width="1.5"/>
  <text x="284" y="130" text-anchor="middle" font-family="sans-serif" font-size="11" fill="#2d3142" font-weight="600">Fivetran</text>
  <text x="284" y="144" text-anchor="middle" font-family="monospace" font-size="8" fill="#eb6c36">sync · CDC</text>
  <text x="284" y="156" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">automated replication</text>

  <!-- Snowflake -->
  <rect x="220" y="184" width="128" height="56" rx="6" fill="#fff" stroke="#eb6c36" stroke-width="1.5"/>
  <text x="284" y="206" text-anchor="middle" font-family="sans-serif" font-size="11" fill="#2d3142" font-weight="600">Snowflake</text>
  <text x="284" y="220" text-anchor="middle" font-family="monospace" font-size="8" fill="#eb6c36">FIVETRAN_DB</text>
  <text x="284" y="232" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">warehouse: BILLING_PIPE</text>

  <!-- Snowflake detail boxes -->
  <rect x="380" y="100" width="120" height="44" rx="4" fill="#fff" stroke="#2e3348" stroke-width="1"/>
  <text x="440" y="118" text-anchor="middle" font-family="sans-serif" font-size="9" fill="#2d3142" font-weight="600">NETSUITE_SUITE</text>
  <text x="440" y="132" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">TRANSACTION · CUSTOMER</text>

  <rect x="380" y="156" width="120" height="44" rx="4" fill="#fff" stroke="#2e3348" stroke-width="1"/>
  <text x="440" y="174" text-anchor="middle" font-family="sans-serif" font-size="9" fill="#2d3142" font-weight="600">FT_SALESFORCE</text>
  <text x="440" y="188" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">ACCOUNT · CONTACT</text>

  <!-- Query box -->
  <rect x="380" y="216" width="120" height="44" rx="4" fill="#fff" stroke="#eb6c36" stroke-width="1.5"/>
  <text x="440" y="234" text-anchor="middle" font-family="sans-serif" font-size="9" fill="#2d3142" font-weight="600">SQL Query</text>
  <text x="440" y="248" text-anchor="middle" font-family="monospace" font-size="7" fill="#eb6c36">JOIN · AGGREGATE</text>

  <!-- GitHub Actions -->
  <rect x="540" y="100" width="88" height="44" rx="4" fill="#fff" stroke="#2e3348" stroke-width="1"/>
  <text x="584" y="118" text-anchor="middle" font-family="sans-serif" font-size="9" fill="#2d3142" font-weight="600">GitHub</text>
  <text x="584" y="132" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">Actions CI/CD</text>

  <!-- Docker -->
  <rect x="540" y="156" width="88" height="44" rx="4" fill="#fff" stroke="#2e3348" stroke-width="1"/>
  <text x="584" y="174" text-anchor="middle" font-family="sans-serif" font-size="9" fill="#2d3142" font-weight="600">Artifact</text>
  <text x="584" y="188" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">Registry · Docker</text>

  <!-- Consumer column -->
  <text x="760" y="76" text-anchor="middle" font-family="sans-serif" font-size="9" fill="#4f5d75" font-weight="600" letter-spacing="1">CONSUMER</text>
  <!-- Cloud Run -->
  <rect x="696" y="92" width="128" height="56" rx="6" fill="#fff" stroke="#2e5aa8" stroke-width="1.5"/>
  <text x="760" y="114" text-anchor="middle" font-family="sans-serif" font-size="11" fill="#2d3142" font-weight="600">Cloud Run</text>
  <text x="760" y="128" text-anchor="middle" font-family="monospace" font-size="8" fill="#2e5aa8">GCP · us-central1</text>
  <text x="760" y="140" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">ar-collections</text>
  <!-- FastHTML App -->
  <rect x="696" y="164" width="128" height="56" rx="6" fill="#fff" stroke="#2e5aa8" stroke-width="1.5"/>
  <text x="760" y="186" text-anchor="middle" font-family="sans-serif" font-size="11" fill="#2d3142" font-weight="600">FastHTML App</text>
  <text x="760" y="200" text-anchor="middle" font-family="monospace" font-size="8" fill="#2e5aa8">Python · port 8080</text>
  <text x="760" y="212" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">this report</text>
  <!-- Browser -->
  <rect x="696" y="236" width="128" height="44" rx="6" fill="#fff" stroke="#2e3348" stroke-width="1"/>
  <text x="760" y="256" text-anchor="middle" font-family="sans-serif" font-size="11" fill="#2d3142" font-weight="600">Browser</text>
  <text x="760" y="268" text-anchor="middle" font-family="monospace" font-size="8" fill="#4f5d75">collections team</text>

  <!-- Arrows: Sources -> Fivetran -->
  <line x1="144" y1="120" x2="216" y2="128" stroke="#4f5d75" stroke-width="1.2" marker-end="url(#arr)"/>
  <rect x="158" y="116" width="28" height="12" fill="#f5f5f5"/><text x="172" y="125" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">API</text>
  <line x1="144" y1="192" x2="216" y2="144" stroke="#4f5d75" stroke-width="1.2" marker-end="url(#arr)"/>
  <rect x="158" y="158" width="32" height="12" fill="#f5f5f5"/><text x="174" y="167" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">REST</text>

  <!-- Fivetran -> Snowflake -->
  <line x1="284" y1="164" x2="284" y2="180" stroke="#eb6c36" stroke-width="1.2" marker-end="url(#arr-a)"/>
  <rect x="262" y="168" width="44" height="10" fill="#f5f5f5"/><text x="284" y="176" text-anchor="middle" font-family="monospace" font-size="7" fill="#eb6c36">SYNC</text>

  <!-- Snowflake -> Schema boxes -->
  <line x1="348" y1="200" x2="376" y2="122" stroke="#4f5d75" stroke-width="1" marker-end="url(#arr)"/>
  <line x1="348" y1="212" x2="376" y2="178" stroke="#4f5d75" stroke-width="1" marker-end="url(#arr)"/>

  <!-- Schema boxes -> Query -->
  <line x1="440" y1="144" x2="440" y2="212" stroke="#eb6c36" stroke-width="1" marker-end="url(#arr-a)"/>

  <!-- Query -> App (via platform boundary) -->
  <line x1="500" y1="238" x2="692" y2="192" stroke="#2e5aa8" stroke-width="1.2" marker-end="url(#arr-l)"/>
  <rect x="576" y="206" width="36" height="10" fill="#f5f5f5"/><text x="594" y="214" text-anchor="middle" font-family="monospace" font-size="7" fill="#2e5aa8">SQL</text>

  <!-- GitHub -> Registry -->
  <line x1="584" y1="144" x2="584" y2="152" stroke="#4f5d75" stroke-width="1" marker-end="url(#arr)"/>
  <!-- Registry -> Cloud Run -->
  <line x1="628" y1="178" x2="692" y2="120" stroke="#4f5d75" stroke-width="1.2" marker-end="url(#arr)"/>
  <rect x="640" y="140" width="40" height="10" fill="#f5f5f5"/><text x="660" y="148" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">DEPLOY</text>

  <!-- Cloud Run -> App -->
  <line x1="760" y1="148" x2="760" y2="160" stroke="#2e5aa8" stroke-width="1" marker-end="url(#arr-l)"/>
  <!-- App -> Browser -->
  <line x1="760" y1="220" x2="760" y2="232" stroke="#2e5aa8" stroke-width="1" marker-end="url(#arr-l)"/>
  <rect x="740" y="222" width="40" height="10" fill="#f5f5f5"/><text x="760" y="230" text-anchor="middle" font-family="monospace" font-size="7" fill="#2e5aa8">HTTPS</text>

  <!-- Auth bar -->
  <rect x="196" y="360" width="448" height="28" rx="4" fill="#fff" stroke="#eb6c36" stroke-width="1" stroke-dasharray="4,2"/>
  <text x="420" y="378" text-anchor="middle" font-family="sans-serif" font-size="9" fill="#eb6c36" font-weight="500">OAuth / RSA Key-Pair Authentication</text>

  <!-- Legend -->
  <line x1="40" y1="404" x2="64" y2="404" stroke="#4f5d75" stroke-width="1.2" marker-end="url(#arr)"/>
  <text x="68" y="407" font-family="monospace" font-size="7" fill="#4f5d75">Standard</text>
  <line x1="140" y1="404" x2="164" y2="404" stroke="#eb6c36" stroke-width="1.2" marker-end="url(#arr-a)"/>
  <text x="168" y="407" font-family="monospace" font-size="7" fill="#eb6c36">Focal / platform</text>
  <line x1="280" y1="404" x2="304" y2="404" stroke="#2e5aa8" stroke-width="1.2" marker-end="url(#arr-l)"/>
  <text x="308" y="407" font-family="monospace" font-size="7" fill="#2e5aa8">Consumer / API</text>
  <rect x="420" y="398" width="10" height="10" rx="2" fill="none" stroke="#2e3348" stroke-width="1" stroke-dasharray="3,2"/>
  <text x="434" y="407" font-family="monospace" font-size="7" fill="#4f5d75">Platform boundary</text>
</svg>'''


def _data_flow_svg():
    return '''<svg viewBox="0 0 840 380" xmlns="http://www.w3.org/2000/svg" role="img">
  <title>Data Flow - AR Aging Pipeline</title>
  <defs>
    <marker id="df-arr" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0,8 3,0 6" fill="#4f5d75"/></marker>
    <marker id="df-arr-a" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0,8 3,0 6" fill="#eb6c36"/></marker>
    <marker id="df-arr-l" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0,8 3,0 6" fill="#2e5aa8"/></marker>
  </defs>
  <rect width="840" height="380" fill="#f5f5f5" rx="8"/>
  <!-- Title -->
  <text x="420" y="28" text-anchor="middle" font-family="Georgia,serif" font-size="16" fill="#2d3142">Data Flow</text>
  <text x="420" y="44" text-anchor="middle" font-family="monospace" font-size="8" fill="#4f5d75" letter-spacing="1">AR AGING PIPELINE · STEP BY STEP</text>

  <!-- Step labels -->
  <text x="80" y="72" text-anchor="middle" font-family="monospace" font-size="8" fill="#eb6c36" font-weight="600">01 INGEST</text>
  <text x="244" y="72" text-anchor="middle" font-family="monospace" font-size="8" fill="#eb6c36" font-weight="600">02 STORE</text>
  <text x="420" y="72" text-anchor="middle" font-family="monospace" font-size="8" fill="#eb6c36" font-weight="600">03 JOIN</text>
  <text x="596" y="72" text-anchor="middle" font-family="monospace" font-size="8" fill="#eb6c36" font-weight="600">04 COMPUTE</text>
  <text x="760" y="72" text-anchor="middle" font-family="monospace" font-size="8" fill="#eb6c36" font-weight="600">05 SERVE</text>

  <!-- Row 1: Invoice path -->
  <rect x="16" y="88" width="128" height="52" rx="5" fill="#fff" stroke="#2e3348" stroke-width="1"/>
  <text x="80" y="108" text-anchor="middle" font-family="sans-serif" font-size="10" fill="#2d3142" font-weight="600">NetSuite Invoice</text>
  <text x="80" y="122" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">TRANSACTION (CustInvc)</text>
  <text x="80" y="132" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">FOREIGNAMOUNTUNPAID</text>

  <line x1="144" y1="114" x2="180" y2="114" stroke="#4f5d75" stroke-width="1" marker-end="url(#df-arr)"/>

  <rect x="180" y="88" width="128" height="52" rx="5" fill="#fff" stroke="#eb6c36" stroke-width="1.5"/>
  <text x="244" y="108" text-anchor="middle" font-family="sans-serif" font-size="10" fill="#2d3142" font-weight="600">NETSUITE_SUITE</text>
  <text x="244" y="122" text-anchor="middle" font-family="monospace" font-size="7" fill="#eb6c36">TRANSACTION table</text>
  <text x="244" y="132" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">balance · dates · entity</text>

  <!-- Row 2: Customer path -->
  <rect x="180" y="156" width="128" height="52" rx="5" fill="#fff" stroke="#eb6c36" stroke-width="1.5"/>
  <text x="244" y="176" text-anchor="middle" font-family="sans-serif" font-size="10" fill="#2d3142" font-weight="600">CUSTOMER</text>
  <text x="244" y="190" text-anchor="middle" font-family="monospace" font-size="7" fill="#eb6c36">ENTITYID · COMPANYNAME</text>
  <text x="244" y="200" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">org_id · addresses</text>

  <!-- Row 3: SFDC Account path -->
  <rect x="16" y="224" width="128" height="52" rx="5" fill="#fff" stroke="#2e3348" stroke-width="1"/>
  <text x="80" y="244" text-anchor="middle" font-family="sans-serif" font-size="10" fill="#2d3142" font-weight="600">Salesforce</text>
  <text x="80" y="258" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">ACCOUNT · CONTACT</text>
  <text x="80" y="268" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">via Fivetran sync</text>

  <line x1="144" y1="250" x2="180" y2="250" stroke="#4f5d75" stroke-width="1" marker-end="url(#df-arr)"/>

  <rect x="180" y="224" width="128" height="52" rx="5" fill="#fff" stroke="#2e3348" stroke-width="1"/>
  <text x="244" y="244" text-anchor="middle" font-family="sans-serif" font-size="10" fill="#2d3142" font-weight="600">FT_SALESFORCE</text>
  <text x="244" y="258" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">ACCOUNT table</text>
  <text x="244" y="268" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">ID · NAME · IS_DELETED</text>

  <!-- JOIN step - focal -->
  <rect x="356" y="88" width="128" height="68" rx="5" fill="#fff" stroke="#eb6c36" stroke-width="2"/>
  <text x="420" y="108" text-anchor="middle" font-family="sans-serif" font-size="10" fill="#eb6c36" font-weight="700">JOIN</text>
  <text x="420" y="122" text-anchor="middle" font-family="monospace" font-size="7" fill="#2d3142">t.ENTITY = c.ID</text>
  <text x="420" y="134" text-anchor="middle" font-family="monospace" font-size="7" fill="#2d3142">UPPER(NAME) match</text>
  <text x="420" y="148" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">+ address + contact</text>

  <!-- Arrows into JOIN -->
  <line x1="308" y1="114" x2="352" y2="114" stroke="#eb6c36" stroke-width="1.2" marker-end="url(#df-arr-a)"/>
  <line x1="308" y1="182" x2="352" y2="136" stroke="#eb6c36" stroke-width="1" marker-end="url(#df-arr-a)"/>
  <line x1="308" y1="250" x2="352" y2="148" stroke="#4f5d75" stroke-width="1" marker-end="url(#df-arr)"/>

  <!-- SFDC Contact box -->
  <rect x="356" y="224" width="128" height="44" rx="5" fill="#fff" stroke="#2e3348" stroke-width="1"/>
  <text x="420" y="244" text-anchor="middle" font-family="sans-serif" font-size="10" fill="#2d3142" font-weight="600">CONTACT</text>
  <text x="420" y="258" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">name · title · email</text>
  <line x1="420" y1="220" x2="420" y2="156" stroke="#4f5d75" stroke-width="1" stroke-dasharray="4,2" marker-end="url(#df-arr)"/>
  <rect x="398" y="192" width="44" height="10" fill="#f5f5f5"/><text x="420" y="200" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">ACCT_ID</text>

  <!-- COMPUTE step -->
  <rect x="532" y="88" width="128" height="68" rx="5" fill="#fff" stroke="#eb6c36" stroke-width="1.5"/>
  <text x="596" y="108" text-anchor="middle" font-family="sans-serif" font-size="10" fill="#2d3142" font-weight="600">Aging Buckets</text>
  <text x="596" y="122" text-anchor="middle" font-family="monospace" font-size="7" fill="#eb6c36">DATEDIFF(due, today)</text>
  <text x="596" y="134" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">31-60 | 61-90</text>
  <text x="596" y="146" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">91-120 | 120+</text>

  <line x1="484" y1="122" x2="528" y2="122" stroke="#eb6c36" stroke-width="1.2" marker-end="url(#df-arr-a)"/>

  <!-- SERVE step -->
  <rect x="696" y="88" width="128" height="68" rx="5" fill="#fff" stroke="#2e5aa8" stroke-width="1.5"/>
  <text x="760" y="108" text-anchor="middle" font-family="sans-serif" font-size="10" fill="#2d3142" font-weight="600">FastHTML</text>
  <text x="760" y="122" text-anchor="middle" font-family="monospace" font-size="8" fill="#2e5aa8">AR Aging Report</text>
  <text x="760" y="134" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">table + drawer + charts</text>
  <text x="760" y="146" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">notes · sort · filter</text>

  <line x1="660" y1="122" x2="692" y2="122" stroke="#2e5aa8" stroke-width="1.2" marker-end="url(#df-arr-l)"/>

  <!-- Key filter callout -->
  <rect x="532" y="184" width="128" height="36" rx="4" fill="#fff" stroke="#eb6c36" stroke-width="1" stroke-dasharray="4,2"/>
  <text x="596" y="200" text-anchor="middle" font-family="sans-serif" font-size="9" fill="#eb6c36" font-weight="500">UNPAID > $0</text>
  <text x="596" y="212" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">past due 31+ days</text>
  <line x1="596" y1="156" x2="596" y2="180" stroke="#eb6c36" stroke-width="1" stroke-dasharray="4,2"/>

  <!-- Payment removal callout -->
  <rect x="696" y="184" width="128" height="48" rx="4" fill="#fff" stroke="#3dd68c" stroke-width="1.5"/>
  <text x="760" y="200" text-anchor="middle" font-family="sans-serif" font-size="9" fill="#3dd68c" font-weight="600">Payment Posted</text>
  <text x="760" y="214" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">UNPAID drops to $0</text>
  <text x="760" y="224" text-anchor="middle" font-family="monospace" font-size="7" fill="#4f5d75">invoice falls off report</text>

  <!-- Legend -->
  <line x1="40" y1="352" x2="64" y2="352" stroke="#4f5d75" stroke-width="1" marker-end="url(#df-arr)"/>
  <text x="68" y="355" font-family="monospace" font-size="7" fill="#4f5d75">Data flow</text>
  <line x1="160" y1="352" x2="184" y2="352" stroke="#eb6c36" stroke-width="1.2" marker-end="url(#df-arr-a)"/>
  <text x="188" y="355" font-family="monospace" font-size="7" fill="#eb6c36">Focal / join</text>
  <line x1="280" y1="352" x2="304" y2="352" stroke="#2e5aa8" stroke-width="1.2" marker-end="url(#df-arr-l)"/>
  <text x="308" y="355" font-family="monospace" font-size="7" fill="#2e5aa8">Output</text>
  <rect x="380" y="346" width="10" height="10" rx="2" fill="none" stroke="#3dd68c" stroke-width="1.5"/>
  <text x="394" y="355" font-family="monospace" font-size="7" fill="#3dd68c">Paid = removed</text>
  <line x1="500" y1="352" x2="524" y2="352" stroke="#4f5d75" stroke-width="1" stroke-dasharray="4,2"/>
  <text x="528" y="355" font-family="monospace" font-size="7" fill="#4f5d75">Lookup / filter</text>
</svg>'''


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app, rt = fast_app(
    hdrs=[
        Link(rel="preconnect", href="https://fonts.googleapis.com"),
        Link(rel="preconnect", href="https://fonts.gstatic.com", crossorigin=""),
        Link(rel="stylesheet", href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&family=Spectral:wght@600&display=swap"),
        Style(CSS),
        Script(JS),
    ],
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
    if _CUSTOMER_DETAIL is None:
        if _CUSTOMER_DETAIL_LOADING:
            return Div(Div(
                Div("Loading customer data...", style="font-size:14px;color:var(--text);margin-bottom:8px;"),
                Div("Detail is being fetched from Snowflake. This typically takes 30-60 seconds on first load.", style="font-size:12px;color:var(--text-muted);margin-bottom:16px;"),
                Div("Try again in a moment.", style="font-size:12px;color:var(--accent);"),
                style="padding:24px;"
            ))
        return Div(Div("Customer detail is not available.", cls="no-data", style="padding:24px;"))
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
                Span(
                    Span(fm(grand_total), cls="header-total", id="header-total"),
                    Span(f"Inv Amt: {fm(total_inv_amount)}", cls="header-inv-amt", id="header-inv-amt"),
                    title=f"Balance Due: {fm(grand_total)}\nInvoice Amount: {fm(total_inv_amount)}",
                ),
                A("Refresh", href="/refresh", cls="refresh-btn"),
                Button(Span("🌙", cls="toggle-icon", id="theme-icon"), Span("Theme", id="theme-label"), cls="theme-toggle", onclick="toggleTheme()", title="Switch between Dark and Iterable themes"),
                Button("?", cls="help-btn", onclick="openHelp()", title="About this report"),
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
        # Easter egg modal
        Div(
            Div(
                Div(
                    H2("About This Report"),
                    Button(NotStr("&times;"), cls="ee-close", onclick="closeHelp()"),
                    cls="ee-header"
                ),
                Div(
                    Button("Data Platform Integration", cls="ee-tab active", data_tab="integration", onclick="switchEETab('integration')"),
                    Button("Data Flow", cls="ee-tab", data_tab="dataflow", onclick="switchEETab('dataflow')"),
                    Button("How to Use", cls="ee-tab", data_tab="guide", onclick="switchEETab('guide')"),
                    cls="ee-tabs"
                ),
                Div(
                    # Tab 1: DP Integration diagram
                    Div(
                        Div(NotStr(_dp_integration_svg()), cls="ee-diagram-wrap"),
                        Div(
                            P("This diagram shows how data flows from source systems through the integration layer into this report."),
                            cls="ee-walkthrough"
                        ),
                        id="ee-integration", cls="ee-panel active"
                    ),
                    # Tab 2: Data Flow diagram
                    Div(
                        Div(NotStr(_data_flow_svg()), cls="ee-diagram-wrap"),
                        Div(
                            P("This diagram shows the specific data objects and joins used to assemble the AR aging view."),
                            cls="ee-walkthrough"
                        ),
                        id="ee-dataflow", cls="ee-panel"
                    ),
                    # Tab 3: Guide
                    Div(
                        Div(
                            H3("What This Report Is"),
                            Ul(
                                Li("A ", Span("live AR aging report", cls="ee-highlight"), " showing all invoices 31+ days past due, sourced directly from NetSuite via Snowflake."),
                                Li("Invoices are grouped into aging buckets: 31-60, 61-90, 91-120, and 120+ days past the due date."),
                                Li("Each customer row links to a detail drawer with Iterable Org ID, Salesforce Account ID, bill-to/ship-to addresses, and SFDC contacts."),
                                Li("Collection notes can be added per invoice and persist in-session."),
                            ),
                            H3("What This Report Is NOT"),
                            Ul(
                                Li("This is ", Span("not a static snapshot", cls="ee-highlight"), ". Data refreshes each time you load the page or click Refresh."),
                                Li("This does ", Span("not", cls="ee-highlight"), " include invoices that are current (0-30 days) or not yet due."),
                                Li("This does ", Span("not", cls="ee-highlight"), " modify NetSuite data. Notes are local to this app and do not sync back."),
                                Li("SFDC contacts are matched by company name, not a direct CRM ID link. Some customers may show fewer contacts if the name match is imprecise."),
                            ),
                            H3("When Invoices Disappear"),
                            P("When a customer pays an invoice in NetSuite, the ", Code("FOREIGNAMOUNTUNPAID"), " field drops to $0. On the next page load, that invoice will ", Span("automatically fall off this report", cls="ee-highlight"), ". No manual action needed."),
                            P(Span("The report is only as current as the Fivetran sync.", cls="ee-muted"), " NetSuite data syncs to Snowflake via Fivetran on a regular schedule. There may be a short delay between a payment posting in NetSuite and it reflecting here."),
                            H3("How to Use"),
                            Ul(
                                Li(Span("Click a bucket card or bar", cls="ee-highlight"), " to filter the table to that aging range. Click again to clear."),
                                Li(Span("Click a customer name", cls="ee-highlight"), " to open the detail drawer with addresses and contacts."),
                                Li(Span("Click the notes icon", cls="ee-highlight"), " on any invoice row to view or add collection notes."),
                                Li("Use the search box to find customers or invoices by name/number."),
                                Li("Click column headers to sort ascending/descending."),
                            ),
                            H3("Data Sources"),
                            Ul(
                                Li(Code("FIVETRAN_DB.NETSUITE_SUITE.TRANSACTION"), " - Invoice records (type CustInvc)"),
                                Li(Code("FIVETRAN_DB.NETSUITE_SUITE.CUSTOMER"), " - Customer master data"),
                                Li(Code("FIVETRAN_DB.FT_SALESFORCE.ACCOUNT"), " - SFDC account matching"),
                                Li(Code("FIVETRAN_DB.FT_SALESFORCE.CONTACT"), " - SFDC contact details"),
                            ),
                            cls="ee-walkthrough"
                        ),
                        id="ee-guide", cls="ee-panel"
                    ),
                    cls="ee-content"
                ),
                cls="ee-modal"
            ),
            id="ee-overlay", cls="ee-overlay", onclick="if(event.target===this)closeHelp()"
        ),
    )


@rt("/refresh")
def refresh_data():
    global _CACHED_DATA, _CUSTOMER_DETAIL
    _CACHED_DATA = _query_snowflake()
    _CUSTOMER_DETAIL = None
    threading.Thread(target=_load_customer_detail_bg, daemon=True).start()
    return RedirectResponse("/", status_code=303)


serve(host="0.0.0.0", port=int(os.environ.get("PORT", 5099)))
