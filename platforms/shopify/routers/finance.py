"""
Shopify Finance Router
同步 Shopify 每日財務彙整到 shop_finance_summary
"""
from fastapi import APIRouter

router = APIRouter(prefix="/shopify/finance", tags=["shopify"])


@router.post("/sync")
async def sync_finance():
    """觸發 Shopify 財務同步"""
    # TODO: 呼叫 ShopifyFinanceService.sync()
    return {"status": "queued"}
