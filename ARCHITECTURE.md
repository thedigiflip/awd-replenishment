# MAGEASY eCom Data Platform — Architecture

> Status: core principles agreed 2026-10-26. This document is the reference for any new
> feature or new data source. Read it before adding tables, pipelines, or dashboard pages.
>
> Related docs: `README.md` (system overview), `DEPLOYMENT.md` (GCP setup),
> `PROJECT_STATUS_<FEATURE>.md` (per-feature status and decisions).

---

## 1. Scope

The project started as an Amazon replenishment dashboard. It is now a **cross-channel
e-commerce analytics platform**:

| Horizon | Data sources |
|---|---|
| Today | Amazon (catalog, FBA / AWD inventory, sales, orders, traffic, fees) for US / UK / DE |
| Mid-term | Shopify (products, orders, sessions), GA4, ad platforms (Amazon Ads, Meta, Google, TikTok) |

Every new channel or feature must fit the structure below so the platform stays consistent
as it grows.

---

## 2. Core principles

1. **One warehouse.** All data sources land in **one database** (DuckDB today). A new data
   source means a new table in the same database, never a new database file.
2. **Bronze / Silver / Gold layers.**
   - **Bronze** — one raw fact table per source, kept close to the source. Only timezone
     normalization and de-duplication.
   - **Silver** — cleaned, consistently defined views, one set per channel. Supports
     "view each channel separately".
   - **Gold** — cross-source views that join Bronze/Silver. Supports "view channels together".
3. **Python vs SQL split.**
   - **Python**: API authentication, extraction, ETL transforms, and complex business rules
     (e.g. replenishment recommendations, alert tiers).
   - **SQL (DuckDB, later BigQuery)**: joins, aggregations, metric definitions.
     If it can be done in SQL, do it in SQL — do not pull data into Python to join it.
4. **Dashboards read views, never raw Bronze tables.** The API layer queries Silver/Gold views.
5. **One source of truth for definitions.** Exchange rates, correction factors, metric
   definitions (e.g. MER) and thresholds live in `dim_params` or in a Silver view.
   Change once, every dashboard updates.
6. **Child ASIN is the product key.** Internal SKUs are internal codes and do not reliably
   match marketplace SKUs. All product-level joins use Child ASIN, sourced from the
   MAGEASY Anchor catalog. Non-Amazon channels (e.g. Shopify) map their products to the
   Anchor ASIN.

---

## 3. Naming conventions

| Object | Pattern | Example |
|---|---|---|
| Bronze fact table | `fact_<source>_<grain>_daily` | `fact_amazon_fba_inventory_daily` |
| Dimension table | `dim_<entity>` | `dim_product` (from Anchor), `dim_params` |
| Silver view | `vw_silver_<channel>_<subject>` | `vw_silver_amazon_fba_by_asin` |
| Gold view | `vw_gold_<subject>` | `vw_gold_replenishment_inputs` |

Existing tables predate these conventions (see §7). Rename only as part of a planned
migration, never ad hoc.

---

## 4. Adding a new data source (4 steps)

1. **Extractor (Python)** — authentication, fetch, light cleaning. Retry/backoff via `tenacity`.
2. **Bronze table** — write raw rows into `fact_<source>_<grain>_daily` in the shared warehouse.
3. **Silver view** — make definitions consistent (currency, timezone, product key = Child ASIN).
4. **Gold view** (only if cross-source) — join Bronze/Silver for combined metrics.

Then expose the view through an API endpoint and build the dashboard page on top of it.

---

## 5. Anti-patterns (do not do)

- Multiple database files joined together in Python.
- Dashboards or API endpoints reading raw Bronze tables directly.
- Metric definitions scattered across several Python scripts instead of one SQL view.
- Data from Shopify / GA4 / ad platforms kept only in Python memory — it must be written
  to the warehouse.
- Joining products on internal SKU instead of Child ASIN.

---

## 6. Reconciliation and validation

Silent errors are the main risk of a data platform. Required checks:

- **Weekly reconciliation**: pick 3–5 core metrics (e.g. total spend, total revenue,
  account-level MER, key ROAS) and compare three ways — platform UI vs API pull vs dashboard.
  Tolerance 2%; anything above must be investigated.
- **Quarterly**: recalibrate any GA4 coverage correction factor (iOS / Consent Mode changes
  over time).
- **Exchange rates** are not hard-coded; `dim_params` is refreshed weekly from an official source.
- **Before deploying calculation changes**: snapshot the current output per product and
  compare field by field against the new output on a copy of production data. Every change
  must be explained (this is how the 2026-10 Child-ASIN migration was verified).

---

## 7. Current state vs target (gap)

The **Replenishment** feature was built before these principles were agreed.

| Layer | Target | Today |
|---|---|---|
| Bronze | `fact_*` tables per source | Tables exist with older names: `inventory`, `awd_inventory`, `sales_summary`, `orders`, `order_items`, `sales_traffic`, `sz_warehouse` |
| Dimensions | `dim_product`, `dim_params` | `product_catalog` (Anchor), `asin_config`, `replenishment_controls` play this role |
| Silver | Per-channel views | **Not yet.** Aggregations (FBA by ASIN, AWD by ASIN, sales by ASIN, SZ by ASIN) are CTEs inside one large query in `services/replenishment_service.py` |
| Gold | Cross-source views | **Not yet.** The same query joins everything for the dashboard |
| Business rules | Python | ✅ Already in Python (`ReplenishmentService._compute`) |
| Product key | Child ASIN | ✅ Done (2026-10-20 migration) |

What already complies: single DuckDB warehouse, Child ASIN as key, business rules in Python,
dashboard reads through the API layer.

### Planned migration (incremental, no big-bang rewrite)

1. When building **Traffic** (the next feature), extract the shared aggregations from
   `replenishment_service.py` into Silver views, e.g. `vw_silver_amazon_fba_by_asin`,
   `vw_silver_amazon_awd_by_asin`, `vw_silver_amazon_sales_by_asin`. Replenishment and
   Traffic then read the same views instead of duplicating SQL.
2. Introduce `dim_product` (view over `product_catalog`, one row per Child ASIN) and
   `dim_params` (currency, thresholds).
3. Point the replenishment query at the Silver views — business rules in `_compute`
   stay unchanged.
4. Verify with the per-ASIN snapshot comparison (§6) before deploying.
5. Rename Bronze tables to `fact_*` only if/when moving to BigQuery, to avoid churn.

---

## 8. Technology roadmap

| Period | Choice |
|---|---|
| Now – 2027-01 | DuckDB single warehouse; all new sources written into it |
| From 2027-02 (when triggered) | Add **BigQuery** via dual-write (ETL writes to both); DuckDB becomes a local sandbox |

**Move to BigQuery when any of these is true:**
- A combined Taiwan + US cross-market dashboard is needed
- A second team member needs to query the data directly
- The DuckDB file exceeds 10 GB
- Multiple schedules need concurrent read/write (DuckDB allows a single writer)

---

## 9. Deferred decisions

- Deployment strategy beyond the current single VM (Cloud Run, CI/CD) — revisit as features grow.
- Whether to connect Shopify through Supermetrics or a direct API extractor.
- Detailed schema for the first Bronze/Silver views — to be designed with the Traffic feature.
