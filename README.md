# MAGEASY Replenishment & Advertising Dashboard

Multi-marketplace Amazon FBA inventory, sales, and advertising management system supporting US / UK / DE with automated daily sync, inventory analytics, replenishment recommendations, and advertising performance tracking.

---

## 3-Minute Brief (for Claude and new engineers)

**What this system does:** Automatically pulls data from Amazon SP-API daily (inventory, sales, orders, fees, ads), writes to local DuckDB, and serves it via FastAPI to a single-page HTML dashboard. Combines the Anchor product master, Shenzhen warehouse (factory-side stock), and Amazon FBA/AWD inventory to compute per-SKU replenishment recommendations (transfer quantity, reorder quantity, AWD replenishment).

**Architecture:** n8n (scheduler) → FastAPI (ETL endpoints) → DuckDB (storage) → static HTML dashboard
**Run:** `docker compose up -d`; services on port 8000 (etl) and 5678 (n8n)
**Multi-marketplace:** One codebase serves 3 sites, per-site data isolation, shared product catalog & factory warehouse

---

## System Architecture

### Service Containers (`docker-compose.yml`)

| Container | Port | Description |
|---|---|---|
| `sp_etl` | 8000 | FastAPI main service (`etl/main.py`) + static dashboard |
| `sp_n8n` | 5678 | n8n scheduler for automated workflows |

### Data Layer

**DuckDB** (`/data/sp_api.duckdb`) — primary business data store

| Table | Purpose | Marketplace Scope |
|---|---|---|
| `inventory` | Amazon FBA inventory snapshots | Per-site (`PRIMARY KEY (snapshot_date, asin, sku, marketplace_id)`) |
| `awd_inventory` | Amazon Warehouse & Distribution stock | **US only** (no marketplace column) |
| `sales_summary` | Daily sales aggregates | Per-site (`PRIMARY KEY (report_date, sku, marketplace_id)`) |
| `orders` / `order_items` | Orders and line items | Per-site |
| `sales_traffic` | Session, Buy Box, CVR monthly reports | Per-site |
| `ads_sponsored_products` | Sponsored Products ad data | Currently US only |
| `sz_warehouse` | Shenzhen warehouse (factory-side stock) | Shared across sites |
| `product_catalog` | MAGEASY Anchor master (SKU→ASIN) | Shared across sites |
| `sku_config` | Product type & case-pack settings | Shared |
| `replenishment_controls` | Dynamic replenishment thresholds | Shared |
| `sync_anomalies` | Data drift detection log | Per-site |

**SQLite** (`/data/sp_api_meta.db`) — pipeline execution log (run_id / status / error)

---

## Multi-marketplace Design (US / UK / DE)

**Configuration in `.env`:**
```
SP_API_MARKETPLACE_IDS=["ATVPDKIKX0DER","A1F83G8C2ARO7P","A1PA6795UKMFR9"]
SP_API_REFRESH_TOKEN=Atzr|...       # NA region (US/CA/MX)
SP_API_REFRESH_TOKEN_EU=Atzr|...    # EU region (UK/DE/FR/IT/ES)
SP_API_REFRESH_TOKEN_FE=            # FE region (JP/AU/SG), unused
```

Marketplace IDs:
- US =  (NA region)
- UK =  (EU region)
- DE =  (EU region)

### Data Isolation Matrix

| Data Type | US | UK | DE | Isolation Method |
|---|---|---|---|---|
| FBA Inventory | Independent | Independent | Independent | `inventory.marketplace_id` |
| AWD Inventory | Shown | 0 | 0 | AWD is US-only service |
| Sales | Independent | Independent | Independent | Split by `currency` (USD/GBP/EUR) |
| Orders | Independent | Independent | Independent | `orders.marketplace_id` |
| Shenzhen / JL warehouse | Shared | Shared | Shared | Factory-side stock |
| Product catalog (Anchor) | Shared | Shared | Shared | One master file |
| Fees / Traffic | Independent | Independent | Independent | Router loops all sites |

### Key Architectural Decisions

