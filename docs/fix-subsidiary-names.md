# Fix: Subsidiary names wrong in drawer + export

## Problem
End users report the Subsidiary field shows **"Iterable EU"**; it should be **"Iterable Ireland"**.

Root cause: `app.py` uses a hardcoded `_SUBSIDIARY_MAP` (~line 168) that is out of sync with NetSuite. It feeds both the Customer Detail drawer and the CSV/Excel export.

| ID | App currently shows | NetSuite `SUBSIDIARY.NAME` |
|---|---|---|
| 1 | Iterable, Inc. | Iterable, Inc. |
| 3 | Iterable UK | ITERABLE LTD |
| 7 | **Iterable EU** | **Iterable Ireland** |
| 9 | **Iterable JP** (wrong entity) | **Iterable Portugal** |
| 10 | Iterable AU | Iterable Australia |

Verified against `FIVETRAN_DB.NETSUITE_SUITE.SUBSIDIARY` on 2026-10-22.

## Change
Source subsidiary names from NetSuite instead of the hardcoded map.

### 1. Add a join in both customer queries
In `_query_single_customer` and `_query_all_customer_details`, add:

```sql
LEFT JOIN FIVETRAN_DB.NETSUITE_SUITE.SUBSIDIARY s
    ON c.SUBSIDIARY = s.ID AND s._FIVETRAN_DELETED = false
```

and add to the SELECT list:

```sql
s.NAME AS subsidiary_name,
```

Keep `c.SUBSIDIARY AS subsidiary_id` as-is.

### 2. Replace the map lookup
In both functions, replace:

```python
detail['SUBSIDIARY'] = _SUBSIDIARY_MAP.get(detail.get('SUBSIDIARY_ID'), str(detail.get('SUBSIDIARY_ID') or '—'))
```

with:

```python
detail['SUBSIDIARY'] = detail.get('SUBSIDIARY_NAME') or str(detail.get('SUBSIDIARY_ID') or '—')
```

### 3. Remove `_SUBSIDIARY_MAP`
Delete the `_SUBSIDIARY_MAP` constant (no other references).

## Verify
- Open the drawer for a customer with subsidiary 7 → shows "Iterable Ireland".
- A subsidiary 9 customer → "Iterable Portugal" (not "Iterable JP").
- Export CSV and XLSX → Subsidiary column shows the NetSuite names.
- No "Iterable EU" / "Iterable JP" anywhere in the output.

---

# Fix: Customer Bank shows "SVB" (should be CIBC)

## Problem
End users report Customer Bank shows **SVB**, which is incorrect. Per Finance:
- Iterable, Inc. customers bank with **CIBC**
- Iterable Ireland customers bank with **JP Morgan**

Root cause: hardcoded `_BANK_MAP` (~line 170) = `{1: 'SVB', 2: 'HSBC', 3: 'JP Morgan'}`.

`CUSTOMER.CUSTENTITY_ITER_CUSTOMER_BANK` distribution (2026-10-22):

| Subsidiary | Bank ID | Customers | Current label | Correct label |
|---|---|---|---|---|
| 1 Iterable, Inc. | 1 | 1,106 | SVB | **CIBC** |
| 1 Iterable, Inc. | 2 | 25 | HSBC | **TBD – confirm with Finance** |
| 7 Iterable Ireland | 3 | 118 | JP Morgan | JP Morgan (no change) |

## Why not join like Subsidiary?
The bank field is a NetSuite custom list, but the custom list table is **not synced by Fivetran** into `FIVETRAN_DB.NETSUITE_SUITE` (only the integer ID exists on CUSTOMER). So names can't be looked up in SQL today.

## Change
Update `_BANK_MAP` in `app.py`:

```python
_BANK_MAP = {1: 'CIBC', 2: 'HSBC', 3: 'JP Morgan'}  # TODO: confirm ID 2 label with Finance
```

No other code changes; the lookup is already used in both `_query_single_customer` and `_query_all_customer_details`, so drawer and export both pick it up.

## Long-term (optional)
Ask the Fivetran/NetSuite admin to sync the customer-bank custom list. Then join it like `SUBSIDIARY` and delete `_BANK_MAP`.

## Verify
- Iterable, Inc. customer with bank ID 1 → "CIBC" in drawer and CSV/XLSX export.
- Iterable Ireland customer → "JP Morgan".
- No "SVB" anywhere in output.

---

# Add: Collector field (drawer + export)

