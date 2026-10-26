"""
Warehouse layers — Dimensions / Silver / Gold views（ARCHITECTURE.md §2, §3, §7）

所有 view 都是 CREATE OR REPLACE，每次啟動重建，可重複執行。
命名規則：
  dim_<entity>                    維度（產品主檔、參數）
  vw_silver_<channel>_<subject>   單一通路、定義一致（Child ASIN 為 key）
  vw_gold_<subject>               跨來源整合
  fact_<subject>_daily            快照 / 事實表

目前使用者：跨境滯銷水位（Excess Inventory）。
補貨（replenishment_service.py）仍使用自己的 CTE，之後依 ARCHITECTURE.md §7 再改讀這些 view，
屆時必須以逐 ASIN 快照比對驗證。Silver view 的定義刻意與補貨 CTE 完全相同。

新增通路（Shopify / TikTok Shop）時：
  1. 新增 vw_silver_<channel>_sales_by_asin（欄位：asin, units_30d, units_prev_30d）
  2. 在 vw_gold_excess_inventory 的 channel_sales 加一段 UNION ALL / 欄位
  3. fact_excess_inventory_daily 加對應欄位（_migrate）
"""

import structlog
import duckdb

log = structlog.get_logger()

# Marketplace IDs
MP_US = "ATVPDKIKX0DER"
MP_CA = "A2EUQ1WTGCTBG2"
MP_MX = "A1AM78C64UM0Y8"
MP_UK = "A1F83G8C2ARO7P"
MP_DE = "A1PA6795UKMFR9"

# 參數預設值（dim_params）— 只在 key 不存在時寫入，不會覆蓋使用者設定
DEFAULT_PARAMS = [
    ("excess_benchmark_usd", 0.0,  "市管處滯銷金額 Benchmark（USD，0 = 未設定）"),
    ("excess_annual_months", 12.0, "滯銷計算：年銷量 = 近 30 天銷量 × N 個月"),
]


def create_warehouse_objects(conn: duckdb.DuckDBPyConnection) -> None:
    """建立 dim / fact 表與 Silver / Gold views。需在欄位 migration 之後呼叫。"""
    try:
        _create_tables(conn)
        _create_views(conn)
        log.info("warehouse.views_ready")
    except Exception as exc:
        # view 建立失敗不應讓整個服務起不來（補貨不依賴這些 view）
        log.error("warehouse.views_failed", error=str(exc))


