"""
DuckDB connection manager.

DuckDB is embedded (no separate server needed).
We keep a single module-level connection so all services share the same instance.

Thread safety note:
  DuckDB connections are NOT thread-safe. FastAPI with uvicorn uses a single
  async event loop + threadpool. We use duckdb.connect() per-operation for
  write-heavy paths, and a shared read connection for query-only paths.
"""

import asyncio
import duckdb
import structlog
from core.config import settings

log = structlog.get_logger()

_conn: duckdb.DuckDBPyConnection | None = None

# ── DuckDB Write Lock ──────────────────────────────────────────────────────────
# DuckDB 是 single-writer：同一時間只能有一個 process 寫入。
# 所有寫入操作透過 db_write() 集中排隊，避免多個 pipeline 同時觸發時的衝突。
# 讀取（_query）不需要這個 lock，可以任意並發。
_write_lock = asyncio.Lock()


async def db_write(fn) -> any:
    """
    在 DuckDB write lock 保護下，於 thread pool 執行同步寫入函式。

    用法：將原本的
        return await asyncio.to_thread(_write)
    換成
        return await db_write(_write)

    這樣無論同時有多少個 pipeline 跑，寫入都會自動排隊，不會互相衝突。
    """
    async with _write_lock:
        return await asyncio.to_thread(fn)


async def init_db() -> None:
    """Called once at application startup. Creates tables if they don't exist."""
    global _conn
    _conn = duckdb.connect(settings.DUCKDB_PATH)
    log.info("duckdb.connected", path=settings.DUCKDB_PATH)
    _create_schema(_conn)


async def close_db() -> None:
    global _conn
    if _conn:
        _conn.close()
        _conn = None
    log.info("duckdb.closed")


def get_conn() -> duckdb.DuckDBPyConnection:
    """Return the shared read connection (FastAPI dependency)."""
    if _conn is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    return _conn


def new_conn() -> duckdb.DuckDBPyConnection:
    """Open a fresh connection for write operations (thread-safe)."""
    return duckdb.connect(settings.DUCKDB_PATH)