1. **`inventory` PK includes `marketplace_id`** — prevents SP-API loop from overwriting rows across sites (same ASIN+SKU on same date)
2. **Sales report is region-level** (Amazon account-level): one EU API call returns all EU sites' data → split into UK / DE using the `currency` field
3. **AWD is US-only service**: replenishment SQL guards with `WHERE ? = 'ATVPDKIKX0DER'`, forcing AWD values to 0 on non-US dashboards
4. **Fees / Traffic router without marketplace_id → loops all sites**; with marketplace_id → only that site (compatible with single-site calls)

---

## Core Business Logic

### Replenishment Computation (`services/replenishment_service.py`)

**Key = ASIN** (not SKU) because a single ASIN may map to multiple SKUs (e.g., version changes).

```
Monthly Sales (H) = Sales 30 days
FBA Total (J) = FBA Available + FBA Inbound
                     ↑ fulfillable + reserved_fc_transfers + reserved_fc_processing
                       + inbound_working + inbound_shipped + inbound_receiving
FBA Month (K) = FBA Total ÷ Monthly Sales

AWD Total (O) = AWD Available + AWD Inbound
                  ↑ Outbound is NOT subtracted. In Amazon's data model,
                    outbound units are already removed from awd_available
                    and automatically appear in FBA inbound_shipped.
                    Subtracting them again would double-count the deduction.
                    (awd_outbound is kept as an info column for tracking
                     "AWD → FBA in-transit" quantities.)
AWD Month (P) = AWD Total ÷ Monthly Sales

Total Coverage (Q) = (FBA Total + AWD Total) ÷ Monthly Sales

US virtual stock = us_qty (on-hand) + pending_qty (ordered, not received)
US virtual Month = US virtual stock ÷ Monthly Sales

safety_stock = Monthly Sales × transfer_level (by product type)
    - Normal:  sz_transfer_level_normal
    - Hero:    sz_transfer_level_hero
    - Rocket:  sz_transfer_level_rocket (default 2.5)

us_gap = max(0, safety_stock − US virtual stock)

transfer_qty = min(JL warehouse stock, us_gap)
    Suppress: if Total Coverage ≥ transfer trigger (default 4.0) → transfer_qty = 0

reorder_qty = max(0, us_gap − JL warehouse stock)
    Suppress: if Total Coverage ≥ reorder trigger (default 4.0) → reorder_qty = 0

AWD replenishment:
    need = awd_target × Monthly Sales − AWD Total
    cap  = 3.0 × Monthly Sales − (FBA Total + AWD Total)   -- overall 3-month cap
    awd_replen = min(need, cap)
```

### Three-tier Alert System (priority: 🔴 > 🟡 > 🔵)

- 🔴 **Reorder Needed**: `reorder_qty > 0`
- 🟡 **JL Warehouse Critical**: `transfer_qty > 0 AND (JL − transfer) < safety_stock × 0.5`
- 🔵 **Ordered, Awaiting Receipt**: `pending_qty > 0 AND reorder_qty = 0`

### Product Types (`sku_config.product_type`)

- **Normal** — default replenishment cadence
- **Hero** — high-traffic products, higher stock levels
- **Rocket** 🚀 — top priority, aggressive stocking (2.5 month default)
- **Discontinued** — no reorder / transfer / AWD replenishment triggered

---

## Main API Endpoints

**Inventory (FBA)**
- `POST /etl/inventory/sync` — sync all marketplaces via SP-API
- `POST /etl/inventory/upload?marketplace_id=US` — manual Amazon Manage FBA Inventory CSV upload (also accepts form field)
- `POST /etl/inventory/manual-adjust` — single SKU manual correction
- `POST /etl/inventory/remap-marketplace` — move rows to correct marketplace
- `GET  /etl/inventory/debug/{asin_or_sku}` — diagnostic query

**AWD** (US only)
- `POST /etl/awd/sync` — API sync
- `POST /etl/awd/upload` — manual AWD Excel upload
- `POST /etl/awd/report-sync` — trigger SP-API AWD Report (authoritative) + reconcile