def _create_tables(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        -- ── dim_params：跨功能共用參數（可在 dashboard 調整）─────────────────
        CREATE TABLE IF NOT EXISTS dim_params (
            key        VARCHAR PRIMARY KEY,
            value      DOUBLE,
            label      VARCHAR DEFAULT '',
            updated_at TIMESTAMPTZ DEFAULT now()
        );

        -- ── 跨境滯銷水位：每日快照（存組成數字，定義改變時可重算）──────────
        CREATE TABLE IF NOT EXISTS fact_excess_inventory_daily (
            snapshot_date     DATE,
            asin              VARCHAR,
            collection        VARCHAR DEFAULT '',
            cog               DECIMAL(10,4) DEFAULT 0,
            fba_us            INTEGER DEFAULT 0,
            fba_uk            INTEGER DEFAULT 0,
            fba_de            INTEGER DEFAULT 0,
            awd_available     INTEGER DEFAULT 0,
            us_qty            INTEGER DEFAULT 0,
            sales_us_30d      INTEGER DEFAULT 0,
            sales_ca_30d      INTEGER DEFAULT 0,
            sales_mx_30d      INTEGER DEFAULT 0,
            sales_uk_30d      INTEGER DEFAULT 0,
            sales_de_30d      INTEGER DEFAULT 0,
            all_channel_30d   INTEGER DEFAULT 0,
            annual_months     DOUBLE  DEFAULT 12,
            stock_total       INTEGER DEFAULT 0,
            excess_qty        INTEGER DEFAULT 0,
            excess_value      DECIMAL(18,2) DEFAULT 0,
            excess_in_us_wh   INTEGER DEFAULT 0,
            excess_in_awd     INTEGER DEFAULT 0,
            excess_in_fba     INTEGER DEFAULT 0,
            pending_qty       INTEGER DEFAULT 0,
            synced_at         TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (snapshot_date, asin)
        );
    """)
    for key, value, label in DEFAULT_PARAMS:
        conn.execute(
            "INSERT OR IGNORE INTO dim_params (key, value, label) VALUES (?, ?, ?)",
            [key, value, label],
        )


def _create_views(conn: duckdb.DuckDBPyConnection) -> None:
    # ════════════════════════════════════════════════════════════════════════
    # Dimension
    # ════════════════════════════════════════════════════════════════════════
    # dim_product：MAGEASY Anchor，一個 Child ASIN 一列（內部 SKU 只用來顯示）
    conn.execute("""
        CREATE OR REPLACE VIEW dim_product AS
        SELECT asin, sku, parent_asin, collection, product_name, cog
        FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY asin ORDER BY sku) AS rn
            FROM product_catalog
            WHERE asin IS NOT NULL AND asin != ''
        ) WHERE rn = 1
    """)

    # ════════════════════════════════════════════════════════════════════════
    # Silver — Amazon
    # ════════════════════════════════════════════════════════════════════════
    # FBA：各站最新快照；同 ASIN 下所有 SKU 回報同一份庫存 → 取 MAX，不加總。
    # 排除 Amazon 自動產生的 alias SKU（XX-XXXX-XXXX）。（與補貨 fba_by_asin 相同）
    conn.execute(r"""
        CREATE OR REPLACE VIEW vw_silver_amazon_fba_by_asin AS
        WITH latest AS (
            SELECT marketplace_id, MAX(snapshot_date) AS snapshot_date
            FROM inventory GROUP BY marketplace_id
        )
        SELECT
            i.marketplace_id,
            i.asin,
            MAX(i.snapshot_date) AS snapshot_date,
            MAX(i.fulfillable_quantity
                + COALESCE(i.reserved_fc_transfers, 0)
                + COALESCE(i.reserved_fc_processing, 0))                 AS fba_available,
            MAX(i.inbound_working + i.inbound_shipped + i.inbound_receiving) AS fba_inbound
        FROM inventory i
        JOIN latest l
          ON i.marketplace_id = l.marketplace_id AND i.snapshot_date = l.snapshot_date
        WHERE i.asin IS NOT NULL AND i.asin != ''
          AND NOT regexp_matches(i.sku, '^[A-Z0-9]{2}-[A-Z0-9]{4}-[A-Z0-9]{4}$')
        GROUP BY i.marketplace_id, i.asin
    """)

    # AWD（只有 US）：Report/CSV 有 ASIN；API 只有 SKU → 用 Amazon 自己的 SKU↔ASIN 補
    conn.execute(f"""
        CREATE OR REPLACE VIEW vw_silver_amazon_awd_by_asin AS
        WITH amazon_sku_asin AS (
            SELECT sku, arg_max(asin, snapshot_date) AS asin
            FROM inventory
            WHERE sku IS NOT NULL AND sku != '' AND asin IS NOT NULL AND asin != ''
            GROUP BY sku
        )
        SELECT
            '{MP_US}'                              AS marketplace_id,
            COALESCE(NULLIF(a.asin, ''), m.asin)   AS asin,
            SUM(a.awd_available)                   AS awd_available,
            SUM(a.awd_inbound)                     AS awd_inbound,
            SUM(a.awd_outbound)                    AS awd_outbound,
            MAX(a.synced_at)                       AS synced_at
        FROM awd_inventory a
        LEFT JOIN amazon_sku_asin m ON a.sku = m.sku
        WHERE COALESCE(NULLIF(a.asin, ''), m.asin) IS NOT NULL
        GROUP BY 2
    """)

    # Sales（每站、每 ASIN）：近 30 天 + 前 30 天（第 31–60 天，用於趨勢）
    # Reports API（sales_summary）優先；該站近 30 天完全沒有 report 資料時才用 orders 備援
    # （與補貨 sales_by_asin 相同的規則，只是改成逐站判斷）
    conn.execute("""
        CREATE OR REPLACE VIEW vw_silver_amazon_sales_by_asin AS
        WITH rpt AS (
            SELECT marketplace_id, asin,
                   SUM(CASE WHEN report_date >= CURRENT_DATE - INTERVAL 30 DAYS
                            THEN units_sold ELSE 0 END)                    AS units_30d,
                   SUM(CASE WHEN report_date <  CURRENT_DATE - INTERVAL 30 DAYS
                            THEN units_sold ELSE 0 END)                    AS units_prev_30d,
                   COUNT(CASE WHEN report_date >= CURRENT_DATE - INTERVAL 30 DAYS
                              THEN 1 END)                                  AS rows_30d
            FROM sales_summary
            WHERE report_date >= CURRENT_DATE - INTERVAL 60 DAYS
              AND asin != ''
            GROUP BY marketplace_id, asin
        ),
        rpt_mp AS (
            SELECT marketplace_id FROM rpt GROUP BY marketplace_id HAVING SUM(rows_30d) > 0
        ),
        ord AS (
            SELECT o.marketplace_id, oi.asin,
                   SUM(CASE WHEN o.purchase_date >= NOW() - INTERVAL '30 days'
                            THEN oi.quantity_ordered ELSE 0 END)           AS units_30d,
                   SUM(CASE WHEN o.purchase_date <  NOW() - INTERVAL '30 days'
                            THEN oi.quantity_ordered ELSE 0 END)           AS units_prev_30d
            FROM order_items oi
            JOIN orders o ON oi.amazon_order_id = o.amazon_order_id
            WHERE o.purchase_date >= NOW() - INTERVAL '60 days'
              AND o.order_status NOT IN ('Canceled', 'Cancelled')
              AND oi.asin IS NOT NULL AND oi.asin != ''
            GROUP BY o.marketplace_id, oi.asin
        )
        SELECT marketplace_id, asin, units_30d, units_prev_30d, 'reports' AS source
        FROM rpt
        WHERE marketplace_id IN (SELECT marketplace_id FROM rpt_mp)
        UNION ALL
        SELECT marketplace_id, asin, units_30d, units_prev_30d, 'orders' AS source
        FROM ord
        WHERE marketplace_id NOT IN (SELECT marketplace_id FROM rpt_mp)
    """)

    # ════════════════════════════════════════════════════════════════════════
    # Silver — 深圳倉 template（美國倉 / 佳樂倉 / 欠數）
    # ════════════════════════════════════════════════════════════════════════
    # 沒有站點之分：美國倉供應所有通路 → 每個 ASIN 只有一個數字，Gold 層只能加一次
    conn.execute("""
        CREATE OR REPLACE VIEW vw_silver_sz_by_asin AS
        WITH sz AS (
            SELECT COALESCE(NULLIF(s.asin, ''), pc.asin) AS asin,
                   s.us_qty, s.jl_qty, s.pending_qty, s.order_date,
                   s.factory_confirmed_date, s.unit_per_case, s.synced_at
            FROM sz_warehouse s
            LEFT JOIN product_catalog pc ON s.sku = pc.sku
        )
        SELECT asin,
               SUM(us_qty)                                       AS us_qty,
               SUM(jl_qty)                                       AS jl_qty,
               SUM(pending_qty)                                  AS pending_qty,
               MAX(order_date)                                   AS order_date,
               MAX(factory_confirmed_date)                       AS factory_confirmed_date,
               MAX(CASE WHEN unit_per_case > 1 THEN unit_per_case END) AS unit_per_case,
               MAX(synced_at)                                    AS synced_at
        FROM sz
        WHERE asin IS NOT NULL AND asin != ''
        GROUP BY asin
    """)

    # ════════════════════════════════════════════════════════════════════════
    # Gold — 跨境滯銷水位（全通路，一個 Child ASIN 一列）
    # ════════════════════════════════════════════════════════════════════════
    # 庫存池 = FBA Available（US+UK+DE）+ AWD Available + 美國倉
    #   - 只列舉 US/UK/DE 的 FBA；CA/MX 若日後同步到（北美遠程配送會回報 US 的同一份庫存）
    #     也不會被重複加進來
    #   - 美國倉來自 vw_silver_sz_by_asin，每個 ASIN 只有一列 → 只加一次
    # 全通路 30 天銷量 = Amazon US + CA + MX + UK + DE（之後加 Shopify / TikTok Shop）
    # 滯銷數量 = max(0, 庫存池 − 全通路 30 天銷量 × N)，逐 ASIN 計算後才加總
    # 滯銷位置（由上游往下游分配：美國倉 → AWD → FBA；越靠近客戶的貨越先賣掉）
    conn.execute(f"""
        CREATE OR REPLACE VIEW vw_gold_excess_inventory AS
        WITH
        params AS (
            SELECT
                COALESCE(MAX(CASE WHEN key = 'excess_annual_months' THEN value END), 12) AS annual_months
            FROM dim_params
        ),
        fba AS (
            SELECT asin,
                   SUM(CASE WHEN marketplace_id = '{MP_US}' THEN fba_available ELSE 0 END) AS fba_us,
                   SUM(CASE WHEN marketplace_id = '{MP_UK}' THEN fba_available ELSE 0 END) AS fba_uk,
                   SUM(CASE WHEN marketplace_id = '{MP_DE}' THEN fba_available ELSE 0 END) AS fba_de,
                   SUM(fba_inbound)                                                        AS fba_inbound
            FROM vw_silver_amazon_fba_by_asin
            WHERE marketplace_id IN ('{MP_US}', '{MP_UK}', '{MP_DE}')
            GROUP BY asin
        ),
        amz_sales AS (
            SELECT asin,
                   SUM(CASE WHEN marketplace_id = '{MP_US}' THEN units_30d ELSE 0 END) AS sales_us_30d,
                   SUM(CASE WHEN marketplace_id = '{MP_CA}' THEN units_30d ELSE 0 END) AS sales_ca_30d,
                   SUM(CASE WHEN marketplace_id = '{MP_MX}' THEN units_30d ELSE 0 END) AS sales_mx_30d,
                   SUM(CASE WHEN marketplace_id = '{MP_UK}' THEN units_30d ELSE 0 END) AS sales_uk_30d,
                   SUM(CASE WHEN marketplace_id = '{MP_DE}' THEN units_30d ELSE 0 END) AS sales_de_30d,
                   SUM(units_prev_30d)                                                 AS amazon_prev_30d
            FROM vw_silver_amazon_sales_by_asin
            WHERE marketplace_id IN ('{MP_US}', '{MP_CA}', '{MP_MX}', '{MP_UK}', '{MP_DE}')
            GROUP BY asin
        ),
        base AS (
            SELECT
                p.asin,
                COALESCE(p.parent_asin, '')            AS parent_asin,
                COALESCE(p.sku, '')                    AS sku,
                COALESCE(p.collection, '')             AS collection,
                COALESCE(p.product_name, '')           AS product_name,
                COALESCE(ac.product_type, '')          AS product_type,
                COALESCE(p.cog, 0)                     AS cog,
                COALESCE(f.fba_us, 0)::INTEGER         AS fba_us,
                COALESCE(f.fba_uk, 0)::INTEGER         AS fba_uk,
                COALESCE(f.fba_de, 0)::INTEGER         AS fba_de,
                COALESCE(f.fba_inbound, 0)::INTEGER    AS fba_inbound,
                COALESCE(a.awd_available, 0)::INTEGER  AS awd_available,
                COALESCE(a.awd_inbound, 0)::INTEGER    AS awd_inbound,
                COALESCE(sz.us_qty, 0)::INTEGER        AS us_qty,
                COALESCE(sz.jl_qty, 0)::INTEGER        AS jl_qty,
                COALESCE(sz.pending_qty, 0)::INTEGER   AS pending_qty,
                COALESCE(s.sales_us_30d, 0)::INTEGER   AS sales_us_30d,
                COALESCE(s.sales_ca_30d, 0)::INTEGER   AS sales_ca_30d,
                COALESCE(s.sales_mx_30d, 0)::INTEGER   AS sales_mx_30d,
                COALESCE(s.sales_uk_30d, 0)::INTEGER   AS sales_uk_30d,
                COALESCE(s.sales_de_30d, 0)::INTEGER   AS sales_de_30d,
                COALESCE(s.amazon_prev_30d, 0)::INTEGER AS amazon_prev_30d
            FROM dim_product p
            LEFT JOIN fba                          f  ON p.asin = f.asin
            LEFT JOIN vw_silver_amazon_awd_by_asin a  ON p.asin = a.asin
            LEFT JOIN vw_silver_sz_by_asin         sz ON p.asin = sz.asin
            LEFT JOIN amz_sales                    s  ON p.asin = s.asin
            LEFT JOIN asin_config                  ac ON p.asin = ac.asin
        ),
        calc AS (
            SELECT b.*,
                   prm.annual_months,
                   (b.fba_us + b.fba_uk + b.fba_de)                         AS fba_available,
                   (b.sales_us_30d + b.sales_ca_30d + b.sales_mx_30d
                    + b.sales_uk_30d + b.sales_de_30d)                      AS amazon_30d,
                   b.amazon_prev_30d                                        AS all_channel_prev_30d
            FROM base b CROSS JOIN params prm
        ),
        calc2 AS (
            SELECT *,
                   amazon_30d                                               AS all_channel_30d,
                   (fba_available + awd_available + us_qty)                 AS stock_total
            FROM calc
        ),
        calc3 AS (
            SELECT *,
                   ROUND(all_channel_30d * annual_months)::INTEGER          AS annual_sales,
                   GREATEST(0, ROUND(stock_total - all_channel_30d * annual_months))::INTEGER
                                                                            AS excess_qty
            FROM calc2
        ),
        calc4 AS (
            SELECT *,
                   ROUND(excess_qty * cog, 2)                               AS excess_value,
                   LEAST(excess_qty, us_qty)                                AS excess_in_us_wh
            FROM calc3
        ),
        calc5 AS (
            SELECT *,
                   LEAST(excess_qty - excess_in_us_wh, awd_available)       AS excess_in_awd
            FROM calc4
        )
        SELECT *,
               (excess_qty - excess_in_us_wh - excess_in_awd)               AS excess_in_fba,
               CASE WHEN all_channel_30d > 0
                    THEN ROUND(stock_total / all_channel_30d, 1) END        AS months_of_supply,
               CASE
                   WHEN stock_total = 0                         THEN '無庫存'
                   WHEN excess_qty > 0 AND all_channel_30d = 0  THEN '無銷量'
                   WHEN excess_qty > 0 AND stock_total >= all_channel_30d * 24 THEN '24M+'
                   WHEN excess_qty > 0 AND stock_total >= all_channel_30d * 18 THEN '18–24M'
                   WHEN excess_qty > 0                          THEN '12–18M'
                   WHEN stock_total >= all_channel_30d * 9      THEN '潛在滯銷 9–12M'
                   ELSE '正常'
               END                                                          AS supply_tier,
               CASE WHEN all_channel_prev_30d > 0
                    THEN ROUND((all_channel_30d - all_channel_prev_30d) * 100.0
                               / all_channel_prev_30d, 1) END               AS sales_trend_pct,
               (pending_qty + fba_inbound + awd_inbound)                    AS replenishing_qty,
               (excess_qty > 0 AND (pending_qty + fba_inbound + awd_inbound) > 0)
                                                                            AS still_replenishing,
               (cog = 0 AND stock_total > 0)                                AS cog_missing
        FROM calc5
    """)
