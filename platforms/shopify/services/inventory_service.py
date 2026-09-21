"""
Shopify Inventory Service
負責從 Shopify API 拉庫存水位並寫入 DuckDB

目標資料表：
  - shop_inventory
"""

CREATE_SHOP_INVENTORY = """
CREATE TABLE IF NOT EXISTS shop_inventory (
    inventory_item_id VARCHAR,
    variant_id        VARCHAR,
    sku               VARCHAR,
    location_id       VARCHAR,
    location_name     VARCHAR,
    available         INTEGER,
    updated_at        TIMESTAMP,
    synced_at         TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (inventory_item_id, location_id)
)
"""


class ShopifyInventoryService:

    def ensure_tables(self):
        from core.database import get_db
        get_db().execute(CREATE_SHOP_INVENTORY)

    def sync(self):
        """從 Shopify API 拉所有 location 的庫存並 upsert"""
        self.ensure_tables()
        # TODO: GET /admin/api/2024-01/inventory_levels.json
        raise NotImplementedError("Shopify API client 尚未實作")
