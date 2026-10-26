"""
跨境滯銷水位（Excess Inventory）Service

計算定義全部在 SQL view（core/warehouse_views.py → vw_gold_excess_inventory），
這裡只負責查詢、快照寫入與參數管理（ARCHITECTURE.md：SQL 做 join / 聚合 / 指標定義）。
"""

import asyncio
from datetime import datetime, timedelta, timezone

import structlog

from core.database import new_conn, db_write
from core.warehouse_views import DEFAULT_PARAMS

log = structlog.get_logger()

# 台灣無夏令時間，用固定 UTC+8（不依賴容器內的 tzdata）
TZ_TAIPEI = timezone(timedelta(hours=8))

# 明細表 / API 回傳欄位（順序 = 前端預設欄位順序）
ROW_FIELDS = [
    "parent_asin", "asin", "sku", "collection", "product_name", "product_type", "cog",
    "sales_us_30d", "sales_ca_30d", "sales_mx_30d", "sales_uk_30d", "sales_de_30d",
    "amazon_30d", "all_channel_30d", "all_channel_prev_30d", "annual_sales",
    "fba_us", "fba_uk", "fba_de", "fba_available", "awd_available", "us_qty",
    "stock_total", "excess_qty", "excess_value",
    "excess_in_us_wh", "excess_in_awd", "excess_in_fba",
    "months_of_supply", "supply_tier", "sales_trend_pct",
    "pending_qty", "fba_inbound", "awd_inbound", "replenishing_qty",
    "still_replenishing", "cog_missing", "jl_qty",
]

SNAPSHOT_FIELDS = [
    "asin", "collection", "cog", "fba_us", "fba_uk", "fba_de", "awd_available", "us_qty",
    "sales_us_30d", "sales_ca_30d", "sales_mx_30d", "sales_uk_30d", "sales_de_30d",
    "all_channel_30d", "annual_months", "stock_total", "excess_qty", "excess_value",
    "excess_in_us_wh", "excess_in_awd", "excess_in_fba", "pending_qty",
]

_PARAM_KEYS = {k for k, _, _ in DEFAULT_PARAMS}


def _num(v):
    """DuckDB Decimal → float（JSON 可序列化）"""
    if v is None:
        return None
    try:
        from decimal import Decimal
        if isinstance(v, Decimal):
            return float(v)
    except Exception:
        pass
    return v


