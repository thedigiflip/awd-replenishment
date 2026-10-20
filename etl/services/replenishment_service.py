"""
Replenishment Service
完整對應 Inventory Analytics A-AB（28 欄）

Controls 常數（對應 Controls tab）:
  空運觸發閾值   = (30 + 40) / 30 = 2.33 months
  AWD 目標一般款 = 1.0 months  (Controls B8)
  AWD 目標流量款 = 1.5 months  (Controls B9)
  FBA+AWD 總上限 = 3.0 months  (Controls B10)
  SZ 返單水位一般款 = 1.0 months  (Controls B12)
  SZ 返單水位流量款 = 1.5 months  (Controls B13)
"""

import asyncio
import math
from datetime import datetime, timezone

import structlog

from core.database import new_conn, db_write

log = structlog.get_logger()

# ── Controls 預設值（DB 沒有資料時使用）──────────────────────────────────────
_DEFAULT_CONTROLS = {
    "production_days":         30.0,
    "sea_days":                40.0,
    "awd_target_normal":        1.0,
    "awd_target_hero":          1.5,
    "awd_target_rocket":        3.0,   # 🚀 Rocket 比流量款更高備貨
    "total_cap_months":         3.0,
    "sz_level_normal":          1.0,
    "sz_level_hero":            1.5,
    "sz_level_rocket":          2.0,   # 🚀 Rocket 返單水位
    # SZ 移倉水位（新）— 美國倉應保持幾個月的庫存
    # 移倉數建議 = max(0, 月銷量 × sz_transfer_level − 美國倉現有)
    "sz_transfer_level_normal": 1.0,
    "sz_transfer_level_hero":   1.5,
    "sz_transfer_level_rocket": 2.5,   # 🚀 Rocket 移倉水位
    # 移倉觸發門檻：只有 Total Coverage(FBA+AWD) < 此值才提示移倉，避免過度囤貨降低周轉
    "transfer_trigger_coverage": 4.0,
    # 返單觸發門檻：只有 Total Coverage(FBA+AWD) < 此值才建議返單（工廠 45 天 lead time，門檻可設較高）
    "reorder_trigger_coverage":  4.0,
}

_CONTROLS_LABELS = {
    "production_days":         "生產天數",
    "sea_days":                "海運天數",
    "awd_target_normal":       "AWD 目標月數（一般款）",
    "awd_target_hero":         "AWD 目標月數（流量款）",
    "awd_target_rocket":       "AWD 目標月數（Rocket 🚀）",
    "total_cap_months":        "FBA+AWD 總上限月數",
    "sz_level_normal":         "SZ 返單水位（一般款）",
    "sz_level_hero":           "SZ 返單水位（流量款）",
    "sz_level_rocket":         "SZ 返單水位（Rocket 🚀）",
    "sz_transfer_level_normal": "SZ 移倉水位（一般款）",
    "sz_transfer_level_hero":   "SZ 移倉水位（流量款）",
    "sz_transfer_level_rocket": "SZ 移倉水位（Rocket 🚀）",
    "transfer_trigger_coverage": "移倉觸發門檻（Total Coverage）",
    "reorder_trigger_coverage":  "返單觸發門檻（Total Coverage）",
}


