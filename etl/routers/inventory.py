"""
Inventory ETL Router
POST /etl/inventory/sync  — 同步 FBA 庫存快照（每日執行）
GET  /etl/inventory/status
"""

from datetime import date
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel
import structlog

from services.inventory_service import InventoryService

router = APIRouter()
log = structlog.get_logger()


class InventorySyncRequest(BaseModel):
    snapshot_date: date | None = None       # 若不填則使用今天
    granularity: str = "Marketplace"        # Marketplace | ASIN
    start_datetime: str | None = None       # ISO8601, 增量同步用


class SyncResponse(BaseModel):
    run_id: int
    status: str
    message: str


@router.post("/sync", response_model=SyncResponse, summary="觸發庫存快照同步")
async def sync_inventory(req: InventorySyncRequest, background_tasks: BackgroundTasks):
    snapshot_date = req.snapshot_date or date.today()
    service = InventoryService()
    run_id = await service.start_run(snapshot_date=snapshot_date)

    background_tasks.add_task(
        service.run,
        run_id=run_id,
        snapshot_date=snapshot_date,
        granularity=req.granularity,
        start_datetime=req.start_datetime,
    )

    log.info("inventory.sync_triggered", run_id=run_id, snapshot_date=str(snapshot_date))
    return SyncResponse(run_id=run_id, status="running", message="Inventory sync started")


@router.get("/status", summary="查詢最近庫存同步狀態")
async def get_status():
    """回傳最新一次執行紀錄（單一物件，供 n8n Success? 節點判斷用）。"""
    service = InventoryService()
    return await service.get_latest_run(pipeline="inventory")


@router.get("/history", summary="查詢最近 N 次庫存同步紀錄")
async def get_history(limit: int = 5):
    service = InventoryService()
    return await service.get_recent_runs(pipeline="inventory", limit=limit)


@router.get("/count", summary="查詢 DuckDB 庫存筆數")
async def get_count():
    service = InventoryService()
    return await service.get_count()
