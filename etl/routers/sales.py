"""
Sales Router — Reports API Pipeline
  POST /etl/sales/sync    → 觸發 Reports API 同步（背景執行，需等 5–15 分鐘）
  GET  /etl/sales/status  → 查詢最新一次執行狀態
  GET  /etl/sales/count   → 查詢 sales_summary 筆數
"""

from datetime import date, datetime, timezone
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field
import structlog

from core.config import settings
from core.database import new_conn, db_write
from services.sales_service import SalesService

router = APIRouter()
log    = structlog.get_logger()


class SalesSyncRequest(BaseModel):
    # None → loop 所有已設定的 marketplace（每站點各發一次 Report 請求）
    # 傳單一 marketplace_id → 只跑該站點（給手動 debug 用）
    marketplace_id: str | None = None
    days: int = 30   # 往回抓幾天的出貨報告


@router.post("/sync", summary="觸發 Sales Report 同步（背景執行；不指定則跑全站點）")
async def sync_sales(req: SalesSyncRequest, background_tasks: BackgroundTasks):
    """
    向 SP-API 提交 GET_FBA_FULFILLMENT_CUSTOMER_SHIPMENT_SALES_DATA 報告請求。
    - 若 marketplace_id 不傳 → 循環所有 SP_API_MARKETPLACE_IDS，每站點各發一次 Report
    - 若 marketplace_id 有傳 → 只跑該站點（相容舊 API）
    - Amazon 每 24 小時每個 (report type × marketplace) 只能跑一次；超過會 FATAL
    - 報告通常需要 5–15 分鐘生成
    """
    svc    = SalesService()
    run_id = await svc.start_run()

    if req.marketplace_id:
        background_tasks.add_task(
            svc.run,
            run_id=run_id,
            marketplace_id=req.marketplace_id,
            days=req.days,
        )
        log.info("sales.sync_triggered", run_id=run_id,
                 marketplace_id=req.marketplace_id, days=req.days, mode="single")
        return {
            "run_id":         run_id,
            "status":         "running",
            "message":        "Sales report requested. Check /etl/sales/status in 5–15 minutes.",
            "marketplace_id": req.marketplace_id,
            "days":           req.days,
        }

    # 沒指定 → 全站點 loop
    background_tasks.add_task(
        svc.run_all_marketplaces,
        run_id=run_id,
        days=req.days,
    )
    from core.config import settings
    mps = settings.SP_API_MARKETPLACE_IDS or ["ATVPDKIKX0DER"]
    log.info("sales.sync_triggered", run_id=run_id, marketplaces=mps, days=req.days, mode="all")
    return {
        "run_id":         run_id,
        "status":         "running",
        "message":        f"Sales report requested for {len(mps)} marketplaces. Check /etl/sales/status in 15–30 minutes.",
        "marketplaces":   mps,
        "days":           req.days,
    }


@router.get("/status", summary="查詢最新一次 Sales 同步狀態")
async def get_status():
    svc = SalesService()
    return await svc.get_latest_run(pipeline="sales")


@router.get("/history", summary="查詢最近 N 次 Sales 同步紀錄")
async def get_history(limit: int = 5):
    svc = SalesService()
    return await svc.get_recent_runs(pipeline="sales", limit=limit)


@router.get("/count", summary="查詢 sales_summary 筆數")
async def get_count():
    svc = SalesService()
    return await svc.get_count()


class SalesManualAdjustRequest(BaseModel):
    sku: str = Field(..., description="Seller SKU")
    asin: str = Field("", description="ASIN（可選）")
    report_date: date = Field(..., description="哪一天的銷量 YYYY-MM-DD")
    units_sold: int = Field(0, ge=0)
    revenue: float = Field(0, ge=0, description="營收 USD（可留 0）")
    marketplace_id: str = Field("ATVPDKIKX0DER")