def _create_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """DDL — idempotent table creation."""
    conn.execute("""
        -- ── Orders ──────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS orders (
            amazon_order_id   VARCHAR PRIMARY KEY,
            purchase_date     TIMESTAMPTZ,
            last_updated_date TIMESTAMPTZ,
            order_status      VARCHAR,
            fulfillment_channel VARCHAR,
            sales_channel     VARCHAR,
            order_total_amount DECIMAL(18,4),
            order_total_currency VARCHAR,
            number_of_items_shipped INTEGER,
            number_of_items_unshipped INTEGER,
            marketplace_id    VARCHAR,
            raw_json          JSON,
            synced_at         TIMESTAMPTZ DEFAULT now()
        );

        -- ── Order Items ──────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS order_items (
            amazon_order_id   VARCHAR,
            order_item_id     VARCHAR PRIMARY KEY,
            asin              VARCHAR,
            sku               VARCHAR,
            title             VARCHAR,
            quantity_ordered  INTEGER,
            quantity_shipped  INTEGER,
            item_price_amount DECIMAL(18,4),
            item_price_currency VARCHAR,
            promotion_discount DECIMAL(18,4),
            raw_json          JSON,
            synced_at         TIMESTAMPTZ DEFAULT now()
        );

        -- ── Inventory ────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS inventory (
            snapshot_date     DATE,
            asin              VARCHAR,
            fnsku             VARCHAR,
            sku               VARCHAR,
            product_name      VARCHAR,
            condition         VARCHAR,
            fulfillable_quantity INTEGER,
            inbound_working   INTEGER,
            inbound_shipped   INTEGER,
            inbound_receiving INTEGER,
            reserved_fc_transfers INTEGER,
            reserved_fc_processing INTEGER,
            total_quantity    INTEGER,
            marketplace_id    VARCHAR,
            raw_json          JSON,
            synced_at         TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (snapshot_date, asin, sku)
        );

        -- ── Ads (Sponsored Products — daily summary) ─────────────────────────
        CREATE TABLE IF NOT EXISTS ads_sponsored_products (
            report_date       DATE,
            profile_id        VARCHAR,
            campaign_id       VARCHAR,
            campaign_name     VARCHAR,
            ad_group_id       VARCHAR,
            ad_group_name     VARCHAR,
            asin              VARCHAR,
            sku               VARCHAR,
            impressions       BIGINT,
            clicks            BIGINT,
            spend             DECIMAL(18,4),
            sales_1d          DECIMAL(18,4),
            sales_7d          DECIMAL(18,4),
            sales_14d         DECIMAL(18,4),
            sales_30d         DECIMAL(18,4),
            units_sold_clicks_1d INTEGER,
            units_sold_clicks_7d INTEGER,
            currency          VARCHAR,
            synced_at         TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (report_date, campaign_id, ad_group_id, asin)
        );

        -- ── Finance / Settlements ────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS finance_events (
            event_id          VARCHAR PRIMARY KEY,
            posted_date       TIMESTAMPTZ,
            event_type        VARCHAR,
            amount            DECIMAL(18,4),
            currency          VARCHAR,
            order_id          VARCHAR,
            marketplace_id    VARCHAR,
            description       VARCHAR,
            raw_json          JSON,
            synced_at         TIMESTAMPTZ DEFAULT now()
        );

        -- ── SZ Warehouse（深圳倉，手動上傳）────────────────────────────────────
        CREATE TABLE IF NOT EXISTS sz_warehouse (
            sku               VARCHAR PRIMARY KEY,
            product_code      VARCHAR,
            product_name      VARCHAR,
            available_qty     INTEGER DEFAULT 0,
            shippable_qty     INTEGER DEFAULT 0,
            synced_at         TIMESTAMPTZ DEFAULT now()
        );

        -- ── AWD Inventory（手動上傳 AWD report）────────────────────────────────
        CREATE TABLE IF NOT EXISTS awd_inventory (
            sku               VARCHAR PRIMARY KEY,
            awd_available     INTEGER DEFAULT 0,
            awd_inbound       INTEGER DEFAULT 0,
            awd_outbound      INTEGER DEFAULT 0,
            synced_at         TIMESTAMPTZ DEFAULT now()
        );

        -- ── SKU Config（產品類型 + 箱規，可在 dashboard 編輯）────────────────────
        CREATE TABLE IF NOT EXISTS sku_config (
            sku               VARCHAR PRIMARY KEY,
            product_type      VARCHAR DEFAULT '',
            unit_per_case     INTEGER DEFAULT 1,
            eta               VARCHAR DEFAULT '',
            updated_at        TIMESTAMPTZ DEFAULT now()
        );

        -- ── Product Catalog（MAGEASY Anchor 上傳，SKU ↔ ASIN/Collection/Name）──
        CREATE TABLE IF NOT EXISTS product_catalog (
            sku               VARCHAR PRIMARY KEY,
            parent_asin       VARCHAR DEFAULT '',
            asin              VARCHAR DEFAULT '',
            collection        VARCHAR DEFAULT '',
            product_name      VARCHAR DEFAULT '',
            updated_at        TIMESTAMPTZ DEFAULT now()
        );

        -- ── Sales Summary（Reports API 每日出貨銷量，用於補貨計算）─────────────
        CREATE TABLE IF NOT EXISTS sales_summary (
            report_date       DATE,
            sku               VARCHAR,
            asin              VARCHAR DEFAULT '',
            units_sold        INTEGER DEFAULT 0,
            revenue           DECIMAL(18,4) DEFAULT 0,
            marketplace_id    VARCHAR,
            synced_at         TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (report_date, sku, marketplace_id)
        );

        -- ── Controls（補貨水位參數，可透過 API 動態調整）─────────────────────────
        CREATE TABLE IF NOT EXISTS replenishment_controls (
            key               VARCHAR PRIMARY KEY,
            value             DOUBLE,
            label             VARCHAR DEFAULT '',
            updated_at        TIMESTAMPTZ DEFAULT now()
        );

        -- ── Sales & Traffic（SP-API Business Report，by Child ASIN）────────────
        -- 每筆紀錄代表一個查詢區間（data_start ~ data_end）內某個 Child ASIN 的流量與銷售
        -- 透過 product_catalog join 取得 SKU / Collection / Name
        CREATE TABLE IF NOT EXISTS sales_traffic (
            data_start_date             DATE,
            data_end_date               DATE,
            child_asin                  VARCHAR,
            parent_asin                 VARCHAR DEFAULT '',
            marketplace_id              VARCHAR,
            -- ── 從 product_catalog join 過來 ──────────────────────────────────
            sku                         VARCHAR DEFAULT '',
            collection                  VARCHAR DEFAULT '',
            product_name                VARCHAR DEFAULT '',
            -- ── Traffic ──────────────────────────────────────────────────────
            sessions                    INTEGER DEFAULT 0,
            page_views                  INTEGER DEFAULT 0,
            buy_box_percentage          DECIMAL(8,4) DEFAULT 0,
            unit_session_percentage     DECIMAL(8,4) DEFAULT 0,   -- B2C CVR
            unit_session_percentage_b2b DECIMAL(8,4) DEFAULT 0,   -- B2B CVR
            -- ── Sales ────────────────────────────────────────────────────────
            units_ordered               INTEGER DEFAULT 0,
            units_ordered_b2b           INTEGER DEFAULT 0,
            total_units_ordered         INTEGER DEFAULT 0,         -- B2C + B2B
            ordered_product_sales       DECIMAL(18,4) DEFAULT 0,
            ordered_product_sales_b2b   DECIMAL(18,4) DEFAULT 0,
            total_ordered_product_sales DECIMAL(18,4) DEFAULT 0,  -- B2C + B2B
            total_order_items           INTEGER DEFAULT 0,
            total_order_items_b2b       INTEGER DEFAULT 0,
            synced_at                   TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (data_start_date, data_end_date, child_asin, marketplace_id)
        );

    """)

    # ── Schema migrations (idempotent — safe to run on every startup) ─────────
    # These ALTER TABLE statements add columns that were missing in older schema
    # versions. DuckDB 1.1.x supports "ADD COLUMN IF NOT EXISTS".
    _migrate_schema(conn)

    log.info("duckdb.schema_ready")


def _migrate_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """
    Idempotent column migrations.
    Called after CREATE TABLE IF NOT EXISTS so existing deployments pick up new
    columns without needing to drop/recreate their DuckDB file.
    """
    migrations = [
        # v2: eta column was added to sku_config after initial release
        (
            "sku_config", "eta",
            "ALTER TABLE sku_config ADD COLUMN IF NOT EXISTS eta VARCHAR DEFAULT ''"
        ),
    ]
    for table, column, sql in migrations:
        try:
            conn.execute(sql)
            log.info("db.migration_applied", table=table, column=column)
        except Exception as exc:
            msg = str(exc).lower()
            if "already exists" in msg or "duplicate column" in msg:
                pass  # column already present — nothing to do
            else:
                log.warning("db.migration_warning", table=table, column=column, error=str(exc))
