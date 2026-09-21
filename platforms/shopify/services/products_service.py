"""
Shopify Products Service
負責從 Shopify API 拉產品目錄並寫入 DuckDB

目標資料表：
  - shop_products  (產品主檔)
"""

CREATE_SHOP_PRODUCTS = """
CREATE TABLE IF NOT EXISTS shop_products (
    product_id      VARCHAR PRIMARY KEY,
    title           VARCHAR,
    vendor          VARCHAR,
    product_type    VARCHAR,
    status          VARCHAR,           -- active / archived / draft
    created_at      TIMESTAMP,
    updated_at      TIMESTAMP,
    synced_at       TIMESTAMP DEFAULT current_timestamp
)
"""

CREATE_SHOP_VARIANTS = """
CREATE TABLE IF NOT EXISTS shop_variants (
    variant_id      VARCHAR PRIMARY KEY,
    product_id      VARCHAR,
    sku             VARCHAR,
    title           VARCHAR,           -- 規格描述（顏色/尺寸）
    price           DECIMAL(12, 2),
    compare_at_price DECIMAL(12, 2),
    inventory_item_id VARCHAR,
    created_at      TIMESTAMP,
    updated_at      TIMESTAMP
)
"""


class ShopifyProductsService:

    def ensure_tables(self):
        from core.database import get_db
        db = get_db()
        db.execute(CREATE_SHOP_PRODUCTS)
        db.execute(CREATE_SHOP_VARIANTS)

    def sync(self):
        """從 Shopify API 拉產品並 upsert 進 DuckDB"""
        self.ensure_tables()
        # TODO: GET /admin/api/2024-01/products.json
        raise NotImplementedError("Shopify API client 尚未實作")