## Request
Collections team sees a **Collector** on the NetSuite customer screen (Jenina / Janda). Show it in the app.

## Source (found + back-tested 2026-10-22)
- Field: `FIVETRAN_DB.NETSUITE_SUITE.CUSTOMER.CUSTENTITY14` (Fivetran exposes it with a generic name; it holds the collector's NetSuite employee ID).
- Name lookup: `FIVETRAN_DB.NETSUITE_SUITE.ENTITY.ENTITYID` (joined on `ENTITY.ID`). Note: `NETSUITE_SUITE.EMPLOYEE` is **not accessible**, so use `ENTITY`.

| Employee ID | Name |
|---|---|
| 350835 | Jenina Dela Fuente |
| 173672 | Janda Wartchow |

Back-test, customers with open invoices (same filter as the app):

| Subsidiary | Jenina | Janda | No collector |
|---|---|---|---|
| 1 Iterable, Inc. | 361 | 229 | 1 (Okta, #2447) |
| 7 Iterable Ireland | 26 | 28 | 0 |

No other values appear in `CUSTENTITY14` for these customers, so it's a clean match.

## Change
### 1. Add to both customer queries
In `_query_single_customer` and `_query_all_customer_details`, add:

```sql
LEFT JOIN FIVETRAN_DB.NETSUITE_SUITE.ENTITY col
    ON c.CUSTENTITY14 = col.ID
```

and in the SELECT list:

```sql
col.ENTITYID AS collector,
```

### 2. Show it
- Customer Detail drawer: add a **Collector** row in the NetSuite Details section (next to Subsidiary / Category / Payment Method / Customer Bank). Show `—` when empty.
- CSV + XLSX export: add a **Collector** column alongside the other NetSuite detail columns.

### 3. Filter
See **"Add: Collector filter (whole app)"** below.

## Verify (spot-check against the NetSuite customer screen)
| Acct # | Customer | Expected Collector |
|---|---|---|
| 745 | JUUL Labs, Inc. | Janda Wartchow |
| 746 | Urban Massage | Jenina Dela Fuente |
| 1924 | Think Money | Janda Wartchow |
| 1930 | SONIDORICO APPS LIMITED | Jenina Dela Fuente |
| 2447 | Okta, Inc. | — (blank in NetSuite) |

---

# Add: NetSuite Account Notes in the Notes dialog

## Request
Show the NetSuite customer's account notes inside the existing **Collection Notes** dialog (the notes icon on each invoice row).

## Source (found 2026-10-22)
- Field: `FIVETRAN_DB.NETSUITE_SUITE.CUSTOMER.COMMENTS` (the "Comments" field on the NetSuite customer record).
- Free text, newest entry first, collector-initialed (e.g. `9/9/26 JW - Customer is making monthly payment on INV61025 ...`). Entries are separated by spaces or `****`, not reliably by newlines.
- Coverage: **451 of 645** customers with open invoices have notes.
- Not used: NetSuite *User Notes* (Communication subtab) are **not synced** by Fivetran. `SYSTEMNOTE` is the field-change audit log, not notes.

## Change
### 1. Query
Add `c.COMMENTS AS account_notes` to the SELECT in `_query_single_customer` and `_query_all_customer_details`. It's already on `CUSTOMER c`, so no join is needed.

### 2. Notes dialog
Notes are **per account**, and the dialog is **per invoice**:
- Add a read-only **"NetSuite Account Notes"** block above the existing textarea. Look it up by the row's account number from `_CUSTOMER_DETAIL_CACHE`.
- Keep the existing editable invoice note (textarea + Save Note → `notes_data.json`) unchanged. **Do not write back to NetSuite.**
- Use `white-space: pre-wrap`, a max height with scroll, and escape the text with `_esc()`.
- If empty, show "No NetSuite account notes".
- Optional: insert a line break before each date pattern (`\d{1,2}/\d{1,2}/\d{2,4} [A-Z]{2,3}\b`) so entries stack.

### 3. (Optional) Export
Add an **Account Notes** column to the CSV/XLSX export.

## Verify
- Open the notes icon for **INV61025 – Rx Marketing Group (#756)**. The NetSuite block should start with "9/9/26 JW - Customer is making monthly payment on INV61025...".
- **#745 JUUL** should start with "1/25/26 JW - CAM provided PO #1040936...".
- Any customer with no notes should show the empty-state message.
- Existing invoice notes still save and load as before.

---

# Add: Resolution column in the invoice table (sortable)

## Current state
- Resolution Category is set per **account** in the drawer (`saveResolution` → `/save-resolution/{acct}`). It's stored in `_NOTES["res_{acct}"]["resolution_category"]`.
- Today it only appears as a small badge inside the **Status** cell (~line 2701), mixed with the Sent/PTP badges. Status isn't sortable.

## Change
1. Add a header `th_sort("Resolution", 9)` between **Bucket** and **Status** (~line 2683). Status and Notes stay non-sortable and shift right.
2. Add a matching cell per row:
   ```python
   res = _NOTES.get(f"res_{acct}", {}).get('resolution_category', '')
   Td(Span(res, cls="resolution-badge") if res else "", data_val=res.lower())
   ```
   Setting `data_val` makes `sortTable()` sort alphabetically. Blank rows should sort last in ascending order: in `sortTable`, treat `''` as greater than any value.
3. Remove the resolution badge from the **Status** cell (~line 2701) so it isn't shown twice.
4. **Live update:** after `saveResolution()` succeeds, update the Resolution cell (text + `data-val`) on every row with that account number, so a page reload isn't needed. Add `data-acct` to each `<tr>` to make this easy.
5. Check any code that indexes columns by position (`sortTable` uses `td[colIdx]`; also check `test_table_scroll.py` and CSS `nth-child` rules) still lines up after inserting the column.

## Verify
- Set a resolution in the drawer → the table cell updates without a reload.
- Click the Resolution header → sorts A→Z with blanks last, then Z→A.
- Status shows only Sent / PTP.

---

# Add: Collector filter (whole app)

## Requirement
A **Collector** filter (All / Jenina Dela Fuente / Janda Wartchow) that scopes the **entire app**: KPI cards, bucket bar chart, Top 10, invoice table, header totals and exports. Not just the table.

## Why server-side
KPI cards, the bar chart, Top 10 and header totals are computed in Python in `get()` (~lines 2449-2493). A JS-only table filter would leave them showing totals for everyone. So the filter must be applied to `data` **before** those calculations.

## Change
### 1. Add collector to the invoice query
In `_query_snowflake` (not only the detail queries), so collector is available at first load, before the detail cache warms:
```sql
LEFT JOIN FIVETRAN_DB.NETSUITE_SUITE.ENTITY col ON c.CUSTENTITY14 = col.ID
-- SELECT:
col.ENTITYID AS collector,
```
(Field source and back-test: see the "Collector field" section above.)

### 2. Filter in `get()`
- Read `collector = request.query_params.get("collector", "")`.
- If set: `data = [r for r in data if r.get('COLLECTOR') == collector]`, right after `data = _CACHED_DATA or []`, so every downstream number uses the filtered set.
- Build the dropdown options from distinct non-null `COLLECTOR` values in `_CACHED_DATA` (don't hardcode the names). Include "All Collectors" (value `""`) and "Unassigned" (rows with no collector, currently 1 account).

### 3. UI
- Add a `Select(id="collector-filter")` to the filter bar next to `bucket-filter` (~line 2650). Also consider placing it in the hero header, since it scopes the whole page.
- `onchange` → navigate to `/?collector=<value>`, keeping other params. Full reload is fine since data is cached in memory.
- Remember the choice: save it to `localStorage('ar-collector')`. On load, if there's no `collector` param but a saved value exists, redirect to `/?collector=<saved>`.
- Show the active collector in the hero meta (e.g. "Collector: Jenina Dela Fuente").

### 4. Export
- `updateExportLinks()` (~line 1282) currently only appends `include_current`. Rebuild the query string so it carries **both** `include_current` and `collector`.
- In `/export/{fmt}` (~line 2985), apply the same `COLLECTOR` filter before writing rows. Also add a **Collector** column.

### 5. Keep existing behavior
The bucket filter, search, Current AR toggle and sorting still work within the collector-filtered data.

## Verify
- Select **Jenina** → KPI totals, chart, Top 10 and table all drop to her accounts only (~387 accounts with open invoices). The table shows no Janda accounts.
- Select **Janda** → ~257 accounts.
- **All** → totals match today's numbers exactly.
- Jenina + 61-90 bucket + search all combine correctly.
- Export while Jenina is selected → the file only contains Jenina's rows and has a Collector column.
- Reload the page → the collector selection persists.

---

# Change: Drawer shows NetSuite emails + addresses; collapse SFDC contacts/addresses

## Request
In the Customer Detail drawer:
- Show the NetSuite contact emails (**Email, Addl Email, Sold To Email**) and NetSuite **Bill To / Ship To** addresses up front.
- Collapse the SFDC contacts (currently a long list, e.g. 74 for Rx Marketing) and SFDC address behind a **"Show SFDC contacts & address"** link that expands when clicked.

## Note on current addresses
The Bill To / Ship To cards in the drawer today **already come from NetSuite** (`CUSTOMERADDRESSBOOKENTITYADDRESS` via `DEFAULTBILLINGADDRESS` / `DEFAULTSHIPPINGADDRESS`). Keep them visible; just group them with the NetSuite emails. Only the **Contacts** list is SFDC.

## NetSuite email fields (found 2026-10-22, open-invoice customers, n=645)

| Label | Column (`NETSUITE_SUITE.CUSTOMER`) | Populated | Rx Marketing #756 |
|---|---|---|---|
| Email | `EMAIL` | 645 | billing@rxmg.com |
| Addl Email | `CUSTENTITY_EMAIL_OF_RECIPIENTS` (comma-separated list) | 573 | trevor@rxmg.com |
| Sold To Email | `CUSTENTITY8` | 644 | trevor@rxmg.com |

**Labels confirmed** by Finance on 2026-10-22. Fivetran shows some NetSuite fields with generic names (`CUSTENTITY8`), so use the mapping above as-is. Reference values for #745 JUUL:
- Email = `apaccounting@juul.com`
- Sold To Email = `areineck@juul.com`
- Addl Email = `ap@juul.com, justin@juul.com, michelle.lowenberg@juul.com, apinvoicing_us@juul.com, invoices@juul.com`

Not used:
- `ALTEMAIL` is empty for all 645 customers.
- `CUSTENTITY10` holds another email on 95 customers; its label is unknown. Ask Finance whether it's needed.

The invoice-level `TRANSACTION.EMAIL` matches `CUSTOMER.EMAIL` on the invoices sampled, so no per-invoice email is needed.

## Change
### 1. Query
Add to the SELECT in `_query_single_customer` and `_query_all_customer_details`:
```sql
c.EMAIL AS ns_email,
c.CUSTENTITY_EMAIL_OF_RECIPIENTS AS ns_addl_email,
c.CUSTENTITY8 AS ns_sold_to_email,
```
For the optional SFDC address, add to the `SRC_SF_ACCOUNT` subquery and select:
```sql
sa.BILLING_STREET, sa.BILLING_CITY, sa.BILLING_STATE, sa.BILLING_POSTAL_CODE, sa.BILLING_COUNTRY
```
(SFDC has a billing address only; there are no shipping columns in `SRC_SF_ACCOUNT`.) The single-customer SFDC query (~line 113) needs the same columns.

### 2. Drawer layout (`get_customer_detail`, ~line 2168)
Order:
1. NetSuite Details (Subsidiary, Category, Payment Method, Customer Bank, Collector)
2. **NetSuite Contacts** (new, always visible): Email, Sold To Email, Addl Email. Split Addl Email on `,` and show one per line. Make every address a `mailto:` link. Show `—` if empty.
3. **Addresses** (NetSuite Bill To / Ship To cards, unchanged)
4. **SFDC Contacts & Address** (collapsed by default):
   - A link/button: `Show SFDC contacts & address (74)`, with the count of SFDC contacts.
   - Clicking it expands to show the existing SFDC contacts table plus an SFDC Billing Address card. The link text changes to `Hide…`.
   - Reuse the existing `toggleAccordion()` pattern, but start closed.
   - If there's no SFDC match, show "No matching SFDC account" and no link.

### 3. Export
Add **NS Email, NS Addl Email, NS Sold To Email** columns to CSV/XLSX.

## Verify
- #756 Rx Marketing: the drawer shows billing@rxmg.com / trevor@rxmg.com right away, with the NetSuite address cards visible. The 74 SFDC contacts are hidden until the link is clicked.
- #745 JUUL: Addl Email shows 5 addresses, one per line.
- Clicking the link expands and collapses; nothing else in the drawer shifts or breaks.

## Possible improvement (not verified)
`CUSTENTITY12` holds a Salesforce Account ID (#756 → `0011a00000ggtSoAAI`). The app currently matches NetSuite to SFDC by **company name**, which can miss or pick the wrong account. Matching on this ID would be more reliable. Check the match rate before switching.

---

## Still to check
`_PAYMENT_METHOD_MAP` (~line 169) is also hardcoded and has not been verified against NetSuite.
