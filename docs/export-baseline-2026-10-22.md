# AR Collections export baseline (2026-10-22)

Use this to **back-test** changes on `22oct/feature_ar_updates` so fixes do not inflate row counts, balances, or invoice keys.

Reference: production export screenshot (Finance / Collections review, Oct 2026).

## Export scope (unchanged by enrichment fixes)

Same NetSuite filter as `app.py` → `_query_snowflake()`:

- `TYPE = 'CustInvc'`, `_FIVETRAN_DELETED = false`, `FOREIGNAMOUNTUNPAID > 0`
- App **default** export (Current AR off): only buckets `31-60`, `61-90`, `91-120`, `120+`
- With `?include_current=1`: also `Current` and `0-30`

**Regression rule:** For a given `include_current` flag and point-in-time Snowflake data, **row count** and **sum(Balance)** must match the main query (minus intentional filter changes, e.g. new Collector filter).

```sql
-- Row count + total balance (default app export = 31+ only)
WITH base AS (
    SELECT
        c.ENTITYID AS account_number,
        t.TRANID AS invoice_number,
        t.FOREIGNAMOUNTUNPAID AS invoice_balance,
        CASE
            WHEN DATEDIFF('day', t.DUEDATE, CURRENT_DATE()) < 0 THEN 'Current'
            WHEN DATEDIFF('day', t.DUEDATE, CURRENT_DATE()) BETWEEN 0 AND 30 THEN '0-30'
            WHEN DATEDIFF('day', t.DUEDATE, CURRENT_DATE()) BETWEEN 31 AND 60 THEN '31-60'
            WHEN DATEDIFF('day', t.DUEDATE, CURRENT_DATE()) BETWEEN 61 AND 90 THEN '61-90'
            WHEN DATEDIFF('day', t.DUEDATE, CURRENT_DATE()) BETWEEN 91 AND 120 THEN '91-120'
            WHEN DATEDIFF('day', t.DUEDATE, CURRENT_DATE()) > 120 THEN '120+'
        END AS aging_bucket
    FROM FIVETRAN_DB.NETSUITE_SUITE.TRANSACTION t
    JOIN FIVETRAN_DB.NETSUITE_SUITE.CUSTOMER c ON t.ENTITY = c.ID
    WHERE t.TYPE = 'CustInvc'
      AND t._FIVETRAN_DELETED = false
      AND t.FOREIGNAMOUNTUNPAID > 0
)
SELECT
    COUNT(*) AS invoice_rows,
    SUM(invoice_balance) AS total_balance
FROM base
WHERE aging_bucket IN ('31-60', '61-90', '91-120', '120+');
```

Optional fingerprint (catch duplicate/join inflation):

```sql
SELECT COUNT(*) AS rows, COUNT(DISTINCT invoice_number) AS distinct_invoices
FROM ( /* same base CTE */ );
```

`rows` must equal `distinct_invoices` unless product intentionally allows duplicates.

## Production export columns (pre–`22oct/feature_ar_updates`)

| Col | Letter | Header | Baseline behavior (screenshot) |
|-----|--------|--------|--------------------------------|
| 1 | A | Account # | NetSuite `ENTITYID` |
| 2 | B | Customer | Company name |
| 3–12 | C–L | Invoice / aging / notes | From transaction + `notes_data.json` |
| 13 | M | Account Owner | SFDC `OWNER_NAME` (name match on company) |
| 14 | N | **CSM** | SFDC CSM name — **removed on branch** |
| 15 | O | CSM Manager | SFDC |
| 16 | P | AE | SFDC |
| 17 | Q | CAM | SFDC |
| 18 | R | Market Segment | SFDC |
| 19 | S | Subsidiary | **Often raw ID** (`1`, `3`, `7`, `9`, `10`) when map miss or cache empty; wrong labels when map hits (e.g. 7→“Iterable EU”, 9→“Iterable JP”) |
| 20 | T | Category | NetSuite category name when cache loaded |
| 21 | U | Payment Method | Often raw ID (`7`, etc.) if `_PAYMENT_METHOD_MAP` key type mismatch |
| 22 | V | Customer Bank | Often raw ID (`1`) or wrong label (**SVB** vs Finance **CIBC**) |

Invoice-level columns (amount, balance, bucket, dates) in the screenshot align with NetSuite; issues are **enrichment** (S, U, V) and SFDC columns, not duplicate invoice rows.

## Branch delta already on `22oct/feature_ar_updates`

| Change | Back-test |
|--------|-----------|
| Drop **CSM** from CSV/XLSX | Column count **22 → 21**; columns after M shift left (N = CSM Manager). Row count and balances unchanged. |

## Planned fixes (see `fix-subsidiary-names.md`)

| Area | Must not change | Should change |
|------|----------------|---------------|
| Subsidiary | Invoice rows, balances | Column S = NetSuite `SUBSIDIARY.NAME` |
| Customer bank | Same | Labels (e.g. CIBC not SVB) |
| Collector | Same until filter ships | New column + optional server filter |
| NetSuite emails / account notes | Same | New read-only columns / UI only |

## Quick export header check (local)

After code changes, hit `/export/csv` with cached data or unit-test the header list in `export_data()`:

- **Production:** 22 columns, includes `CSM` at position 14.
- **Branch:** 21 columns, no `CSM`; `Account Owner` still position 13.
