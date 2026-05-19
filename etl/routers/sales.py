"""
Sales Router — Reports API Pipeline
  POST /etl/sales/sync    → 觸發 Reports API 同步（背景執行，需等 5–15 分鐘）
  GET  /etl/sales/status  → 查詢最新一次執行狀態
  GET  /etl/sales/count   → 查詢 sales_summary 筆數
"""

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel
import structlog

from core.config import settings
from services.sales_service import SalesService

router = APIRouter()
log    = structlog.get_logger()


class SalesSyncRequest(BaseModel):
    marketplace_id: str = "ATVPDKIKX0DER"
    days: int = 30   # 往回抓幾天的出貨報告


@router.post("/sync", summary="觸發 Sales Report 同步（背景執行）")
async def sync_sales(req: SalesSyncRequest, background_tasks: BackgroundTasks):
    """
    向 SP-API 提交 GET_FLAT_FILE_ORDERS_DATA 報告請求。
    報告通常需要 5–15 分鐘生成，完成後自動解析並寫入 DuckDB sales_summary。
    立即回傳 run_id，用 /status 輪詢進度。
    """
    svc    = SalesService()
    run_id = await svc.start_run()

    background_tasks.add_task(
        svc.run,
        run_id=run_id,
        marketplace_id=req.marketplace_id,
        days=req.days,
    )

    log.info("sales.sync_triggered", run_id=run_id, marketplace_id=req.marketplace_id, days=req.days)
    return {
        "run_id":         run_id,
        "status":         "running",
        "message":        "Sales report requested. Check /etl/sales/status in 5–15 minutes.",
        "marketplace_id": req.marketplace_id,
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
