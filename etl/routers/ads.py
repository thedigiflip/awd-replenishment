"""
Ads ETL Router
POST /etl/ads/sync  — 觸發廣告報表同步（Sponsored Products 每日彙總）
GET  /etl/ads/status
"""

from datetime import date, timedelta
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel
import structlog

from services.ads_service import AdsService

router = APIRouter()
log = structlog.get_logger()


class AdsSyncRequest(BaseModel):
    report_date: date | None = None              # 預設昨天（廣告資料通常 D+1）
    lookback_days: int = 1
    report_type: str = "spAdvertisedProduct"     # spAdvertisedProduct | spCampaign | sbCampaign


class SyncResponse(BaseModel):
    run_id: int
    status: str
    message: str


@router.post("/sync", response_model=SyncResponse, summary="觸發廣告報表同步")
async def sync_ads(req: AdsSyncRequest, background_tasks: BackgroundTasks):
    # 廣告資料通常在 D+1 才完整，預設抓昨天
    report_date = req.report_date or (date.today() - timedelta(days=req.lookback_days))
    service = AdsService()
    run_id = await service.start_run(report_date=report_date, report_type=req.report_type)

    background_tasks.add_task(
        service.run,
        run_id=run_id,
        report_date=report_date,
        report_type=req.report_type,
    )

    log.info("ads.sync_triggered", run_id=run_id, report_date=str(report_date))
    return SyncResponse(run_id=run_id, status="running", message="Ads sync started")


@router.get("/status", summary="查詢最近廣告同步狀態")
async def get_status():
    """回傳最新一次執行紀錄（單一物件，供 n8n Success? 節點判斷用）。"""
    service = AdsService()
    return await service.get_latest_run(pipeline="ads")


@router.get("/history", summary="查詢最近 N 次廣告同步紀錄")
async def get_history(limit: int = 5):
    service = AdsService()
    return await service.get_recent_runs(pipeline="ads", limit=limit)


@router.get("/count", summary="查詢廣告資料筆數")
async def get_count():
    service = AdsService()
    return await service.get_count()
