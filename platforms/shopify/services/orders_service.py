"""
Shopify Orders Service
負責從 Shopify API 拉訂單資料並寫入 DuckDB

目標資料表：
  - shop_orders      (訂單主檔)
  - shop_line_items  (訂單明細，SKU 層級)
"""
import duckdb
from core.database import get_db


CREATE_SHOP_ORDERS = """
CREATE TABLE IF NOT EXISTS shop_orders (
    order_id        VARCHAR PRIMARY KEY,
    created_at      TIMESTAMP,
    updated_at      TIMESTAMP,
    status          VARCHAR,           -- open / closed / cancelled
    financial_status VARCHAR,          -- paid / refunded / partially_refunded
    fulfillment_status VARCHAR,        -- fulfilled / partial / null
    total_price     DECIMAL(12, 2),
    subtotal_price  DECIMAL(12, 2),
    total_discounts DECIMAL(12, 2),
    total_tax       DECIMAL(12, 2),
    currency        VARCHAR(8),
    channel         VARCHAR,           -- online_store / pos / draft_orders
    customer_id     VARCHAR,
    synced_at       TIMESTAMP DEFAULT current_timestamp
)
"""

CREATE_SHOP_LINE_ITEMS = """
CREATE TABLE IF NOT EXISTS shop_line_items (
    line_item_id    VARCHAR PRIMARY KEY,
    order_id        VARCHAR,
    product_id      VARCHAR,
    variant_id      VARCHAR,
    sku             VARCHAR,
    title           VARCHAR,
    quantity        INTEGER,
    price           DECIMAL(12, 2),
    total_discount  DECIMAL(12, 2),
    fulfillment_status VARCHAR,
    created_at      TIMESTAMP
)
"""


class ShopifyOrdersService:

    def __init__(self):
        self.db = get_db()

    def ensure_tables(self):
        self.db.execute(CREATE_SHOP_ORDERS)
        self.db.execute(CREATE_SHOP_LINE_ITEMS)

    def sync(self, since_date: str = None):
        """
        從 Shopify API 拉訂單並 upsert 進 DuckDB
        since_date: 'YYYY-MM-DD'，None 表示近 90 天
        """
        self.ensure_tables()
        # TODO: 初始化 Shopify API client（建議用 shopify-python-api 或 httpx 直接呼叫 REST）
        # TODO: 分頁拉取 GET /admin/api/2024-01/orders.json
        # TODO: upsert shop_orders / shop_line_items
        raise NotImplementedError("Shopify API client 尚未實作")
