"""
Shopify Inventory Router
同步 Shopify 庫存水位到 shop_inventory
"""
from fastapi import APIRouter

router = APIRouter(prefix="/shopify/inventory", tags=["shopify"])


@router.post("/sync")
async def sync_inventory():
    """觸發 Shopify 庫存同步"""
    # TODO: 呼叫 ShopifyInventoryService.sync()
    return {"status": "queued"}
