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
    marketplace_id: Optional[str] = Field(
        default=None,
        description="Amazon Marketplace ID；不傳 → loop 所有已設定的 marketplace",
    )
    data_start_date: Optional[str] = Field(
        default=None,
        description="報告起始日 YYYY-MM-DD，留空自動使用上個月第一天",
    )
    data_end_date: Optional[str] = Field(
        default=None,
        description="報告結束日 YYYY-MM-DD，留空自動使用上個月最後一天",
    )


async def _run_traffic_all_marketplaces(svc, run_id: int, start: str, end: str):
    """依序跑每個 marketplace 的 traffic sync；單站失敗不影響其他站。"""
    from core.config import settings
    mps = settings.SP_API_MARKETPLACE_IDS or ["ATVPDKIKX0DER"]
    results = []
    for mp in mps:
        try:
            n = await svc.run(run_id=run_id, marketplace_id=mp,
                              data_start_date=start, data_end_date=end)
            results.append({"marketplace_id": mp, "status": "ok", "rows": n})
        except Exception as e:
            log.error("traffic.marketplace_failed", marketplace_id=mp, error=str(e))
            results.append({"marketplace_id": mp, "status": "error", "error": str(e)[:200]})
    log.info("traffic.run_all_done", run_id=run_id, results=results)


