"""
Shopify Finance Service
從訂單資料彙整每日財務摘要並寫入 shop_finance_summary

目標資料表：
  - shop_finance_summary
"""

CREATE_SHOP_FINANCE_SUMMARY = """
CREATE TABLE IF NOT EXISTS shop_finance_summary (
    date            DATE PRIMARY KEY,
    gross_sales     DECIMAL(14, 2),    -- 含折扣前總售價
    discounts       DECIMAL(14, 2),
    refunds         DECIMAL(14, 2),
    net_sales       DECIMAL(14, 2),    -- gross - discounts - refunds
    shipping        DECIMAL(14, 2),
    taxes           DECIMAL(14, 2),
    total_revenue   DECIMAL(14, 2),
    order_count     INTEGER,
    units_sold      INTEGER,
    synced_at       TIMESTAMP DEFAULT current_timestamp
)
"""


class ShopifyFinanceService:

    def ensure_tables(self):
        from core.database import get_db
        get_db().execute(CREATE_SHOP_FINANCE_SUMMARY)

    def sync(self, date_from: str = None, date_to: str = None):
        """
        從 shop_orders / shop_line_items 彙整每日財務摘要
        date_from / date_to: 'YYYY-MM-DD'
        """
        self.ensure_tables()
        # TODO: 彙整 SQL INSERT INTO shop_finance_summary ... SELECT FROM shop_orders
        raise NotImplementedError("Finance aggregation 尚未實作")
