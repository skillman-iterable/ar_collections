# AR Aging Collections Report

A FastHTML app that generates a live AR Aging report from NetSuite and Salesforce data via Snowflake. Designed for the Iterable Finance/Collections team.

## Features

### Dashboard
- KPI cards by aging bucket (31-60, 61-90, 91-120, 120+ days) with click-to-filter
- Bar chart of outstanding balances by customer
- Top 10 customers ranked by outstanding balance
- Searchable, sortable, filterable invoice detail table
- Status column with Invoice Sent, Promise to Pay, and Resolution Category badges

### Customer Detail Drawer
- SFDC Account Info: Account Owner, CSM, CSM Manager, AE, CAM, Market Segment (via `analytics_prod.bi_gtm.src_sf_account`)
- NetSuite Details: Subsidiary, Category, Payment Method, Customer Bank
- Bill-to and Ship-to addresses
- SFDC Contacts with email/phone
- Resolution Category dropdown (Legal, Access Suspended, Amendment, etc.)
- Promise to Pay per-invoice checkboxes

### Export
- CSV and Excel (.xlsx) with all enrichment fields
- Excel includes formatted teal headers, currency formatting, auto-width columns
- Background cache warmup ensures exports are instant

### Theming
- Dark theme (default) and Iterable brand theme (Poppins + Spectral typography, teal header, warm cream background)
- Theme toggle with localStorage persistence

### Architecture Diagram
- Available at `/diagram` - interactive architecture diagram showing the full data flow

### Performance
- Invoice data loaded at startup from Snowflake
- Customer detail cached on-demand with background warmup for all accounts
- Hourly auto-refresh of all data (invoices + customer detail cache)
- Manual refresh via header button

## Data Sources

| Source | Table | Purpose |
|---|---|---|
| NetSuite | `FIVETRAN_DB.NETSUITE_SUITE.TRANSACTION` | Invoices (CustInvc, unpaid, 31+ days past due) |
| NetSuite | `FIVETRAN_DB.NETSUITE_SUITE.CUSTOMER` | Customer metadata, addresses |
| NetSuite | `FIVETRAN_DB.NETSUITE_SUITE.CUSTOMERADDRESSBOOKENTITYADDRESS` | Bill-to / Ship-to addresses |
| NetSuite | `FIVETRAN_DB.NETSUITE_SUITE.CUSTOMERCATEGORY` | Customer category names |
| Salesforce | `ANALYTICS_PROD.BI_GTM.SRC_SF_ACCOUNT` | Account owner, CSM, AE, CAM, market segment |
| Salesforce | `FIVETRAN_DB.FT_SALESFORCE.USER` | Resolve CSM/AE user IDs to names |
| Salesforce | `FIVETRAN_DB.FT_SALESFORCE.CONTACT` | Account contacts |

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Requires a Snowflake connection named `pp13258-iterable` in `~/.snowflake/connections.toml`.

## Run Locally

```bash
LIVE_RELOAD=true python3 app.py
# Opens at http://localhost:5099
```

## Deploy

Deployed to Google Cloud Run via GitHub Actions CI/CD:
- Push to `main` triggers the deploy workflow
- Uses Workload Identity Federation for GCP auth
- Builds Docker image, pushes to Artifact Registry, deploys to Cloud Run
- RSA key-pair auth to Snowflake in production (no browser OAuth)

```
Project: udi-uat-504519
Region: us-central1
Service: ar-collections
```

## File Structure

```
app.py              # Main application (~1900 lines, single-file FastHTML app)
requirements.txt    # Python dependencies (fasthtml, snowflake-connector, openpyxl)
favicon.ico         # Iterable favicon
notes_data.json     # Persisted notes, resolution categories, promise-to-pay flags
Dockerfile          # Cloud Run container
.github/workflows/  # CI/CD deploy pipeline
.keys/              # RSA key for Snowflake auth in Cloud Run
```
