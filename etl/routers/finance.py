"""
Finance ETL Router
POST /etl/finance/sync  — 同步財務事件（Settlement / Refunds / Fees）
GET  /etl/finance/status
"""

from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel
import structlog

from services.finance_service import FinanceService

router = APIRouter()
log = structlog.get_logger()


class FinanceSyncRequest(BaseModel):
    posted_after:  datetime | None = None    # 預設 30 天前
    posted_before: datetime | None = None
    lookback_days: int = 30


class SyncResponse(BaseModel):
    run_id: int
    status: str
    message: str
    params: dict


@router.post("/sync", response_model=SyncResponse, summary="觸發財務事件同步")
async def sync_finance(req: FinanceSyncRequest, background_tasks: BackgroundTasks):
    service = FinanceService()

    # 防止重複執行：若上一次還在跑，直接回傳不重啟
    latest = await service.get_latest_run(pipeline="finance")
    if latest.get("status") == "running":
        log.info("finance.sync_skipped_already_running", run_id=latest["id"])
        return SyncResponse(
            run_id=latest["id"],
            status="running",
            message="Finance sync already in progress, skipping duplicate",
            params={},
        )

    now = datetime.now(tz=timezone.utc)
    posted_after  = req.posted_after  or (now - timedelta(days=req.lookback_days))
    posted_before = req.posted_before or (now - timedelta(minutes=5))

    run_id = await service.start_run(posted_after=posted_after, posted_before=posted_before)

    background_tasks.add_task(
        service.run,
        run_id=run_id,
        posted_after=posted_after,
        posted_before=posted_before,
    )

    log.info("finance.sync_triggered", run_id=run_id)
    return SyncResponse(
        run_id=run_id,
        status="running",
        message="Finance sync started",
        params={
            "posted_after":  posted_after.isoformat(),
            "posted_before": posted_before.isoformat(),
        },
    )


@router.get("/status", summary="查詢最近財務同步狀態")
async def get_status():
    """回傳最新一次執行紀錄（單一物件，供 n8n Success? 節點判斷用）。"""
    service = FinanceService()
    return await service.get_latest_run(pipeline="finance")


@router.get("/history", summary="查詢最近 N 次財務同步紀錄")
async def get_history(limit: int = 5):
    service = FinanceService()
    return await service.get_recent_runs(pipeline="finance", limit=limit)


@router.get("/count", summary="查詢財務事件筆數")
async def get_count():
    service = FinanceService()
    return await service.get_count()