**Sales**
- `POST /etl/sales/sync` — no `marketplace_id` → loops all regions
- `GET  /etl/sales/marketplace-breakdown` — diagnostic: per-site distribution
- `POST /etl/sales/purge?marketplace_id=X&since_days=90` — purge site data

**Orders / Fees / Traffic / Ads** — all support loop-all mode

**Replenishment**
- `GET  /replenishment?marketplace_id=US` — dashboard main data
- `GET  /replenishment/daily-alert` — daily email alert data (called by n8n)

**Health**
- `GET  /etl/health/summary?marketplace_id=US` — per-pipeline status
- `GET  /etl/health/anomalies?marketplace_id=US` — recent data anomalies

**SZ Warehouse**
- `POST /etl/sz/upload?replace=true` — upload Shenzhen warehouse template
- `GET  /etl/sz/download-template` — download blank template

**Catalog (Anchor)**
- `POST /etl/catalog/upload` — upload MAGEASY Anchor xlsx

---

## Dashboard (`etl/static/dashboard.html`)

Single HTML file served at `http://localhost:8000/dashboard`.

**Main panels:**
1. **Marketplace filter dropdown** (🇺🇸 US / 🇬🇧 UK / 🇩🇪 DE) — switching triggers `loadData` + `loadHealthPanels`
2. **Upload tools** — Anchor / SZ / AWD / FBA CSV
   - FBA upload has **independent red-bordered marketplace selector** (decoupled from filter)
   - Every upload prompts a confirmation dialog showing target site
3. **Sync health panel** — freshness, coverage, anomalies per pipeline
4. **Anomaly panel** — past 24h `sync_anomalies` events
5. **Replenishment master table** — one row per SKU with reorder / transfer / AWD suggestions and alerts

**Controls (floating gear button)** — adjust replenishment thresholds; persists to `replenishment_controls` table.

---

## n8n Workflows (`n8n/*.json`)

| Workflow | Cron | Purpose |
|---|---|---|
| `awd_replenishment_workflow.json` | `0 10 * * *` (10:00 UTC = 18:00 Taiwan) | Main ETL: FBA + AWD + Orders + Sales + Alert |
| `workflow_orders.json` | Every 4 hours | Orders sync |
| `workflow_finance.json` | `0 23 * * *` (23:00 UTC) | Finance sync |
| `workflow_ads.json` | `0 0 * * *` | Ads sync |
| `workflow_fees.json` | `0 4 2 * *` | Monthly Fees (2nd of month) |
| `workflow_traffic.json` | `0 3 2 * *` | Monthly Traffic (2nd of month) |
| `workflow_traffic_mtd.json` | `0 9 * * *` | Daily month-to-date Traffic |
| `workflow_awd_report.json` | `0 3 * * *` | AWD authoritative report reconcile |

**⚠️ Important:** Most n8n workflow HTTP nodes should NOT hard-code `marketplace_id` (unless explicitly single-site). The backend auto-loops all sites.

**Amazon 24h Rate Limit:** Each (report type × marketplace) can only succeed once per 24-hour window. If today at 10:00 UTC succeeded, next successful run must wait until after 10:00 UTC tomorrow.

---

## Environment Variables (`.env`)

```
# SP-API
SP_API_REFRESH_TOKEN=...              # NA region
SP_API_REFRESH_TOKEN_EU=...           # EU region
SP_API_REFRESH_TOKEN_FE=              # FE region (unused)
SP_API_CLIENT_ID=amzn1.application-oa2-client.<your_client_id>
SP_API_CLIENT_SECRET=amzn1.oa2-cs.v1.<your_client_secret>
SP_API_MARKETPLACE_IDS=["ATVPDKIKX0DER","A1F83G8C2ARO7P","A1PA6795UKMFR9"]

# Ads API
ADS_CLIENT_ID=...
ADS_CLIENT_SECRET=...
ADS_REFRESH_TOKEN=...
ADS_PROFILE_ID=...

# DuckDB
DUCKDB_PATH=/data/sp_api.duckdb

# n8n
N8N_USER=...
N8N_PASSWORD=...
```