@router.post("/sync", summary="觸發 Sales & Traffic Report 同步（不傳 marketplace_id → loop 全站）")
async def sync_traffic(req: TrafficSyncRequest, background_tasks: BackgroundTasks):
    """
    向 SP-API 提交 GET_SALES_AND_TRAFFIC_REPORT（by Child ASIN）。
    - marketplace_id 不傳 → loop 所有 SP_API_MARKETPLACE_IDS
    - marketplace_id 有傳 → 只跑該站點
    - 報告 5–15 分鐘完成，每 marketplace 每天最多 1 次
    """
    if req.data_start_date and req.data_end_date:
        start, end = req.data_start_date, req.data_end_date
    else:
        start, end = last_month_range()

    svc    = TrafficService()
    run_id = await svc.start_run()

    if req.marketplace_id:
        background_tasks.add_task(
            svc.run,
            run_id=run_id,
            marketplace_id=req.marketplace_id,
            data_start_date=start,
            data_end_date=end,
        )
        log.info("traffic.sync_triggered", run_id=run_id,
                 marketplace_id=req.marketplace_id, start=start, end=end, mode="single")
        return {
            "run_id":           run_id,
            "status":           "running",
            "message":          "Traffic report requested. Check /etl/traffic/status in 5–15 minutes.",
            "marketplace_id":   req.marketplace_id,
            "data_start_date":  start,
            "data_end_date":    end,
        }

    background_tasks.add_task(_run_traffic_all_marketplaces, svc, run_id, start, end)
    from core.config import settings
    mps = settings.SP_API_MARKETPLACE_IDS or ["ATVPDKIKX0DER"]
    log.info("traffic.sync_triggered", run_id=run_id, marketplaces=mps,
             start=start, end=end, mode="all")
    return {
        "run_id":           run_id,
        "status":           "running",
        "message":          f"Traffic report requested for {len(mps)} marketplaces.",
        "marketplaces":     mps,
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
                    LEFT(CAST(data_start_date AS VARCHAR), 7)           AS month,
                    SUM(sessions)                                        AS sessions,
                    SUM(page_views)                                      AS page_views,
                    CASE WHEN SUM(sessions) > 0
                         THEN ROUND(CAST(SUM(total_units_ordered) AS DOUBLE)
                              / SUM(sessions) * 100, 2)
                         ELSE 0 END                                      AS cvr,
                    SUM(total_units_ordered)                             AS total_units,
                    ROUND(SUM(total_ordered_product_sales), 2)           AS total_revenue
                FROM sales_traffic
                GROUP BY month
                ORDER BY month
            """).fetchall()

            # ── 依 Collection 加總（含 COG + FBA fee）────────────────────────
            coll_rows = conn.execute("""
                SELECT
                    COALESCE(NULLIF(TRIM(st.collection), ''), 'Unknown') AS coll,
                    SUM(st.sessions)                                      AS sessions,
                    SUM(st.total_units_ordered)                           AS total_units,
                    ROUND(SUM(st.total_ordered_product_sales), 2)         AS total_revenue,
                    ROUND(SUM(st.total_units_ordered * (
                        COALESCE(pc.cog, 0) + COALESCE(pc.fba_fee, 0)
                    )), 2)                                                 AS total_variable_cost,
                    ROUND(SUM(st.total_ordered_product_sales) * 0.15, 2)  AS total_referral_fee
                FROM sales_traffic st
                LEFT JOIN product_catalog pc ON st.child_asin = pc.asin
                GROUP BY coll
                ORDER BY total_revenue DESC
            """).fetchall()

            # ── Top SKU（含 COG、FBA fee、MSRP）──────────────────────────────
            sku_rows = conn.execute("""
                SELECT
                    st.sku,
                    COALESCE(NULLIF(TRIM(st.collection), ''), 'Unknown') AS collection,
                    MAX(st.product_name)                                   AS product_name,
                    SUM(st.sessions)                                       AS sessions,
                    SUM(st.total_units_ordered)                            AS total_units,
                    ROUND(SUM(st.total_ordered_product_sales), 2)          AS total_revenue,
                    MAX(COALESCE(pc.cog, 0))                               AS cog,
                    MAX(COALESCE(pc.fba_fee, 0))                           AS fba_fee,
                    ROUND(SUM(st.total_units_ordered * (
                        COALESCE(pc.cog, 0) + COALESCE(pc.fba_fee, 0)
                    )), 2)                                                  AS total_variable_cost,
                    ROUND(SUM(st.total_ordered_product_sales) * 0.15, 2)   AS total_referral_fee
                FROM sales_traffic st
                LEFT JOIN product_catalog pc ON st.child_asin = pc.asin
                WHERE st.sku != ''
                GROUP BY st.sku, st.collection
                ORDER BY total_revenue DESC
                LIMIT 50
            """).fetchall()

            # ── 明細（每月 × SKU，含 COG + FBA fee）──────────────────────────
            detail_rows = conn.execute("""
                SELECT
                    LEFT(CAST(st.data_start_date AS VARCHAR), 7)          AS month,
                    st.child_asin,
                    st.sku,
                    COALESCE(NULLIF(TRIM(st.collection), ''), 'Unknown')  AS collection,
                    st.product_name,
                    SUM(st.sessions)                                       AS sessions,
                    SUM(st.page_views)                                     AS page_views,
                    ROUND(SUM(st.total_ordered_product_sales), 2)          AS revenue,
                    SUM(st.total_units_ordered)                            AS units,
                    CASE WHEN SUM(st.sessions) > 0
                         THEN ROUND(CAST(SUM(st.total_units_ordered) AS DOUBLE)
                              / SUM(st.sessions) * 100, 2)
                         ELSE 0 END                                        AS cvr,
                    MAX(COALESCE(pc.cog, 0))                               AS cog,
                    MAX(COALESCE(pc.fba_fee, 0))                           AS fba_fee
                FROM sales_traffic st
                LEFT JOIN product_catalog pc ON st.child_asin = pc.asin
                GROUP BY month, st.child_asin, st.sku, st.collection, st.product_name
                ORDER BY month DESC, revenue DESC
            """).fetchall()

            # ── 可用月份（降冪）────────────────────────────────────────────
            month_rows = conn.execute("""
                SELECT DISTINCT LEFT(CAST(data_start_date AS VARCHAR), 7) AS month
                FROM sales_traffic
                ORDER BY month DESC
            """).fetchall()

            REFERRAL_RATE = 0.15  # 固定 15%

            def _msrp(revenue, units):
                return round(revenue / units, 2) if units > 0 else 0.0

            def _profit(revenue, var_cost, referral_fee):
                """Revenue - COG - FBA fee - Referral fee"""
                return round(revenue - var_cost - referral_fee, 2)

            def _margin(revenue, var_cost, referral_fee):
                profit = revenue - var_cost - referral_fee
                return round(profit / revenue * 100, 1) if revenue > 0 else 0.0

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
                        "collection":        r[0],
                        "sessions":          r[1] or 0,
                        "total_units":       r[2] or 0,
                        "total_revenue":     float(r[3] or 0),
                        "total_variable_cost": float(r[4] or 0),
                        "total_referral_fee":  float(r[5] or 0),
                        "total_cost":        float(r[4] or 0) + float(r[5] or 0),
                        "profit":            _profit(float(r[3] or 0), float(r[4] or 0), float(r[5] or 0)),
                        "margin":            _margin(float(r[3] or 0), float(r[4] or 0), float(r[5] or 0)),
                    }
                    for r in coll_rows
                ],
                "top_skus": [
                    {
                        "sku":              r[0],
                        "collection":       r[1],
                        "product_name":     r[2],
                        "sessions":         r[3] or 0,
                        "total_units":      r[4] or 0,
                        "total_revenue":    float(r[5] or 0),
                        "cog":              float(r[6] or 0),
                        "fba_fee":          float(r[7] or 0),
                        "total_variable_cost": float(r[8] or 0),
                        "total_referral_fee":  float(r[9] or 0),
                        "msrp":             _msrp(float(r[5] or 0), r[4] or 0),
                        "profit":           _profit(float(r[5] or 0), float(r[8] or 0), float(r[9] or 0)),
                        "margin":           _margin(float(r[5] or 0), float(r[8] or 0), float(r[9] or 0)),
                    }
                    for r in sku_rows
                ],
                "detail": [
                    {
                        "month":        r[0],
                        "child_asin":   r[1],
                        "sku":          r[2],
                        "collection":   r[3],
                        "product_name": r[4],
                        "sessions":     r[5] or 0,
                        "page_views":   r[6] or 0,
                        "revenue":      float(r[7] or 0),
                        "units":        r[8] or 0,
                        "cvr":          float(r[9] or 0),
                        "cog":          float(r[10] or 0),
                        "fba_fee":      float(r[11] or 0),
                        "msrp":         _msrp(float(r[7] or 0), r[8] or 0),
                        "profit":       _profit(
                            float(r[7] or 0),
                            (r[8] or 0) * (float(r[10] or 0) + float(r[11] or 0)),
                            float(r[7] or 0) * REFERRAL_RATE,
                        ),
                    }
                    for r in detail_rows
                ],
                "available_months": [r[0] for r in month_rows],
                "fee_config": {"referral_rate": REFERRAL_RATE},
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


@router.post("/sync-mtd", summary="同步當月 MTD 資料（當月 1 號 → 昨天，每天覆蓋）")
async def sync_mtd(background_tasks: BackgroundTasks,
                   marketplace_id: str = "ATVPDKIKX0DER"):
    """
    取得本月 Month-to-Date 資料（月初 → 昨天的累計值）。
    - 可每天執行，資料會直接覆蓋上次的 MTD，不會產生重複。
    - 建議透過 n8n 每天早上自動觸發。
    - 完成後當月資料會出現在 Dashboard 流量分析 Tab。
    """
    svc    = TrafficService()
    run_id = await svc.start_run()
    background_tasks.add_task(svc.run_mtd, run_id=run_id, marketplace_id=marketplace_id)
    log.info("traffic.mtd_triggered", run_id=run_id, marketplace_id=marketplace_id)
    return {
        "run_id":         run_id,
        "status":         "running",
        "message":        "MTD sync started. Check /etl/traffic/status in 5–15 minutes.",
        "marketplace_id": marketplace_id,
    }


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
