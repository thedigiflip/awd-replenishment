"""
Traffic Router — Sales & Traffic Report API
  POST /etl/traffic/sync    → 觸發報告同步（背景執行，需等 5–15 分鐘）
  GET  /etl/traffic/status  → 查詢最新一次執行狀態
  GET  /etl/traffic/history → 查詢最近 N 次執行紀錄
  GET  /etl/traffic/count   → 查詢 sales_traffic 筆數摘要
  GET  /etl/traffic/data    → Dashboard 用彙整資料（月趨勢 / 分 Collection / Top SKU / 明細）
"""

import asyncio
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel, Field
from typing import Optional
import structlog

from core.database import new_conn

from services.traffic_service import TrafficService, last_month_range, _generate_months, _month_to_range

router = APIRouter()
log    = structlog.get_logger()


class TrafficSyncRequest(BaseModel):
    marketplace_id: str = Field(
        default="ATVPDKIKX0DER",
        description="Amazon Marketplace ID（預設 US）",
    )
    data_start_date: Optional[str] = Field(
        default=None,
        description="報告起始日 YYYY-MM-DD，留空自動使用上個月第一天",
    )
    data_end_date: Optional[str] = Field(
        default=None,
        description="報告結束日 YYYY-MM-DD，留空自動使用上個月最後一天",
    )


@router.post("/sync", summary="觸發 Sales & Traffic Report 同步（背景執行）")
async def sync_traffic(req: TrafficSyncRequest, background_tasks: BackgroundTasks):
    """
    向 SP-API 提交 GET_SALES_AND_TRAFFIC_REPORT（by Child ASIN）。

    - 報告通常需要 5–15 分鐘生成，完成後自動解析並寫入 DuckDB sales_traffic。
    - 立即回傳 run_id，用 GET /etl/traffic/status 輪詢進度。
    - 若未指定日期，自動查詢上個完整月份。
    - 注意：每個 Marketplace 每天最多觸發 1 次報告請求。
    """
    # 日期預設：上個完整月份
    if req.data_start_date and req.data_end_date:
        start, end = req.data_start_date, req.data_end_date
    else:
        start, end = last_month_range()

    svc    = TrafficService()
    run_id = await svc.start_run()

    background_tasks.add_task(
        svc.run,
        run_id=run_id,
        marketplace_id=req.marketplace_id,
        data_start_date=start,
        data_end_date=end,
    )

    log.info(
        "traffic.sync_triggered",
        run_id=run_id,
        marketplace_id=req.marketplace_id,
        start=start,
        end=end,
    )
    return {
        "run_id":           run_id,
        "status":           "running",
        "message":          "Traffic report requested. Check /etl/traffic/status in 5–15 minutes.",
        "marketplace_id":   req.marketplace_id,
        "data_start_date":  start,
        "data_end_date":    end,
    }


@router.get("/status", summary="查詢最新一次 Traffic 同步狀態")
async def get_status():
    svc = TrafficService()
    return await svc.get_latest_run(pipeline="traffic")


@router.get("/history", summary="查詢最近 N 次 Traffic 同步紀錄")
async def get_history(limit: int = 5):
    svc = TrafficService()
    return await svc.get_recent_runs(pipeline="traffic", limit=limit)


@router.get("/count", summary="查詢 sales_traffic 筆數摘要")
async def get_count():
    svc = TrafficService()
    return await svc.get_count()


