from fasthtml.common import *
from starlette.responses import RedirectResponse
import snowflake.connector
from datetime import date
import os

# ---------------------------------------------------------------------------
# Data layer -- prefetch at startup
# ---------------------------------------------------------------------------
print("Connecting to Snowflake...")
_CACHED_DATA = None

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

def _query_snowflake():
    conn = _get_connection()
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
    conn.close()
    return [dict(zip(cols, r)) for r in rows]

_CACHED_DATA = _query_snowflake()
print(f"Loaded {len(_CACHED_DATA)} invoices from NetSuite.")

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
    border-radius: 12px; padding: 20px;
}
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
.bar-col { display: flex; flex-direction: column; align-items: center; flex: 1; gap: 4px; }
.bar-val { font-size: 12px; font-weight: 600; color: var(--text); }
.bar { width: 100%; border-radius: 6px 6px 0 0; min-height: 4px; }
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
    border-bottom: 2px solid var(--border);
}
th.r { text-align: right; }
tr { border-bottom: 1px solid var(--border); }
tbody tr:hover { background: var(--surface2); }
td { padding: 10px 12px; white-space: nowrap; color: var(--text); }
td.r { text-align: right; font-variant-numeric: tabular-nums; }
td.cust { max-width: 240px; overflow: hidden; text-overflow: ellipsis; }
"""

JS = """
function filterTable() {
    var s = document.getElementById('search').value.toLowerCase();
    var b = document.getElementById('bucket-filter').value;
    document.querySelectorAll('#ar-table tbody tr').forEach(function(row) {
        var text = row.textContent.toLowerCase();
        var rb = row.getAttribute('data-bucket');
        var ms = !s || text.indexOf(s) >= 0;
        var mb = !b || rb === b;
        row.style.display = (ms && mb) ? '' : 'none';
    });
}
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

    cust_totals = {}
    for r in data:
        n = r['CUSTOMER_NAME']
        cust_totals[n] = cust_totals.get(n, 0) + r['INVOICE_BALANCE']
    top10 = sorted(cust_totals.items(), key=lambda x: -x[1])[:10]

    mx = max(totals.values()) if totals.values() else 1
    bc = {'31-60':'#f5c542','61-90':'#f59e42','91-120':'#f54242','120+':'#ff6b6b'}

    return Div(
        # Header
        Div(
            Div(
                H1("AR Aging for Collections"),
                Div(f"Live from NetSuite via Snowflake  \u00b7  {today}", cls="subtitle"),
            ),
            Div(
                Span(f"{grand_count} invoices", cls="header-count"),
                Span(fm(grand_total), cls="header-total"),
                A("Refresh", href="/refresh", cls="refresh-btn"),
                cls="header-right"
            ),
            cls="app-header"
        ),

        Div(
            # KPI cards
            Div(
                Div(Div("Total Outstanding", cls="label"), Div(fm(grand_total), cls="value"), Div(f"{grand_count} invoices past due 31+ days", cls="sub"), cls="metric-card c-total"),
                Div(Div("31-60 Days", cls="label"), Div(fm(totals.get('31-60',0)), cls="value"), Div(f"{counts.get('31-60',0)} invoices", cls="sub"), cls="metric-card c-31"),
                Div(Div("61-90 Days", cls="label"), Div(fm(totals.get('61-90',0)), cls="value"), Div(f"{counts.get('61-90',0)} invoices", cls="sub"), cls="metric-card c-61"),
                Div(Div("91-120 Days", cls="label"), Div(fm(totals.get('91-120',0)), cls="value"), Div(f"{counts.get('91-120',0)} invoices", cls="sub"), cls="metric-card c-91"),
                Div(Div("120+ Days", cls="label"), Div(fm(totals.get('120+',0)), cls="value"), Div(f"{counts.get('120+',0)} invoices", cls="sub"), cls="metric-card c-120"),
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
                            cls="bar-col"
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
                        cls="top-list"
                    ),
                    cls="panel"
                ),
                cls="panels"
            ),

            # Filters
            Div(
                Input(id="search", placeholder="Search customer or invoice...", oninput="filterTable()"),
                Select(
                    Option("All Buckets", value=""),
                    Option("31-60 Days", value="31-60"),
                    Option("61-90 Days", value="61-90"),
                    Option("91-120 Days", value="91-120"),
                    Option("120+ Days", value="120+"),
                    id="bucket-filter", onchange="filterTable()"
                ),
                cls="filter-bar"
            ),

            # Table
            Div(
                Table(
                    Thead(Tr(
                        Th("Acct #"), Th("Customer"), Th("Invoice #"),
                        Th("Inv Date"), Th("Inv Amount", cls="r"),
                        Th("Due Date"), Th("Balance", cls="r"),
                        Th("Days", cls="r"), Th("Bucket")
                    )),
                    Tbody(*[
                        Tr(
                            Td(str(r['ACCOUNT_NUMBER'])),
                            Td(str(r['CUSTOMER_NAME']), cls="cust"),
                            Td(str(r['INVOICE_NUMBER'])),
                            Td(str(r['INVOICE_DATE'])),
                            Td(fm(r['INVOICE_AMOUNT']), cls="r"),
                            Td(str(r['DUE_DATE'])),
                            Td(fm(r['INVOICE_BALANCE']), cls="r"),
                            Td(str(r['DAYS_PAST_DUE']), cls="r"),
                            Td(badge(r['AGING_BUCKET'])),
                            data_bucket=r['AGING_BUCKET']
                        ) for r in data
                    ]),
                    id="ar-table"
                ),
                cls="table-wrap"
            ),
            cls="container"
        ),
    )


@rt("/refresh")
def refresh_data():
    global _CACHED_DATA
    _CACHED_DATA = _query_snowflake()
    return RedirectResponse("/", status_code=303)


serve(host="0.0.0.0", port=int(os.environ.get("PORT", 5099)))
