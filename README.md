# AR Aging Collections Report

FastHTML app that generates a live AR Aging report from NetSuite data in Snowflake.

## Features
- Real-time query against `FIVETRAN_DB.NETSUITE_SUITE.TRANSACTION` + `CUSTOMER`
- KPI cards by aging bucket (31-60, 61-90, 91-120, 120+ days)
- Bar chart visualization of outstanding balances
- Top 10 customers ranked by outstanding balance
- Searchable, filterable invoice detail table
- Refresh button to re-query Snowflake

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Requires a Snowflake connection named `pp13258-iterable` in `~/.snowflake/connections.toml`.

## Run

```bash
python3 app.py
# Opens at http://localhost:5099
```

## Data Source
- **Invoices:** `FIVETRAN_DB.NETSUITE_SUITE.TRANSACTION` (type = `CustInvc`, unpaid balance > 0, 31+ days past due)
- **Customers:** `FIVETRAN_DB.NETSUITE_SUITE.CUSTOMER` (joined via `ENTITY` ID)