**⚠️ Never push `.env` to GitHub.** Use Google Secret Manager for GCP deployment.

---

## Common Operations

**Start / Stop:**
```bash
docker compose up -d              # Start
docker compose down               # Stop (data preserved)
docker compose restart etl        # Restart API (does NOT reload .env)
docker compose down && docker compose up -d   # Full restart (reloads .env)
```

**View logs:**
```bash
docker compose logs -f etl
docker compose logs --tail 100 etl | grep -i error
```

**Read-only DB query (avoids FastAPI lock):**
```bash
docker compose exec etl python3 -c "
import duckdb
c = duckdb.connect('/data/sp_api.duckdb', read_only=True)
# ...
"
```

**Backup DB:** Copy `sp_api.duckdb` file directly (stop API before copying).

---

## Deployment to GCP

**Checklist:**
1. Compute Engine (recommended over Cloud Run — DuckDB needs persistent volume)
2. Move `.env` to Google Secret Manager
3. Mount Persistent Disk at `/data` (critical for DuckDB file)
4. Deploy n8n separately (Docker or GCP-hosted)
5. Open outbound firewall to `sellingpartnerapi-*.amazon.com`
6. After deploy: verify DB migrations ran (`docker compose exec etl python3 -c "..."` to check schema)

---

## Architectural Decisions Log

1. **`inventory` PK adds marketplace_id** — prevents SP-API loop from overwriting rows
2. **FBA CSV upload accepts marketplace_id via query + form field** — safety net for accidental site mismatch
3. **Sales split by currency (UK/DE)** — Amazon Sales Report has no sales-channel column
4. **AWD only shows in US site** — `awd_by_asin` CTE has marketplace guard
5. **Health / Anomaly split by marketplace** (P1-2, P1-3, P1-4)
6. **Fees / Traffic router loops all sites** (P1-6, P1-7)
7. **Sales sync region-level + currency split** (P0-7)

---

## Pending Work (P1 remaining)

- **P1-5**: Add `marketplace_id` column to `ads_sponsored_products` (needed before running UK/DE Ads)
- **P1-9**: Add marketplace loop to Ads n8n workflow (pair with P1-5)

---

## Data Flow Overview

```
Amazon SP-API ─┐
Amazon Ads API ─┼→ n8n (schedule) → FastAPI (/etl/*/sync) → DuckDB
Manual upload ──┘                                             │
                                                              ▼
                                             FastAPI (/replenishment)
                                                              │
                                                              ▼
                                             Dashboard HTML (single page)
                                                              │
                                                 (user tweaks Controls)
                                                              │
                                                              ▼
                                             replenishment_controls table
                                                → feeds next computation
```

---

## Repository Structure

```
etl/
  main.py                    # FastAPI app
  core/
    config.py                # Pydantic Settings (region_for, refresh_token_for)
    database.py              # DuckDB schema + migrations
    sp_api_client.py         # Region-aware SP-API client
    db_meta.py               # SQLite pipeline runs
  routers/
    inventory.py             # FBA endpoints
    sales.py / orders.py / fees.py / traffic.py / ads.py
    awd_upload.py            # AWD Excel upload
    sz_warehouse.py          # Shenzhen warehouse template
    catalog.py               # MAGEASY Anchor upload
    replenishment.py         # Dashboard main data
    health.py                # Health / anomalies
    controls.py              # Replenishment thresholds
  services/
    inventory_service.py     # FBA sync
    awd_service.py           # AWD sync
    awd_report_service.py    # AWD authoritative report + reconcile
    sales_service.py         # Region-level sync + currency split
    orders_service.py / fees_service.py / traffic_service.py / ads_service.py
    replenishment_service.py # Main computation (big CTE query)
    anomaly_detector.py      # Data drift detection
    base_service.py          # BaseService (start_run / finish_run / fail_run)
  static/
    dashboard.html           # Single-page dashboard

n8n/                         # n8n workflow JSON files
.env                         # Environment variables (never commit)
docker-compose.yml
```
