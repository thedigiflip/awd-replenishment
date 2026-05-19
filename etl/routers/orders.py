"""
Orders ETL Router
POST /etl/orders/sync  — n8n 呼叫這個 endpoint 來觸發訂單資料同步
GET  /etl/orders/status — 查詢最後一次執行狀態
"""

from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel
import structlog

from core.config import settings
from services.orders_service import OrdersService

router = APIRouter()
log = structlog.get_logger()


class SyncRequest(BaseModel):
    """n8n 傳入的同步參數（全部選填，有預設值）"""
    lookback_days: int = settings.ORDERS_LOOKBACK_DAYS
    created_after: datetime | None = None   # 若指定則忽略 lookback_days
    created_before: datetime | None = None
    order_statuses: list[str] = ["Pending", "Unshipped", "PartiallyShipped", "Shipped", "Canceled", "Unfulfillable"]


class SyncResponse(BaseModel):
    run_id: int
    status: str
    message: str
    params: dict


@router.post("/sync", response_model=SyncResponse, summary="觸發訂單同步")
async def sync_orders(req: SyncRequest, background_tasks: BackgroundTasks):
    """
    由 n8n 排程呼叫。非同步執行 ETL，立即回傳 run_id。
    """
    now = datetime.now(tz=timezone.utc)
    created_after  = req.created_after  or (now - timedelta(days=req.lookback_days))
    created_before = req.created_before or (now - timedelta(minutes=5))  # SP-API requires at least 2 min before now

    service = OrdersService()
    run_id = await service.start_run(
        created_after=created_after,
        created_before=created_before,
        order_statuses=req.order_statuses,
    )

    background_tasks.add_task(
        service.run,
        run_id=run_id,
        created_after=created_after,
        created_before=created_before,
        order_statuses=req.order_statuses,
    )

    log.info("orders.sync_triggered", run_id=run_id)
    return SyncResponse(
        run_id=run_id,
        status="running",
        message="Orders sync started in background",
        params={
            "created_after":  created_after.isoformat(),
            "created_before": created_before.isoformat(),
            "order_statuses": req.order_statuses,
        },
    )


@router.get("/status", summary="查詢最近訂單同步狀態")
async def get_status():
    """回傳最新一次執行紀錄（單一物件，供 n8n Success? 節點判斷用）。"""
    service = OrdersService()
    return await service.get_latest_run(pipeline="orders")


@router.get("/history", summary="查詢最近 N 次訂單同步紀錄")
async def get_history(limit: int = 5):
    service = OrdersService()
    return await service.get_recent_runs(pipeline="orders", limit=limit)


@router.get("/count", summary="查詢 DuckDB 訂單筆數")
async def get_count():
    service = OrdersService()
    return await service.get_count()