@router.get("/data", summary="Dashboard 彙整資料（月趨勢 / Collection / Top SKU / 明細）")
async def get_traffic_data():
    """
    回傳 sales_traffic 全部彙整資料，供前端 Dashboard 使用。
    包含：
      - monthly_trends   : 每月 sessions / cvr / units / revenue
      - by_collection    : 依 collection 加總
      - top_skus         : 依 revenue 排序的前 30 SKU
      - detail           : 每月 × SKU 明細（前端做月份篩選）
      - available_months : 已有資料的月份清單（降冪）
    """
    def _query():
        conn = new_conn()
        try:
            # ── 月趨勢 ──────────────────────────────────────────────────────
            monthly_rows = conn.execute("""
                SELECT
                    LEFT(CAST(data_start_date AS VARCHAR), 7)  AS month,
                    SUM(sessions)                              AS sessions,
                    SUM(page_views)                            AS page_views,
                    CASE WHEN SUM(sessions) > 0
                         THEN ROUND(
                             CAST(SUM(total_units_ordered) AS DOUBLE)
                             / SUM(sessions) * 100, 2)
                         ELSE 0 END                            AS cvr,
                    SUM(total_units_ordered)                   AS total_units,
                    ROUND(SUM(total_ordered_product_sales), 2) AS total_revenue
                FROM sales_traffic
                GROUP BY month
                ORDER BY month
            """).fetchall()

            # ── 依 Collection 加總 ──────────────────────────────────────────
            coll_rows = conn.execute("""
                SELECT
                    COALESCE(NULLIF(TRIM(collection), ''), 'Unknown') AS collection,
                    SUM(sessions)                                      AS sessions,
                    SUM(total_units_ordered)                           AS total_units,
                    ROUND(SUM(total_ordered_product_sales), 2)         AS total_revenue
                FROM sales_traffic
                GROUP BY collection
                ORDER BY total_revenue DESC
            """).fetchall()

            # ── Top 30 SKU（依 revenue）──────────────────────────────────────
            sku_rows = conn.execute("""
                SELECT
                    sku,
                    COALESCE(NULLIF(TRIM(collection), ''), 'Unknown') AS collection,
                    MAX(product_name)                                   AS product_name,
                    SUM(sessions)                                       AS sessions,
                    SUM(total_units_ordered)                            AS total_units,
                    ROUND(SUM(total_ordered_product_sales), 2)          AS total_revenue
                FROM sales_traffic
                GROUP BY sku, collection
                ORDER BY total_revenue DESC
                LIMIT 30
            """).fetchall()

            # ── 明細（每月 × SKU）──────────────────────────────────────────
            detail_rows = conn.execute("""
                SELECT
                    LEFT(CAST(data_start_date AS VARCHAR), 7)          AS month,
                    sku,
                    COALESCE(NULLIF(TRIM(collection), ''), 'Unknown')  AS collection,
                    product_name,
                    SUM(sessions)                                       AS sessions,
                    SUM(page_views)                                     AS page_views,
                    ROUND(SUM(total_ordered_product_sales), 2)          AS revenue,
                    SUM(total_units_ordered)                            AS units,
                    CASE WHEN SUM(sessions) > 0
                         THEN ROUND(
                             CAST(SUM(total_units_ordered) AS DOUBLE)
                             / SUM(sessions) * 100, 2)
                         ELSE 0 END                                     AS cvr
                FROM sales_traffic
                GROUP BY month, sku, collection, product_name
                ORDER BY month DESC, revenue DESC
            """).fetchall()

            # ── 可用月份（降冪）────────────────────────────────────────────
            month_rows = conn.execute("""
                SELECT DISTINCT LEFT(CAST(data_start_date AS VARCHAR), 7) AS month
                FROM sales_traffic
                ORDER BY month DESC
            """).fetchall()

            return {
                "monthly_trends": [
                    {
                        "month":         r[0],
                        "sessions":      r[1] or 0,
                        "page_views":    r[2] or 0,
                        "cvr":           float(r[3] or 0),
                        "total_units":   r[4] or 0,
                        "total_revenue": float(r[5] or 0),
                    }
                    for r in monthly_rows
                ],
                "by_collection": [
                    {
                        "collection":    r[0],
                        "sessions":      r[1] or 0,
                        "total_units":   r[2] or 0,
                        "total_revenue": float(r[3] or 0),
                    }
                    for r in coll_rows
                ],
                "top_skus": [
                    {
                        "sku":           r[0],
                        "collection":    r[1],
                        "product_name":  r[2],
                        "sessions":      r[3] or 0,
                        "total_units":   r[4] or 0,
                        "total_revenue": float(r[5] or 0),
                    }
                    for r in sku_rows
                ],
                "detail": [
                    {
                        "month":        r[0],
                        "sku":          r[1],
                        "collection":   r[2],
                        "product_name": r[3],
                        "sessions":     r[4] or 0,
                        "page_views":   r[5] or 0,
                        "revenue":      float(r[6] or 0),
                        "units":        r[7] or 0,
                        "cvr":          float(r[8] or 0),
                    }
                    for r in detail_rows
                ],
                "available_months": [r[0] for r in month_rows],
            }
        finally:
            conn.close()

    return await asyncio.to_thread(_query)


class BackfillRequest(BaseModel):
    marketplace_id: str = Field(
        default="ATVPDKIKX0DER",
        description="Amazon Marketplace ID（預設 US）",
    )
    start_month: str = Field(
        ...,
        description="起始月份 YYYY-MM，例如 2024-05",
        pattern=r"^\d{4}-\d{2}$",
    )
    end_month: Optional[str] = Field(
        default=None,
        description="結束月份 YYYY-MM，留空自動使用上個完整月份",
        pattern=r"^\d{4}-\d{2}$",
    )


@router.post("/backfill", summary="回補歷史資料（按月份逐一請求，背景執行）")
async def backfill_traffic(req: BackfillRequest, background_tasks: BackgroundTasks):
    """
    從 start_month 到 end_month，逐月向 SP-API 請求 Sales & Traffic Report。

    - 每個月建立獨立的 run_id，透過 GET /etl/traffic/history 追蹤各月進度。
    - 月份之間自動等待 10 秒（SP-API rate limit buffer）。
    - 預估時間：每月約 2–3 分鐘，24 個月約 1–1.5 小時。
    - Amazon 允許最多回推 2 年。
    """
    # 計算結束月份（預設上個完整月份）
    if req.end_month:
        end_m = req.end_month
    else:
        last_start, _ = last_month_range()
        end_m = last_start[:7]   # "YYYY-MM"

    months = _generate_months(req.start_month, end_m)

    if not months:
        return {
            "status":  "error",
            "message": f"start_month ({req.start_month}) 必須早於 end_month ({end_m})",
        }

    svc = TrafficService()
    background_tasks.add_task(
        svc.backfill,
        marketplace_id=req.marketplace_id,
        start_month=req.start_month,
        end_month=end_m,
    )

    log.info(
        "traffic.backfill_triggered",
        marketplace_id=req.marketplace_id,
        start_month=req.start_month,
        end_month=end_m,
        months_planned=len(months),
    )
    return {
        "status":         "running",
        "message":        f"Backfill started for {len(months)} months. Check /etl/traffic/history to monitor progress.",
        "marketplace_id": req.marketplace_id,
        "start_month":    req.start_month,
        "end_month":      end_m,
        "months_planned": len(months),
        "months":         months,
        "estimated_minutes": len(months) * 3,
    }