@router.get("/marketplace-breakdown", summary="Diagnose: sales_summary 各 marketplace_id 分布")
async def sales_marketplace_breakdown(since_days: int = 30):
    """診斷用 — 看目前 sales_summary 內每個 marketplace_id 有多少筆資料。"""
    def _query():
        conn = new_conn()
        try:
            rows = conn.execute(f"""
                SELECT marketplace_id, COUNT(*) AS rows,
                       COUNT(DISTINCT sku) AS unique_sku,
                       SUM(units_sold) AS total_units,
                       MIN(report_date) AS earliest,
                       MAX(report_date) AS latest
                FROM sales_summary
                WHERE report_date >= CURRENT_DATE - INTERVAL '{since_days} DAYS'
                GROUP BY marketplace_id
                ORDER BY marketplace_id
            """).fetchall()
            return [
                {"marketplace_id": r[0], "rows": r[1], "unique_sku": r[2],
                 "total_units": r[3], "earliest": str(r[4]), "latest": str(r[5])}
                for r in rows
            ]
        finally:
            conn.close()
    import asyncio
    return await asyncio.to_thread(_query)


@router.post("/purge", summary="清掉指定 marketplace 的 sales_summary（供修正資料用）")
async def purge_sales(
    marketplace_id: str = "",
    since_days: int = 30,
):
    """
    清除 sales_summary 中指定 marketplace_id 過去 N 天的資料。
    - 若 marketplace_id 空 → 清所有
    - 用於資料混亂需要重新 sync 時
    """
    from datetime import date, timedelta
    cutoff = date.today() - timedelta(days=since_days)

    def _write():
        conn = new_conn()
        try:
            if marketplace_id:
                n = conn.execute(
                    "DELETE FROM sales_summary WHERE marketplace_id = ? AND report_date >= ?",
                    [marketplace_id, cutoff]
                ).fetchone()
            else:
                n = conn.execute(
                    "DELETE FROM sales_summary WHERE report_date >= ?",
                    [cutoff]
                ).fetchone()
            deleted = conn.execute("SELECT changes()").fetchone()[0] if hasattr(conn, 'changes') else 0
            conn.commit()
            return {"deleted_marketplace": marketplace_id or "ALL", "since": str(cutoff)}
        finally:
            conn.close()

    result = await db_write(_write)
    log.info("sales.purged", **result)
    return {"status": "ok", **result}


@router.post("/manual-adjust", summary="手動修正單一 SKU 某天的銷量")
async def sales_manual_adjust(body: SalesManualAdjustRequest):
    """
    Upsert `sales_summary` (report_date, sku, marketplace_id) 為主鍵。
    - 已存在 → 覆蓋 units_sold / revenue
    - 不存在 → 新增一筆
    """
    now = datetime.now(tz=timezone.utc)

    def _write():
        conn = new_conn()
        try:
            conn.execute("""
                INSERT INTO sales_summary
                    (report_date, sku, asin, units_sold, revenue, marketplace_id, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (report_date, sku, marketplace_id) DO UPDATE SET
                    units_sold = excluded.units_sold,
                    revenue    = excluded.revenue,
                    asin       = COALESCE(NULLIF(excluded.asin, ''), sales_summary.asin),
                    synced_at  = excluded.synced_at
            """, [body.report_date, body.sku, body.asin, body.units_sold,
                  body.revenue, body.marketplace_id, now])
            conn.commit()
            row = conn.execute("""
                SELECT sku, asin, units_sold, revenue, report_date
                FROM sales_summary
                WHERE report_date=? AND sku=? AND marketplace_id=?
            """, [body.report_date, body.sku, body.marketplace_id]).fetchone()
            return row
        finally:
            conn.close()

    row = await db_write(_write)
    log.info("sales.manual_adjust", sku=body.sku, date=str(body.report_date), units=body.units_sold)
    return {
        "status": "ok",
        "sku": row[0], "asin": row[1],
        "units_sold": row[2], "revenue": float(row[3] or 0),
        "report_date": str(row[4]),
    }
