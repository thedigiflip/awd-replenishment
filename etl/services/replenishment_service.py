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
    "production_days":   30.0,
    "sea_days":          40.0,
    "awd_target_normal": 1.0,
    "awd_target_hero":   1.5,
    "total_cap_months":  3.0,
    "sz_level_normal":   1.0,
    "sz_level_hero":     1.5,
}

_CONTROLS_LABELS = {
    "production_days":   "生產天數",
    "sea_days":          "海運天數",
    "awd_target_normal": "AWD 目標月數（一般款）",
    "awd_target_hero":   "AWD 目標月數（流量款）",
    "total_cap_months":  "FBA+AWD 總上限月數",
    "sz_level_normal":   "SZ 返單水位（一般款）",
    "sz_level_hero":     "SZ 返單水位（流量款）",
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
        conn = new_conn()
        try:
            rows = conn.execute("""
                WITH
                -- product_catalog 是主表（MAGEASY Anchor，確保所有 SKU 都顯示）
                -- FBA inventory（SP-API 同步後自動補入，空時 COALESCE 為 0）
                fba AS (
                    SELECT
                        sku,
                        asin,
                        product_name,
                        -- fba_available = 可出貨 + FC 間轉倉 + FC 內部處理（都已在 FBA 倉內）
                        SUM(fulfillable_quantity
                            + COALESCE(reserved_fc_transfers, 0)
                            + COALESCE(reserved_fc_processing, 0))                 AS fba_available,
                        SUM(inbound_working + inbound_shipped + inbound_receiving)  AS fba_inbound
                    FROM inventory
                    WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM inventory)
                      AND marketplace_id = ?
                    GROUP BY sku, asin, product_name
                ),
                -- Reports API 數據（確認出貨，授權後自動使用）
                sales_reports AS (
                    SELECT sku, SUM(units_sold) AS sales_30d
                    FROM sales_summary
                    WHERE report_date >= CURRENT_DATE - INTERVAL 30 DAYS
                      AND marketplace_id = ?
                    GROUP BY sku
                ),
                -- Fallback：order_items（含 Pending，Reports API 未授權時使用）
                sales_orders AS (
                    SELECT oi.sku, SUM(oi.quantity_ordered) AS sales_30d
                    FROM order_items oi
                    JOIN orders o ON oi.amazon_order_id = o.amazon_order_id
                    WHERE o.purchase_date >= NOW() - INTERVAL '30 days'
                      AND o.order_status NOT IN ('Canceled', 'Cancelled')
                      AND o.marketplace_id = ?
                    GROUP BY oi.sku
                ),
                sales AS (
                    -- Reports API 有資料優先用，否則用 order_items fallback
                    SELECT sku, sales_30d FROM sales_reports
                    WHERE (SELECT COUNT(*) FROM sales_reports) > 0
                    UNION ALL
                    SELECT sku, sales_30d FROM sales_orders
                    WHERE (SELECT COUNT(*) FROM sales_reports) = 0
                )
                SELECT
                    pc.sku,
                    COALESCE(pc.asin,        f.asin, '')          AS asin,
                    COALESCE(pc.parent_asin, '')                  AS parent_asin,
                    COALESCE(pc.collection,  '')                  AS collection,
                    COALESCE(pc.product_name, f.product_name, '') AS product_name,
                    COALESCE(sc.product_type, '')                 AS product_type,
                    COALESCE(sc.unit_per_case, 1)                 AS unit_per_case,
                    COALESCE(sc.eta, '')                          AS eta,
                    COALESCE(s.sales_30d, 0)                      AS sales_30d,
                    COALESCE(f.fba_available, 0)                  AS fba_available,
                    COALESCE(f.fba_inbound, 0)                    AS fba_inbound,
                    COALESCE(a.awd_available, 0)                  AS awd_available,
                    COALESCE(a.awd_inbound, 0)                    AS awd_inbound,
                    COALESCE(a.awd_outbound, 0)                   AS awd_outbound,
                    COALESCE(sz.shippable_qty, 0)                 AS sz_shippable,
                    a.synced_at                                   AS awd_synced_at,
                    sz.synced_at                                  AS sz_synced_at
                FROM product_catalog pc
                LEFT JOIN awd_inventory   a  ON pc.sku = a.sku
                LEFT JOIN fba             f  ON pc.sku = f.sku
                LEFT JOIN sales           s  ON pc.sku = s.sku
                LEFT JOIN sz_warehouse    sz ON pc.sku = sz.sku
                LEFT JOIN sku_config      sc ON pc.sku = sc.sku
                ORDER BY pc.collection NULLS LAST, pc.sku
            """, [marketplace_id, marketplace_id, marketplace_id]).fetchall()

            cols = [
                "sku", "asin", "parent_asin", "collection", "product_name",
                "product_type", "unit_per_case", "eta",
                "sales_30d", "fba_available", "fba_inbound",
                "awd_available", "awd_inbound", "awd_outbound",
                "sz_shippable", "awd_synced_at", "sz_synced_at",
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
        awd_out     = float(r["awd_outbound"])          # N
        awd_total   = awd_avail + awd_inbound - awd_out # O

        sz_ship     = float(r["sz_shippable"])          # R
        unit_case   = max(1, int(r["unit_per_case"]))   # U

        # G — AWD 目標月數（動態）
        awd_target = c["awd_target_hero"] if prod_type == "流量款" else c["awd_target_normal"]
        # S — 返單水位（動態）
        sz_level   = c["sz_level_hero"] if prod_type == "流量款" else c["sz_level_normal"]

        def _div(a, b, dec=2):
            return round(a / b, dec) if b and b > 0 else None

        # K — FBA Total Month
        fba_month = _div(fba_total, sales)
        # P — AWD Month
        awd_month = _div(awd_total, sales)
        # Q — Total Coverage FBA+AWD
        total_cov = _div(fba_total + awd_total, sales)
        # S — 返單水位保持
        sz_reorder = round(sales * sz_level, 1) if sales > 0 else 0
        # V — Total Pipeline
        total_pipe = _div(fba_total + awd_total + sz_ship, sales)
        # W — Immediate Coverage（只算現貨，不含在途）
        immed_cov  = _div(fba_avail + awd_avail, sales)

        # X — AWD 需補量
        awd_replen = None
        if sales > 0:
            need  = awd_target * sales - awd_total           # 達目標還差多少
            cap   = TOTAL_CAP_MONTHS * sales - (fba_total + awd_total)
            awd_replen = max(0, round(min(need, cap)))

        # Y — SZ 可出量（扣除返單水位後剩餘）
        sz_avail = max(0, round(sz_ship - sz_reorder)) if sales > 0 else 0

        # Z — 建議海運量（整箱取整）
        sea_qty = 0
        if awd_replen and sz_avail and unit_case:
            sea_qty = math.ceil(min(awd_replen, sz_avail) / unit_case) * unit_case

        # AA — 空運警示
        air_alert = ""
        if sales > 0 and total_cov is not None and total_cov < AIR_THRESHOLD:
            air_alert = "⚠️ 評估空運"

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
            # R-U SZ / 設定
            "sz_shippable":   sz_ship,
            "sz_reorder_level": sz_reorder,
            "eta":            r["eta"],
            "unit_per_case":  unit_case,
            # V-Z 補貨計算
            "total_pipeline":     total_pipe,
            "immediate_coverage": immed_cov,
            "awd_replen_qty":     awd_replen,
            "sz_available_qty":   sz_avail,
            "sea_shipment_qty":   sea_qty,
            # AA-AC 警示
            "air_alert":   air_alert,
            "cap_alert":   cap_alert,
            "stock_alert": stock_alert,
            # 資料時間
            "awd_synced_at": str(r["awd_synced_at"]) if r["awd_synced_at"] else None,
            "sz_synced_at":  str(r["sz_synced_at"])  if r["sz_synced_at"]  else None,
        }

    # ── SKU Config CRUD ───────────────────────────────────────────────────────

    async def upsert_sku_config(self, configs: list[dict]) -> int:
        now = datetime.now(tz=timezone.utc)
        rows = [
            (c["sku"], c.get("product_type", ""), c.get("unit_per_case", 1),
             c.get("eta", ""), now)
            for c in configs if c.get("sku")
        ]

        def _write():
            conn = new_conn()
            try:
                conn.executemany("""
                    INSERT OR REPLACE INTO sku_config
                        (sku, product_type, unit_per_case, eta, updated_at)
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
                # FBA SKUs not in product_catalog
                fba_rows = conn.execute("""
                    SELECT DISTINCT i.sku, i.asin, i.product_name,
                           SUM(i.fulfillable_quantity) AS fba_available,
                           SUM(i.inbound_working + i.inbound_shipped + i.inbound_receiving) AS fba_inbound
                    FROM inventory i
                    WHERE i.snapshot_date = (SELECT MAX(snapshot_date) FROM inventory)
                      AND i.sku IS NOT NULL AND i.sku != ''
                      AND NOT EXISTS (
                          SELECT 1 FROM product_catalog pc WHERE pc.sku = i.sku
                      )
                    GROUP BY i.sku, i.asin, i.product_name
                    ORDER BY i.sku
                """).fetchall()

                # AWD SKUs not in product_catalog
                awd_rows = conn.execute("""
                    SELECT a.sku, a.awd_available, a.awd_inbound, a.awd_outbound
                    FROM awd_inventory a
                    WHERE a.sku IS NOT NULL AND a.sku != ''
                      AND NOT EXISTS (
                          SELECT 1 FROM product_catalog pc WHERE pc.sku = a.sku
                      )
                    ORDER BY a.sku
                """).fetchall()

                fba_unmatched = [
                    {"sku": r[0], "asin": r[1] or "", "product_name": r[2] or "",
                     "fba_available": r[3] or 0, "fba_inbound": r[4] or 0}
                    for r in fba_rows
                ]
                awd_unmatched = [
                    {"sku": r[0], "awd_available": r[1] or 0,
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
