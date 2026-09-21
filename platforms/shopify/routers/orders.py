"""
Shopify Orders Router
同步 Shopify 訂單資料到 shop_orders / shop_line_items
"""
from fastapi import APIRouter, BackgroundTasks
from core.database import get_db

router = APIRouter(prefix="/shopify/orders", tags=["shopify"])


@router.post("/sync")
async def sync_orders(background_tasks: BackgroundTasks):
    """觸發 Shopify 訂單同步"""
    # TODO: 呼叫 ShopifyOrdersService.sync()
    return {"status": "queued"}


@router.get("/status")
async def sync_status():
    """查詢最近一次同步狀態"""
    # TODO: 查詢 etl_state.db
    return {"status": "idle"}
