"""
Shopify Products Router
同步 Shopify 產品目錄到 shop_products
"""
from fastapi import APIRouter
from core.database import get_db

router = APIRouter(prefix="/shopify/products", tags=["shopify"])


@router.post("/sync")
async def sync_products():
    """觸發 Shopify 產品同步"""
    # TODO: 呼叫 ShopifyProductsService.sync()
    return {"status": "queued"}