class ReplenishmentService:

    # ── Controls ─────────────────────────────────────────────────────────────

    def _load_controls_sync(self) -> dict:
        """從 DB 讀取 controls，缺少的 key 用預設值補齊。"""
        conn = new_conn()
        try:
            rows = conn.execute(
                "SELECT key, value FROM replenishment_controls"
            ).fetchall()
            db_vals = {r[0]: float(r[1]) for r in rows}
        except Exception:
            db_vals = {}
        finally:
            conn.close()
        # 合併：DB 值優先，缺少的用預設值
        return {k: db_vals.get(k, v) for k, v in _DEFAULT_CONTROLS.items()}

    async def get_controls(self) -> dict:
        """回傳目前所有 controls 值（含 label），供 API GET 使用。"""
        vals = await asyncio.to_thread(self._load_controls_sync)
        return {
            k: {"value": vals[k], "label": _CONTROLS_LABELS.get(k, k)}
            for k in _DEFAULT_CONTROLS
        }

    async def upsert_controls(self, updates: dict) -> dict:
        """更新 controls，只更新傳入的 key，其他不動。回傳更新後完整值。"""
        valid_keys = set(_DEFAULT_CONTROLS.keys())
        filtered = {k: float(v) for k, v in updates.items() if k in valid_keys}
        if not filtered:
            raise ValueError(f"無有效的 key，可用的 key: {sorted(valid_keys)}")

        now = datetime.now(tz=timezone.utc)
        rows = [
            (k, v, _CONTROLS_LABELS.get(k, k), now)
            for k, v in filtered.items()
        ]

        def _write():
            conn = new_conn()
            try:
                conn.executemany("""
                    INSERT OR REPLACE INTO replenishment_controls
                        (key, value, label, updated_at)
                    VALUES (?, ?, ?, ?)
                """, rows)
                conn.commit()
            finally:
                conn.close()

        await db_write(_write)
        return await self.get_controls()

    async def get_table(self, marketplace_id: str = "ATVPDKIKX0DER") -> list[dict]:
        controls, raw = await asyncio.gather(
            asyncio.to_thread(self._load_controls_sync),
            asyncio.to_thread(self._query, marketplace_id),
        )
        return [self._compute(row, controls) for row in raw]

    # ── SQL — JOIN 五張表 ──────────────────────────────────────────────────────

    def _query(self, marketplace_id: str) -> list[dict]:
        """
        v2 — 使用 ASIN 為 Key 聚合 FBA / AWD / Sales
        ────────────────────────────────────────────────────────────────────────
        以前用 SKU 為 Key，會有兩個問題：
        1. 同一產品換 SKU 時（例如 CoverBuddyLite 1.2 → 1.3），舊 SKU 的銷量對不到 Anchor
        2. Amazon 系統自動產生的 SKU（如 "Amazon.Found.XXX"、"Stickered.MSKU.XXX"、
           " ME" 後綴等）都會被漏掉

        新做法：
        - Anchor 提供 SKU → ASIN 對應（權威來源）
        - FBA / Sales 已有 ASIN，直接依 ASIN 加總
        - AWD 沒有 ASIN，透過所有已知來源建 SKU→ASIN 對應後再加總
        - 一個 ASIN 對應多個 SKU 時，sku_count > 1，all_skus 會列出全部
        """
        conn = new_conn()
        try:
            rows = conn.execute("""
                WITH
                -- ════════════════════════════════════════════════════════════════
                -- 全面以 Child ASIN 為 key（2026-10-20）
                -- SKU 是內部編碼，跟 Amazon 上的 SKU 對不上（例：多了 " ME" 後綴、
                -- 舊版 SKU 仍掛在同一 ASIN）。唯一可靠的對應是 Child ASIN。
                --   - FBA / Sales / AWD Report：Amazon 直接給 ASIN
                --   - AWD API：只給 Amazon 自己的 SKU → 用 Amazon 自己的 SKU↔ASIN 換算
                --   - 深圳倉 template：存 ASIN（內部檔案，缺 ASIN 才用 Anchor 換算）
                --   - 產品類型 / 箱規：asin_config（以 ASIN 為 key）
                -- ════════════════════════════════════════════════════════════════

                -- ── Anchor：一個 ASIN 一列（內部 SKU 只用來顯示）────────────────
                anchor AS (
                    SELECT asin, sku, parent_asin, collection, product_name, cog
                    FROM (
                        SELECT *, ROW_NUMBER() OVER (PARTITION BY asin ORDER BY sku) AS rn
                        FROM product_catalog
                        WHERE asin IS NOT NULL AND asin != ''
                    ) WHERE rn = 1
                ),

                -- ── Amazon 自己回報的 SKU→ASIN（只給 AWD API 換算用，不含 Anchor）──
                amazon_sku_asin AS (
                    SELECT sku, arg_max(asin, snapshot_date) AS asin
                    FROM inventory
                    WHERE sku IS NOT NULL AND sku != '' AND asin IS NOT NULL AND asin != ''
                    GROUP BY sku
                ),

                -- ── 多 SKU 提示（純顯示）：Amazon 上這個 ASIN 掛了幾個 SKU ─────────
                skus_per_asin AS (
                    SELECT asin,
                           COUNT(DISTINCT sku)                         AS sku_count,
                           STRING_AGG(DISTINCT sku, ', ' ORDER BY sku) AS all_skus
                    FROM (
                        SELECT DISTINCT sku, asin FROM inventory
                        WHERE sku IS NOT NULL AND sku != '' AND asin IS NOT NULL AND asin != ''
                          AND NOT regexp_matches(sku, '^[A-Z]{2}-[A-Z0-9]{4}-[A-Z0-9]{4}$')
                    )
                    GROUP BY asin
                ),

                -- ── FBA by ASIN ───────────────────────────────────────────────────
                -- MAX(snapshot_date) 依當前 marketplace 限縮（各站同步日期不同）
                -- Amazon 對同一 ASIN 底下的每個 SKU（舊版、" ME"、Stickered…）回報的是
                -- 同一份庫存，所以每個 ASIN 只取一個數字（MAX），不加總、不比對 SKU。
                -- 排除 Amazon 自動產生的 alias SKU（XX-XXXX-XXXX）。
                fba_by_asin AS (
                    SELECT
                        asin,
                        MAX(fulfillable_quantity
                            + COALESCE(reserved_fc_transfers, 0)
                            + COALESCE(reserved_fc_processing, 0))               AS fba_available,
                        MAX(inbound_working + inbound_shipped + inbound_receiving) AS fba_inbound
                    FROM inventory
                    WHERE snapshot_date = (
                              SELECT MAX(snapshot_date) FROM inventory
                              WHERE marketplace_id = ?
                          )
                      AND marketplace_id = ?
                      AND asin IS NOT NULL AND asin != ''
                      AND NOT regexp_matches(sku, '^[A-Z]{2}-[A-Z0-9]{4}-[A-Z0-9]{4}$')
                    GROUP BY asin
                ),

                -- ── AWD by ASIN（US only）──────────────────────────────────────────
                -- AWD Report / CSV 上傳會直接寫入 ASIN；AWD API 只給 SKU，
                -- 用 Amazon 自己的 SKU↔ASIN 補上
                awd_by_asin AS (
                    SELECT
                        COALESCE(NULLIF(a.asin, ''), m.asin) AS asin,
                        SUM(a.awd_available)  AS awd_available,
                        SUM(a.awd_inbound)    AS awd_inbound,
                        SUM(a.awd_outbound)   AS awd_outbound,
                        MAX(a.synced_at)      AS synced_at
                    FROM awd_inventory a
                    LEFT JOIN amazon_sku_asin m ON a.sku = m.sku
                    WHERE ? = 'ATVPDKIKX0DER'   -- 非 US 站點時 CTE 為空
                      AND COALESCE(NULLIF(a.asin, ''), m.asin) IS NOT NULL
                    GROUP BY 1
                ),

                -- ── Sales by ASIN — Reports API 優先 ────────────────────────────
                sales_reports AS (
                    SELECT asin, SUM(units_sold) AS sales_30d
                    FROM sales_summary
                    WHERE report_date >= CURRENT_DATE - INTERVAL 30 DAYS
                      AND marketplace_id = ?
                      AND asin != ''
                    GROUP BY asin
                ),
                -- Fallback：order_items（Reports API 未授權時）
                sales_orders AS (
                    SELECT oi.asin, SUM(oi.quantity_ordered) AS sales_30d
                    FROM order_items oi
                    JOIN orders o ON oi.amazon_order_id = o.amazon_order_id
                    WHERE o.purchase_date >= NOW() - INTERVAL '30 days'
                      AND o.order_status NOT IN ('Canceled', 'Cancelled')
                      AND o.marketplace_id = ?
                      AND oi.asin IS NOT NULL AND oi.asin != ''
                    GROUP BY oi.asin
                ),
                sales_by_asin AS (
                    SELECT asin, sales_30d FROM sales_reports
                    WHERE (SELECT COUNT(*) FROM sales_reports) > 0
                    UNION ALL
                    SELECT asin, sales_30d FROM sales_orders
                    WHERE (SELECT COUNT(*) FROM sales_reports) = 0
                ),

                -- ── 深圳倉（美國倉 + 佳樂倉 + 欠數）依 ASIN ───────────────────────
                -- sz_warehouse.asin 由上傳時寫入；舊資料缺 ASIN 才用 Anchor 內部 SKU 換算
                sz AS (
                    SELECT COALESCE(NULLIF(s.asin, ''), pc.asin) AS asin, s.*
                    FROM sz_warehouse s
                    LEFT JOIN product_catalog pc ON s.sku = pc.sku
                ),
                us_by_asin AS (
                    SELECT asin,
                           SUM(us_qty)      AS us_qty,
                           SUM(pending_qty) AS pending_qty,
                           MAX(order_date)  AS order_date,
                           MAX(factory_confirmed_date) AS factory_confirmed_date,
                           MAX(synced_at)   AS synced_at
                    FROM sz WHERE asin IS NOT NULL AND asin != ''
                    GROUP BY asin
                ),
                jl_by_asin AS (
                    SELECT asin, SUM(jl_qty) AS jl_qty
                    FROM sz WHERE asin IS NOT NULL AND asin != ''
                    GROUP BY asin
                ),
                uc_by_asin AS (
                    SELECT asin, MAX(unit_per_case) AS unit_per_case
                    FROM sz WHERE asin IS NOT NULL AND asin != '' AND unit_per_case > 1
                    GROUP BY asin
                )

                -- ── 主查詢：以 Anchor 的 Child ASIN 為列 ────────────────────────────
                SELECT
                    pc.sku,
                    pc.asin                                       AS asin,
                    COALESCE(pc.parent_asin, '')                  AS parent_asin,
                    COALESCE(pc.collection,  '')                  AS collection,
                    COALESCE(pc.product_name, '')                 AS product_name,
                    COALESCE(ac.product_type, '')                 AS product_type,
                    COALESCE(ac.eta, '')                          AS eta,
                    COALESCE(s.sales_30d, 0)                      AS sales_30d,
                    COALESCE(f.fba_available, 0)                  AS fba_available,
                    COALESCE(f.fba_inbound, 0)                    AS fba_inbound,
                    COALESCE(a.awd_available, 0)                  AS awd_available,
                    COALESCE(a.awd_inbound, 0)                    AS awd_inbound,
                    COALESCE(a.awd_outbound, 0)                   AS awd_outbound,
                    COALESCE(us.us_qty, 0)                        AS us_qty,
                    COALESCE(us.pending_qty, 0)                   AS pending_qty,
                    us.order_date                                 AS order_date,
                    us.factory_confirmed_date                     AS factory_confirmed_date,
                    COALESCE(jl.jl_qty, 0)                        AS jl_qty,
                    -- Unit/Case 優先用 SZ template 上傳的，沒設定回退 asin_config
                    COALESCE(uc.unit_per_case, ac.unit_per_case, 1) AS unit_per_case,
                    a.synced_at                                   AS awd_synced_at,
                    us.synced_at                                  AS sz_synced_at,
                    COALESCE(sap.sku_count, 1)                    AS sku_count,
                    COALESCE(sap.all_skus, pc.sku)                AS all_skus,
                    -- 產品成本（Anchor COG，一律美元）—— 滯銷金額用
                    COALESCE(pc.cog, 0)                           AS cog
                FROM anchor pc
                LEFT JOIN fba_by_asin    f   ON pc.asin = f.asin
                LEFT JOIN awd_by_asin    a   ON pc.asin = a.asin
                LEFT JOIN sales_by_asin  s   ON pc.asin = s.asin
                LEFT JOIN us_by_asin     us  ON pc.asin = us.asin
                LEFT JOIN jl_by_asin     jl  ON pc.asin = jl.asin
                LEFT JOIN uc_by_asin     uc  ON pc.asin = uc.asin
                LEFT JOIN asin_config    ac  ON pc.asin = ac.asin
                LEFT JOIN skus_per_asin  sap ON pc.asin = sap.asin
                ORDER BY pc.collection NULLS LAST, pc.sku
            """, [marketplace_id, marketplace_id, marketplace_id,
                  marketplace_id, marketplace_id]).fetchall()

            cols = [
                "sku", "asin", "parent_asin", "collection", "product_name",
                "product_type", "eta",
                "sales_30d", "fba_available", "fba_inbound",
                "awd_available", "awd_inbound", "awd_outbound",
                "us_qty", "pending_qty", "order_date", "factory_confirmed_date",
                "jl_qty", "unit_per_case",
                "awd_synced_at", "sz_synced_at",
                "sku_count", "all_skus",
                "cog",
            ]
            return [dict(zip(cols, r)) for r in rows]
        finally:
            conn.close()

    # ── 公式計算（完整對應 Excel v4 A-AB 28 欄）──────────────────────────────────

    def _compute(self, r: dict, controls: dict | None = None) -> dict:
        c = controls or _DEFAULT_CONTROLS

        PRODUCTION_DAYS  = c["production_days"]
        SEA_DAYS         = c["sea_days"]
        AIR_THRESHOLD    = (PRODUCTION_DAYS + SEA_DAYS) / 30
        TOTAL_CAP_MONTHS = c["total_cap_months"]

        prod_type   = r["product_type"] or ""          # F
        sales       = float(r["sales_30d"])             # H
        fba_avail   = float(r["fba_available"])
        fba_inbound = float(r["fba_inbound"])           # I
        fba_total   = fba_avail + fba_inbound           # J

        awd_avail   = float(r["awd_available"])         # L
        awd_inbound = float(r["awd_inbound"])           # M
        awd_out     = float(r["awd_outbound"])          # N  — 資訊欄，不列入計算
        # AWD Total (O) = Available + Inbound
        # Amazon 資料模型：outbound units 已從 awd_available 扣除（實體離開 AWD 倉），
        # 並自動出現在 FBA inbound_shipped。若再扣一次會重複扣減、造成 Total Coverage
        # 偏低或負值。Outbound 保留為獨立顯示欄位，供業務追蹤「AWD → FBA 移轉中」的量。
        awd_total   = awd_avail + awd_inbound

        # SZ 兩倉（新版）+ 欠數 + 下單日期 + 工廠回覆交期
        us_qty      = float(r.get("us_qty") or 0)        # 美國倉現貨
        pending_qty = float(r.get("pending_qty") or 0)   # 欠數（已下單未到）
        order_date  = r.get("order_date")                # 下單日期（Option A：最新一筆）
        factory_confirmed_date = r.get("factory_confirmed_date")  # 工廠回覆交期
        jl_qty      = float(r.get("jl_qty") or 0)        # 佳樂倉
        # 美國虛擬庫存 = 現貨 + 已下單（將自動入美國倉）
        us_virtual  = us_qty + pending_qty
        # 深圳倉庫總量（不含欠數，因為欠數不在物理倉）
        sz_total    = us_qty + jl_qty
        # 相容舊變數
        sz_ship     = us_qty
        unit_case   = max(1, int(r.get("unit_per_case") or 1))

        # ── 約定交期 & 工廠回覆交期 & 逾期判定 ─────────────────────────
        from datetime import date, timedelta
        expected_date  = None
        overdue_days   = 0        # > 0 表示逾期
        days_to_arrive = None     # None or 天數（負=逾期）
        # 統一 order_date 為 date 型別
        if order_date:
            if isinstance(order_date, str):
                try:
                    order_date = date.fromisoformat(order_date[:10])
                except Exception:
                    order_date = None
        # 統一 factory_confirmed_date 為 date 型別
        if factory_confirmed_date:
            if isinstance(factory_confirmed_date, str):
                try:
                    factory_confirmed_date = date.fromisoformat(factory_confirmed_date[:10])
                except Exception:
                    factory_confirmed_date = None
        # 計算約定交期
        if order_date:
            expected_date  = order_date + timedelta(days=int(PRODUCTION_DAYS))

        # ── 有效交期：工廠回覆優先，否則用約定交期（Option A）───────────
        # effective_date 用於「交期倒數」和「逾期偵測」
        effective_date = factory_confirmed_date or expected_date
        delivery_advance_days = None  # 正 = 工廠提前 X 天；負 = 工廠延後 X 天
        if factory_confirmed_date and expected_date:
            delivery_advance_days = (expected_date - factory_confirmed_date).days

        if effective_date:
            days_to_arrive = (effective_date - date.today()).days
            if days_to_arrive < 0:
                overdue_days = -days_to_arrive

        # 依產品類型選 controls 值：Rocket > 流量款 > 一般款
        def _pick(rocket_key, hero_key, normal_key):
            if prod_type == "Rocket":  return c[rocket_key]
            if prod_type == "流量款":   return c[hero_key]
            return c[normal_key]
        # G — AWD 目標月數（動態）
        awd_target = _pick("awd_target_rocket", "awd_target_hero", "awd_target_normal")
        # S — 返單水位（動態）
        sz_level   = _pick("sz_level_rocket", "sz_level_hero", "sz_level_normal")
        # SZ 移倉水位（動態）— 美國倉應保持幾個月的庫存
        transfer_level = _pick("sz_transfer_level_rocket", "sz_transfer_level_hero", "sz_transfer_level_normal")

        def _div(a, b, dec=2):
            return round(a / b, dec) if b and b > 0 else None

        # K — FBA Total Month
        fba_month = _div(fba_total, sales)
        # P — AWD Month
        awd_month = _div(awd_total, sales)
        # Q — Total Coverage FBA+AWD
        total_cov = _div(fba_total + awd_total, sales)
        # 美國倉 Month —— 純現貨（Q5=B：保留原本純現貨語意）
        us_month  = _div(us_qty, sales)
        # 美國虛擬 Month —— 現貨 + 欠數（含已下單，未來可用的美國庫存）
        us_virtual_month = _div(us_virtual, sales)
        # 深圳倉庫總量 Month —— (美國倉 + 佳樂倉) / 月銷量（Q4=B：不含欠數）
        sz_total_month = _div(sz_total, sales)

        # ── 返單邏輯 v3（含欠數）───────────────────────────────────────────
        # 安全庫存門檻 = 月銷量 × 移倉水位（美國倉應維持的量）
        safety_stock = round(sales * transfer_level) if sales > 0 else 0
        # 美國倉缺口 —— 用「美國虛擬庫存」計算（現貨 + 欠數都算）
        us_gap = max(0, safety_stock - int(us_virtual))
        # 移倉數建議 = min(佳樂倉庫存, 缺口)
        transfer_qty = min(int(jl_qty), us_gap) if sales > 0 else 0
        # 移倉抑制：若 Total Coverage(FBA+AWD) ≥ 觸發門檻 → 不建議移倉（避免降低周轉）
        transfer_suppressed_by_coverage = False
        _trigger = c.get("transfer_trigger_coverage", 4.0)
        if transfer_qty > 0 and total_cov is not None and total_cov >= _trigger:
            transfer_suppressed_by_coverage = True
            transfer_qty = 0
        # 建議返單數量 = 佳樂倉也不夠時，向工廠下的量
        reorder_qty = max(0, us_gap - int(jl_qty)) if sales > 0 else 0
        # 返單抑制：若 Total Coverage(FBA+AWD) ≥ 觸發門檻 → 不建議返單
        reorder_suppressed_by_coverage = False
        _reorder_trigger = c.get("reorder_trigger_coverage", 4.0)
        if reorder_qty > 0 and total_cov is not None and total_cov >= _reorder_trigger:
            reorder_suppressed_by_coverage = True
            reorder_qty = 0
        # 返單警示（三級，優先順序：🔴 > 🟡 > 🔵）
        reorder_alert = ""
        if sales > 0:
            if reorder_qty > 0:
                reorder_alert = "🔴 需返單"
            elif transfer_qty > 0 and int(jl_qty) - transfer_qty < safety_stock * 0.5:
                reorder_alert = "🟡 佳樂告急"
            elif pending_qty > 0:
                # 有欠數且不需再下單 → 已下單待收
                reorder_alert = "🔵 已下單待收"

        # 整箱返單量 —— ceil(建議返單量 ÷ 箱裝) × 箱裝
        case_reorder_qty = 0
        case_count = 0
        if reorder_qty > 0 and unit_case > 0:
            case_count = math.ceil(reorder_qty / unit_case)
            case_reorder_qty = case_count * unit_case

        # ── 交期與下單日 ─────────────────────────────────────────────────
        # 保守可撐天數 = (美國+佳樂) / (月銷/30)，「不含欠數」（欠數還沒到，防斷貨）
        days_can_last = None
        if sales > 0:
            days_can_last = round((us_qty + jl_qty) / (sales / 30), 1)
        # 交期倒數升級版：
        #  1) 若有下單日期（實際 PO 存在）→ 用 (約定交期日 − 今天) 真實倒數
        #  2) 否則若需返單 → 用 (可撐天數 − 生產交期) 估算
        #  3) 都沒 → None
        days_until_late = None
        suggested_order_date = None
        if order_date and pending_qty > 0:
            # 用真實 PO 倒數（負數 = 逾期天數）
            days_until_late = days_to_arrive
        elif days_can_last is not None and reorder_qty > 0:
            # 沒下單、但需要下單 → 估算
            days_until_late = round(days_can_last - PRODUCTION_DAYS)

        if reorder_qty > 0 and days_until_late is not None:
            offset_days = max(0, int(days_until_late))
            suggested_order_date = (date.today() + timedelta(days=offset_days)).isoformat()
        # 為了 dashboard 顯示，即使不需返單也保留 lead_time_days 相容欄位
        lead_time_days = int(PRODUCTION_DAYS)
        # S — 返單水位保持
        sz_reorder = round(sales * sz_level, 1) if sales > 0 else 0
        # V — Total Pipeline
        # Total Pipeline —— FBA total + AWD total + 美國倉（不含佳樂倉、不含欠數）
        # 佳樂倉是工廠端庫存，尚未進入銷售 pipeline，只算美國倉
        total_pipe = _div(fba_total + awd_total + us_qty, sales)
        # W — Immediate Coverage（只算現貨，不含在途）
        immed_cov  = _div(fba_avail + awd_avail, sales)

        # X — AWD 需補量
        awd_replen = None
        awd_replen_case_qty = 0     # 整箱返單量（依 Unit/Case 取整）
        awd_replen_case_cnt = 0     # 幾箱
        if sales > 0:
            need  = awd_target * sales - awd_total           # 達目標還差多少
            cap   = TOTAL_CAP_MONTHS * sales - (fba_total + awd_total)
            awd_replen = max(0, round(min(need, cap)))
            if awd_replen > 0 and unit_case > 0:
                awd_replen_case_cnt = math.ceil(awd_replen / unit_case)
                awd_replen_case_qty = awd_replen_case_cnt * unit_case

        # Y — SZ 可出量（扣除返單水位後剩餘）— 用 SZ 總量計算
        sz_avail = max(0, round(sz_total - sz_reorder)) if sales > 0 else 0

        # Z — 建議海運量（整箱取整）
        sea_qty = 0
        if awd_replen and sz_avail and unit_case:
            sea_qty = math.ceil(min(awd_replen, sz_avail) / unit_case) * unit_case

        # AA — 空運警示
        # 空運警示分兩級：
        #   Total Coverage < 1.0            → 🔴 立即空運（快斷）
        #   1.0 ≤ Coverage < AIR_THRESHOLD  → 🟠 評估空運（還來得及決定）
        air_alert = ""
        if sales > 0 and total_cov is not None:
            if total_cov < 1.0:
                air_alert = "🔴 立即空運"
            elif total_cov < AIR_THRESHOLD:
                air_alert = "🟠 評估空運"

        # AB — 3M Cap 警示
        cap_alert = ""
        if total_cov is not None:
            if total_cov > TOTAL_CAP_MONTHS:
                cap_alert = "🔴 超過上限"
            elif total_cov > TOTAL_CAP_MONTHS * 0.85:
                cap_alert = "🟡 接近上限"
            else:
                cap_alert = "✅ OK"

        # AC — 庫存警示（主表是 product_catalog，確保斷貨 SKU 也顯示）
        stock_alert = ""
        fba_avail_i = int(fba_avail)
        awd_avail_i = int(awd_avail)
        fba_total_i = int(fba_total)
        awd_total_i = int(round(awd_total))
        if fba_total_i == 0 and awd_total_i <= 0:
            stock_alert = "🔴 全通路斷貨"
        elif fba_avail_i == 0 and awd_avail_i == 0:
            stock_alert = "🟠 FBA+AWD 無現貨"
        elif fba_avail_i == 0:
            stock_alert = "🟡 FBA 斷貨"

        # ── 滯銷庫存 ─────────────────────────────────────────────────────────
        # 滯銷數量 = max(0, FBA Available + AWD Available − 年銷量)
        #   - FBA Available 沿用 dashboard 定義（可售 + FC 調撥 + FC 處理中）
        #   - 不含任何 inbound（只看已經在倉內的積壓）
        #   - 年銷量 = 近 30 天銷量 × 12（淡旺季會失真，已列入日後檢討）
        #   - AWD 只有 US 站，UK/DE 的 awd_avail 為 0
        # 滯銷金額 = 滯銷數量 × Anchor COG（美元）；COG = 0 代表已下架，金額即為 0
        # 屬於資訊欄位，Discontinued 也照常計算（停產品的積壓正是要看的）
        cog          = float(r.get("cog") or 0)
        annual_sales = sales * 12
        excess_qty   = max(0, int(round(fba_avail + awd_avail - annual_sales)))
        excess_value = round(excess_qty * cog, 2)

        # ── Discontinued（已停產）─── 全部歸零 action 欄位，只保留 informational ─
        # 邏輯：FBA + AWD 現有庫存賣完為止，不再返單/移倉/AWD 補貨/空運
        if prod_type == "Discontinued":
            awd_replen          = 0
            awd_replen_case_qty = 0
            awd_replen_case_cnt = 0
            sea_qty             = 0
            transfer_qty        = 0
            reorder_qty         = 0
            case_reorder_qty    = 0
            case_count          = 0
            air_alert           = ""
            cap_alert           = ""
            reorder_alert       = "⏸️ 已停產"
            suggested_order_date = None
            days_until_late     = None

        return {
            # A-E 識別
            "collection":   r["collection"],
            "parent_asin":  r["parent_asin"],
            "asin":         r["asin"],
            "sku":          r["sku"],
            "product_name": r["product_name"],
            # F-G 設定
            "product_type":      prod_type,
            "awd_target_months": awd_target,
            # H 銷售
            "sales_qty":    sales,
            # I-K FBA
            "fba_available": fba_avail,
            "fba_inbound":   fba_inbound,
            "fba_total":     fba_total,
            "fba_month":     fba_month,
            # L-P AWD
            "awd_available":  awd_avail,
            "awd_inbound":    awd_inbound,
            "awd_outbound":   awd_out,
            "awd_total":      round(awd_total),
            "awd_month":      awd_month,
            # Q Coverage
            "total_coverage": total_cov,
            # R-U SZ 兩倉 / 設定
            "safety_stock":      safety_stock,     # 安全庫存門檻（月銷量×移倉水位）
            "us_qty":            us_qty,           # 美國倉現貨
            "pending_qty":       pending_qty,      # 欠數（已下單未到）
            "order_date":              str(order_date) if order_date else None,   # 下單日期 (最新一筆)
            "expected_date":           str(expected_date) if expected_date else None,  # 約定交期 = order + production_days
            "factory_confirmed_date":  str(factory_confirmed_date) if factory_confirmed_date else None,  # 工廠回覆交期
            "delivery_advance_days":   delivery_advance_days,  # 工廠 vs 約定的差 (正=提前, 負=延後)
            "overdue_days":            overdue_days,     # 逾期天數（>0 表示已逾期）
            "us_virtual":        us_virtual,       # 美國虛擬庫存 = 現貨 + 欠數
            "jl_qty":            jl_qty,           # 佳樂倉
            "sz_total":          sz_total,         # (美國+佳樂) 不含欠數
            "us_month":          us_month,         # 美國倉 Month（純現貨）
            "us_virtual_month":  us_virtual_month, # 美國虛擬 Month（含欠數）
            "sz_total_month":    sz_total_month,   # 深圳倉庫總量 Month（不含欠數）
            "transfer_qty":      transfer_qty,     # 移倉數建議（佳樂→美國，min 佳樂庫存）
            "transfer_suppressed_by_coverage": transfer_suppressed_by_coverage,
            "transfer_trigger_coverage": _trigger,
            "sz_transfer_level": transfer_level,   # 目前套用的移倉水位
            # 返單計畫（v4 增強）
            "reorder_qty":           reorder_qty,          # 建議返單數量（原始 units）
            "case_reorder_qty":      case_reorder_qty,     # 整箱返單量（依 Unit/Case 取整）
            "case_reorder_count":    case_count,           # 幾箱
            "reorder_alert":         reorder_alert,        # 🔴 需返單 / 🟡 佳樂告急 / 🔵 已下單 / ''
            "reorder_suppressed_by_coverage": reorder_suppressed_by_coverage,
            "reorder_trigger_coverage":       _reorder_trigger,
            "days_can_last":         days_can_last,        # 保守可撐天數（不含欠數）
            "days_until_late":       days_until_late,      # 交期倒數（可撐-交期，正/負）
            "suggested_order_date":  suggested_order_date, # 建議下單日 (YYYY-MM-DD)
            "lead_time_days":        lead_time_days,       # 生產交期（固定 production_days）
            # 相容舊欄位（部分 API 消費端仍使用）
            "sz_shippable":      sz_ship,          # = us_qty
            "sz_reorder_level":  sz_reorder,
            "eta":               r["eta"],
            "unit_per_case":     unit_case,
            # V-Z 補貨計算
            "total_pipeline":     total_pipe,
            "immediate_coverage": immed_cov,
            "awd_replen_qty":       awd_replen,
            "awd_replen_case_qty":  awd_replen_case_qty,   # 整箱返單量
            "awd_replen_case_cnt":  awd_replen_case_cnt,   # 幾箱
            "sz_available_qty":   sz_avail,
            "sea_shipment_qty":   sea_qty,
            # AA-AC 警示
            "air_alert":   air_alert,
            "cap_alert":   cap_alert,
            "stock_alert": stock_alert,
            # 滯銷庫存（金額為美元）
            "cog":           cog,
            "excess_qty":    excess_qty,
            "excess_value":  excess_value,
            # 資料時間
            "awd_synced_at": str(r["awd_synced_at"]) if r["awd_synced_at"] else None,
            "sz_synced_at":  str(r["sz_synced_at"])  if r["sz_synced_at"]  else None,
            # 多 SKU 提示：sku_count > 1 表示這 ASIN 底下有多個 seller SKU（例：新舊 SKU、-stickerless、Amazon.Found 等）
            "sku_count":     int(r.get("sku_count") or 1),
            "all_skus":      r.get("all_skus") or r["sku"],
        }

    # ── SKU Config CRUD ───────────────────────────────────────────────────────

    async def upsert_sku_config(self, configs: list[dict]) -> int:
        """產品類型 / 箱規 / ETA 存在 asin_config（以 Child ASIN 為 key）。
        前端應傳 asin；若只傳 sku，透過 Anchor 換算成 ASIN。"""
        now = datetime.now(tz=timezone.utc)

        def _write():
            conn = new_conn()
            try:
                sku2asin = dict(conn.execute(
                    "SELECT sku, asin FROM product_catalog WHERE asin != ''"
                ).fetchall())
                rows = []
                for c in configs:
                    asin = (c.get("asin") or sku2asin.get(c.get("sku") or "") or "").strip()
                    if not asin:
                        continue
                    rows.append((asin, c.get("product_type", ""), c.get("unit_per_case", 1),
                                 c.get("eta", ""), now))
                conn.executemany("""
                    INSERT OR REPLACE INTO asin_config
                        (asin, product_type, unit_per_case, eta, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                """, rows)
                conn.commit()
                return len(rows)
            finally:
                conn.close()

        return await db_write(_write)

    # ── Unmatched SKUs（FBA/AWD 有但 Anchor 沒有）────────────────────────────────

    async def get_unmatched_skus(self) -> dict:
        """
        找出 FBA inventory 或 AWD inventory 中有、但 product_catalog 裡沒有的 SKU。
        這些 SKU 代表 Anchor 尚未建立紀錄，需要人工確認。
        """
        def _query():
            conn = new_conn()
            try:
                # 以 Child ASIN 判斷：Amazon 上有庫存、但 Anchor 沒有這個 ASIN
                # （SKU 是內部編碼，不能拿來判斷是否已登記）
                fba_rows = conn.execute("""
                    WITH latest_per_mp AS (
                        SELECT marketplace_id, MAX(snapshot_date) AS max_date
                        FROM inventory GROUP BY marketplace_id
                    ),
                    per_mp AS (   -- 每站每 ASIN 一個數字（同 ASIN 各 SKU 回報相同庫存，取 MAX）
                        SELECT i.marketplace_id, i.asin,
                               ANY_VALUE(i.sku) AS sku, ANY_VALUE(i.product_name) AS product_name,
                               MAX(i.fulfillable_quantity) AS fba_available,
                               MAX(i.inbound_working + i.inbound_shipped + i.inbound_receiving) AS fba_inbound
                        FROM inventory i
                        JOIN latest_per_mp lmp
                          ON i.marketplace_id = lmp.marketplace_id AND i.snapshot_date = lmp.max_date
                        WHERE i.asin IS NOT NULL AND i.asin != ''
                          AND NOT regexp_matches(i.sku, '^[A-Z]{2}-[A-Z0-9]{4}-[A-Z0-9]{4}$')
                        GROUP BY i.marketplace_id, i.asin
                    )
                    SELECT ANY_VALUE(sku), asin, ANY_VALUE(product_name),
                           SUM(fba_available), SUM(fba_inbound)
                    FROM per_mp
                    WHERE asin NOT IN (SELECT asin FROM product_catalog WHERE asin != '')
                    GROUP BY asin
                    HAVING SUM(fba_available) + SUM(fba_inbound) > 0
                    ORDER BY asin
                """).fetchall()

                # AWD：ASIN 來自 AWD Report，或 Amazon 自己的 SKU↔ASIN
                awd_rows = conn.execute("""
                    WITH m AS (
                        SELECT sku, arg_max(asin, snapshot_date) AS asin FROM inventory
                        WHERE asin IS NOT NULL AND asin != '' GROUP BY sku
                    )
                    SELECT a.sku, a.awd_available, a.awd_inbound, a.awd_outbound,
                           COALESCE(NULLIF(a.asin, ''), m.asin, '') AS asin
                    FROM awd_inventory a LEFT JOIN m ON a.sku = m.sku
                    WHERE (a.awd_available > 0 OR a.awd_inbound > 0)
                      AND COALESCE(NULLIF(a.asin, ''), m.asin, '') NOT IN
                          (SELECT asin FROM product_catalog WHERE asin != '')
                    ORDER BY 5
                """).fetchall()

                fba_unmatched = [
                    {"sku": r[0], "asin": r[1] or "", "product_name": r[2] or "",
                     "fba_available": r[3] or 0, "fba_inbound": r[4] or 0}
                    for r in fba_rows
                ]
                awd_unmatched = [
                    {"sku": r[0], "asin": r[4] or "", "awd_available": r[1] or 0,
                     "awd_inbound": r[2] or 0, "awd_outbound": r[3] or 0}
                    for r in awd_rows
                ]
                return {
                    "fba_unmatched": fba_unmatched,
                    "awd_unmatched": awd_unmatched,
                    "fba_count": len(fba_unmatched),
                    "awd_count": len(awd_unmatched),
                }
            finally:
                conn.close()

        return await asyncio.to_thread(_query)

    # ── Daily Alert（供 n8n 每日通知）────────────────────────────────────────────

    async def get_daily_alert(self, marketplace_id: str = "ATVPDKIKX0DER") -> dict:
        """
        整合：
        1. stock_alert 不為空的 SKU（斷貨警示）
        2. FBA/AWD 裡有但 Anchor 沒有的 SKU（未對應警示）
        適合 n8n 每日定時觸發後發送 email。
        """
        table, unmatched = await asyncio.gather(
            self.get_table(marketplace_id),
            self.get_unmatched_skus(),
        )

        stock_alerts = [
            {
                "sku":          r["sku"],
                "collection":   r["collection"],
                "product_name": r["product_name"],
                "stock_alert":  r["stock_alert"],
                "fba_available":   r.get("fba_available", 0),
                "awd_available":   r["awd_available"],
                "sales_qty":       r["sales_qty"],
            }
            for r in table if r.get("stock_alert")
        ]

        return {
            "date": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"),
            "marketplace_id": marketplace_id,
            "stock_alerts": stock_alerts,
            "stock_alert_count": len(stock_alerts),
            "unmatched_skus": unmatched,
            "has_issues": len(stock_alerts) > 0 or unmatched["fba_count"] > 0 or unmatched["awd_count"] > 0,
        }