class ExcessInventoryService:

    # ── 明細 + 摘要 ────────────────────────────────────────────────────────
    async def get_table(self) -> dict:
        return await asyncio.to_thread(self._get_table_sync)

    def _get_table_sync(self) -> dict:
        conn = new_conn()
        try:
            cols = ", ".join(ROW_FIELDS)
            rows = conn.execute(f"""
                SELECT {cols} FROM vw_gold_excess_inventory
                ORDER BY excess_value DESC, excess_qty DESC, collection, sku
            """).fetchall()
            data = [{k: _num(v) for k, v in zip(ROW_FIELDS, r)} for r in rows]

            s = conn.execute("""
                SELECT
                    SUM(excess_qty)                                           AS excess_qty,
                    SUM(excess_value)                                         AS excess_value,
                    COUNT(CASE WHEN excess_qty > 0 THEN 1 END)                AS excess_asins,
                    COUNT(CASE WHEN still_replenishing THEN 1 END)            AS still_replenishing,
                    COUNT(CASE WHEN supply_tier = '潛在滯銷 9–12M' THEN 1 END) AS potential_asins,
                    SUM(excess_in_us_wh * cog)                                AS value_us_wh,
                    SUM(excess_in_awd   * cog)                                AS value_awd,
                    SUM(excess_in_fba   * cog)                                AS value_fba,
                    SUM(excess_in_us_wh)                                      AS qty_us_wh,
                    SUM(excess_in_awd)                                        AS qty_awd,
                    SUM(excess_in_fba)                                        AS qty_fba,
                    SUM(stock_total)                                          AS stock_total,
                    SUM(stock_total * cog)                                    AS stock_value,
                    MAX(annual_months)                                        AS annual_months
                FROM vw_gold_excess_inventory
            """).fetchone()
            skeys = ["excess_qty", "excess_value", "excess_asins", "still_replenishing",
                     "potential_asins", "value_us_wh", "value_awd", "value_fba",
                     "qty_us_wh", "qty_awd", "qty_fba", "stock_total", "stock_value",
                     "annual_months"]
            summary = {k: _num(v) or 0 for k, v in zip(skeys, s)}

            # 資料新鮮度（讓使用者知道各來源更新到哪一天）
            fresh = conn.execute("""
                SELECT
                    (SELECT MAX(snapshot_date) FROM inventory WHERE marketplace_id = 'ATVPDKIKX0DER'),
                    (SELECT MAX(snapshot_date) FROM inventory WHERE marketplace_id = 'A1F83G8C2ARO7P'),
                    (SELECT MAX(snapshot_date) FROM inventory WHERE marketplace_id = 'A1PA6795UKMFR9'),
                    (SELECT MAX(synced_at) FROM awd_inventory),
                    (SELECT MAX(synced_at) FROM sz_warehouse),
                    (SELECT MAX(report_date) FROM sales_summary)
            """).fetchone()
            freshness = dict(zip(
                ["fba_us", "fba_uk", "fba_de", "awd", "us_warehouse", "sales"],
                [str(v)[:10] if v else None for v in fresh]))

            return {"count": len(data), "data": data, "summary": summary,
                    "params": self._get_params_sync(conn), "freshness": freshness}
        finally:
            conn.close()

    # ── 趨勢（快照）────────────────────────────────────────────────────────
    async def get_trend(self, grain: str = "month") -> dict:
        return await asyncio.to_thread(self._get_trend_sync, grain)

    def _get_trend_sync(self, grain: str) -> dict:
        conn = new_conn()
        try:
            if grain == "day":
                rows = conn.execute("""
                    SELECT snapshot_date, SUM(excess_value), SUM(excess_qty)
                    FROM fact_excess_inventory_daily
                    GROUP BY snapshot_date ORDER BY snapshot_date
                """).fetchall()
                series = [{"period": str(r[0]), "as_of": str(r[0]),
                           "excess_value": _num(r[1]), "excess_qty": _num(r[2])} for r in rows]
            else:
                # 每月取「當月最後一個快照日」的水位（月底水位；當月則為最新）
                rows = conn.execute("""
                    WITH last_day AS (
                        SELECT strftime(snapshot_date, '%Y-%m') AS ym, MAX(snapshot_date) AS d
                        FROM fact_excess_inventory_daily GROUP BY 1
                    )
                    SELECT l.ym, l.d, SUM(f.excess_value), SUM(f.excess_qty)
                    FROM last_day l
                    JOIN fact_excess_inventory_daily f ON f.snapshot_date = l.d
                    GROUP BY l.ym, l.d ORDER BY l.ym
                """).fetchall()
                series = [{"period": r[0], "as_of": str(r[1]),
                           "excess_value": _num(r[2]), "excess_qty": _num(r[3])} for r in rows]
            return {"grain": grain, "series": series, "params": self._get_params_sync(conn)}
        finally:
            conn.close()

    # ── 每日快照（n8n 每天呼叫；同一天重跑會覆蓋）──────────────────────────
    async def take_snapshot(self) -> dict:
        snap_date = datetime.now(TZ_TAIPEI).date()
        cols = ", ".join(SNAPSHOT_FIELDS)

        def _write():
            conn = new_conn()
            try:
                # DuckDB 限制：同一 transaction 內 DELETE 後再 INSERT 相同 PK 會違反約束，
                # 所以改用 INSERT OR REPLACE，再刪掉已不在 Anchor 的 ASIN（同一天重跑可覆蓋）
                conn.execute(f"""
                    INSERT OR REPLACE INTO fact_excess_inventory_daily
                        (snapshot_date, {cols}, synced_at)
                    SELECT ?::DATE, {cols}, now() FROM vw_gold_excess_inventory
                """, [snap_date])
                conn.execute("""
                    DELETE FROM fact_excess_inventory_daily
                    WHERE snapshot_date = ?
                      AND asin NOT IN (SELECT asin FROM dim_product)
                """, [snap_date])
                n, v, q = conn.execute("""
                    SELECT COUNT(*), SUM(excess_value), SUM(excess_qty)
                    FROM fact_excess_inventory_daily WHERE snapshot_date = ?
                """, [snap_date]).fetchone()
                return {"snapshot_date": str(snap_date), "rows": n,
                        "excess_value": _num(v), "excess_qty": _num(q)}
            finally:
                conn.close()

        result = await db_write(_write)
        log.info("excess.snapshot", **result)
        return result

    # ── 參數（Benchmark 等）────────────────────────────────────────────────
    async def get_params(self) -> dict:
        def _q():
            conn = new_conn()
            try:
                return self._get_params_sync(conn)
            finally:
                conn.close()
        return await asyncio.to_thread(_q)

    @staticmethod
    def _get_params_sync(conn) -> dict:
        rows = conn.execute("""
            SELECT key, value, label, updated_at FROM dim_params
            WHERE key LIKE 'excess_%' ORDER BY key
        """).fetchall()
        return {r[0]: {"value": r[1], "label": r[2], "updated_at": str(r[3])} for r in rows}

    async def update_params(self, body: dict[str, float]) -> dict:
        unknown = set(body) - _PARAM_KEYS
        if unknown:
            raise ValueError(f"未知參數: {sorted(unknown)}")
        for k, v in body.items():
            if v is None or float(v) < 0:
                raise ValueError(f"{k} 必須是 >= 0 的數字")
        if "excess_annual_months" in body and float(body["excess_annual_months"]) <= 0:
            raise ValueError("excess_annual_months 必須 > 0")

        def _write():
            conn = new_conn()
            try:
                for k, v in body.items():
                    conn.execute(
                        "UPDATE dim_params SET value = ?, updated_at = now() WHERE key = ?",
                        [float(v), k])
                return self._get_params_sync(conn)
            finally:
                conn.close()

        return await db_write(_write)
