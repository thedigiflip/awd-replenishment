# AWD Replenishment Dashboard

A self-hosted inventory replenishment system for Amazon sellers managing both **FBA (Fulfillment by Amazon)** and **AWD (Amazon Warehousing & Distribution)**.

Built with FastAPI, DuckDB, Docker, and n8n — it automatically syncs inventory, sales, and order data from the Amazon SP-API, then calculates replenishment recommendations based on configurable stock level targets.

---

## Features

- **Automatic SP-API sync** — FBA inventory, AWD inventory, sales reports, and orders pulled directly from Amazon
- **Replenishment calculations** — AWD target coverage, sea shipment quantity, air freight alerts, 3-month cap warnings
- **MAGEASY Anchor as master SKU list** — all SKUs from your product catalog are always visible, even when out of stock
- **Stock alert system** — 🔴 fully out of stock / 🟠 no available units / 🟡 FBA out of stock
- **Daily email notifications** — n8n workflow sends alerts for stock issues and unmatched SKUs
- **Configurable water level parameters** — adjust AWD targets, SZ reorder levels, and cap limits directly from the dashboard without restarting
- **SZ warehouse integration** — upload Shenzhen warehouse stock to calculate sea shipment recommendations
- **Web dashboard** — filterable table with 29 columns, CSV export, SKU config editor
- **Fully containerized** — runs locally with Docker Compose, portable across machines

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| API & ETL | Python 3.11, FastAPI |
| Database | DuckDB (embedded, no server needed) |
| Automation | n8n (self-hosted) |
| Container | Docker, Docker Compose |
| Amazon API | SP-API via `python-amazon-sp-api` |

---

## Architecture

```
Amazon SP-API
     │
     ▼
FastAPI ETL Service (port 8000)
  ├── FBA Inventory Sync
  ├── AWD Inventory Sync
  ├── Sales Reports Sync (Reports API)
  ├── Orders Sync
  └── Replenishment Calculator
     │
     ▼
DuckDB (local file)
  ├── inventory
  ├── awd_inventory
  ├── sales_summary
  ├── orders / order_items
  ├── product_catalog  ← master SKU list
  ├── sz_warehouse
  ├── sku_config
  └── replenishment_controls

n8n (port 5678)
  └── Daily workflow: sync all → check alerts → send email
```

---

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- Amazon Seller Central account with SP-API access
- SP-API app credentials (Client ID, Client Secret, Refresh Token)

---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/thedigiflip/awd-replenishment.git
cd awd-replenishment
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Open `.env` and fill in your credentials:

```env
SP_API_CLIENT_ID=your_client_id
SP_API_CLIENT_SECRET=your_client_secret
SP_API_REFRESH_TOKEN=your_refresh_token
SP_API_MARKETPLACE_IDS=ATVPDKIKX0DER
```

### 3. Start the containers

```bash
docker compose up -d
```

### 4. Open the dashboard

```
http://localhost:8000/dashboard
```

### 5. Open n8n (automation)

```
http://localhost:5678
```

Import `n8n/awd_replenishment_workflow.json` and configure your Gmail credentials.

---

## Dashboard Overview

| Section | Description |
|---------|-------------|
| 📋 MAGEASY Anchor upload | Upload your product catalog CSV/Excel to set the master SKU list |
| 🏪 SZ Warehouse upload | Upload Shenzhen warehouse stock for sea shipment calculations |
| 🏭 AWD Sync | One-click sync from SP-API |
| ⚙️ SKU Settings | Set product type (流量款/一般款), unit per case, ETA per SKU |
| 🎛️ Water Level Settings | Adjust AWD targets, SZ reorder levels, and cap limits live |

---

## Replenishment Logic

| Column | Formula |
|--------|---------|
| FBA Available | `fulfillable + reserved_fc_transfers + reserved_fc_processing` |
| FBA Inbound | `inbound_working + inbound_shipped + inbound_receiving` |
| FBA Total | `FBA Available + FBA Inbound` |
| AWD Total | `awd_available + awd_inbound - awd_outbound` |
| Total Coverage | `(FBA Total + AWD Total) ÷ 30-day sales` |
| AWD Replen Qty | `AWD target × sales - AWD Total` (capped at 3M cap) |
| Sea Shipment Qty | `min(AWD Replen, SZ Available)` rounded to full cases |
| Air Alert | Triggered when Total Coverage < `(production days + sea days) ÷ 30` |

### Default Controls

| Parameter | Default | Description |
|-----------|---------|-------------|
| AWD Target (Normal) | 1.0 months | Standard SKUs |
| AWD Target (Hero) | 1.5 months | High-velocity SKUs |
| FBA+AWD Cap | 3.0 months | Maximum total coverage |
| SZ Reorder Level (Normal) | 1.0 months | Keep this much in SZ warehouse |
| SZ Reorder Level (Hero) | 1.5 months | Keep this much in SZ warehouse |
| Production Days | 30 days | Used for air freight alert threshold |
| Sea Days | 40 days | Used for air freight alert threshold |

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/dashboard` | Web dashboard |
| `POST` | `/etl/inventory/sync` | Sync FBA inventory |
| `POST` | `/etl/awd/sync` | Sync AWD inventory |
| `POST` | `/etl/sales/sync` | Sync sales reports |
| `POST` | `/etl/orders/sync` | Sync orders |
| `GET` | `/replenishment` | Full replenishment table (JSON) |
| `GET` | `/replenishment/export` | Download as CSV |
| `GET` | `/replenishment/daily-alert` | Stock alerts for n8n |
| `GET` | `/replenishment/unmatched-skus` | SKUs in FBA/AWD but not in Anchor |
| `GET/POST` | `/replenishment/controls` | View/update water level parameters |
| `POST` | `/replenishment/sku-config` | Batch update SKU settings |
| `GET` | `/docs` | Interactive API documentation (Swagger) |

---

## n8n Automation

The included workflow (`n8n/awd_replenishment_workflow.json`) runs daily and:

1. Syncs FBA inventory
2. Syncs AWD inventory
3. Syncs orders
4. Requests sales report (waits ~10 min for Amazon to generate)
5. Fetches daily alerts
6. Sends an HTML email if there are stock issues or unmatched SKUs

To import: n8n → Workflows → Import from file → select `awd_replenishment_workflow.json`

---

## Moving to Another Machine

Since all data is in the local DuckDB file, when switching machines:

1. Clone the repo and create `.env`
2. Run `docker compose up -d`
3. Trigger a fresh sync from the dashboard or n8n
4. Re-upload your MAGEASY Anchor CSV
5. Re-run SKU config batch if needed

---

## License

Private — all rights reserved.
