"""
Orders ETL Service
- Calls SP-API getOrders → getOrderItems
- Upserts into DuckDB orders + order_items tables
- Uses tenacity for rate-limit retries
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from sp_api.base import SellingApiException

from core.config import settings
from core.database import new_conn, db_write
from core.sp_api_client import get_orders_api
from services.base_service import BaseService

log = structlog.get_logger()


class OrdersService(BaseService):
    pipeline = "orders"

    async def run(
        self,
        run_id: int,
        created_after: datetime,
        created_before: datetime,
        order_statuses: list[str],
    ) -> None:
        log.info("orders.run_start", run_id=run_id, marketplaces=settings.SP_API_MARKETPLACE_IDS)
        total_rows = 0
        try:
            for marketplace_id in settings.SP_API_MARKETPLACE_IDS:
                log.info("orders.marketplace_start", marketplace_id=marketplace_id)
                orders = await self._fetch_all_orders(
                    marketplace_id, created_after, created_before, order_statuses
                )
                total_rows += await self._upsert_orders(orders, marketplace_id)
                total_rows += await self._fetch_and_upsert_items(orders, marketplace_id)
            await self.finish_run(run_id, total_rows)
            log.info("orders.run_done", run_id=run_id, rows=total_rows)
        except Exception as e:
            log.error("orders.run_failed", run_id=run_id, error=str(e))
            await self.fail_run(run_id, str(e))
            raise

    # ─── SP-API Fetchers ──────────────────────────────────────────────────────

    async def _fetch_all_orders(
        self,
        marketplace_id: str,
        created_after: datetime,
        created_before: datetime,
        order_statuses: list[str],
    ) -> list[dict]:
        """Each SP-API call runs in a thread so it never blocks the event loop."""
        api = get_orders_api(marketplace_id)
        all_orders: list[dict] = []
        next_token: str | None = None

        while True:
            kwargs: dict[str, Any] = {
                "MarketplaceIds": [marketplace_id],
                "CreatedAfter":   created_after.strftime('%Y-%m-%dT%H:%M:%SZ'),
                "CreatedBefore":  created_before.strftime('%Y-%m-%dT%H:%M:%SZ'),
                "OrderStatuses":  order_statuses,
                "MaxResultsPerPage": settings.ETL_BATCH_SIZE,
            }
            if next_token:
                kwargs["NextToken"] = next_token

            resp = await asyncio.to_thread(api.get_orders, **kwargs)
            orders = resp.payload.get("Orders", [])
            all_orders.extend(orders)

            next_token = resp.payload.get("NextToken")
            log.info("orders.page_fetched", marketplace=marketplace_id, count=len(orders), has_next=bool(next_token))
            if not next_token:
                break

        log.info("orders.total_fetched", marketplace=marketplace_id, total=len(all_orders))
        return all_orders

    async def _fetch_order_items(self, order_id: str, marketplace_id: str) -> list[dict]:
        api = get_orders_api(marketplace_id)
        resp = await asyncio.to_thread(api.get_order_items, order_id=order_id)
        return resp.payload.get("OrderItems", [])

    # ─── DuckDB Writers ───────────────────────────────────────────────────────

    async def _upsert_orders(self, orders: list[dict], marketplace_id: str) -> int:
        if not orders:
            return 0

        now = datetime.now(tz=timezone.utc)
        rows = []
        for o in orders:
            total = o.get("OrderTotal", {})
            rows.append((
                o.get("AmazonOrderId"),
                o.get("PurchaseDate"),
                o.get("LastUpdateDate"),
                o.get("OrderStatus"),
                o.get("FulfillmentChannel"),
                o.get("SalesChannel"),
                float(total.get("Amount", 0)) if total.get("Amount") else None,
                total.get("CurrencyCode"),
                o.get("NumberOfItemsShipped", 0),
                o.get("NumberOfItemsUnshipped", 0),
                marketplace_id,
                json.dumps(o),
                now,
            ))

        def _write():
            conn = new_conn()
            try:
                conn.executemany("""
                    INSERT OR REPLACE INTO orders (
                        amazon_order_id, purchase_date, last_updated_date,
                        order_status, fulfillment_channel, sales_channel,
                        order_total_amount, order_total_currency,
                        number_of_items_shipped, number_of_items_unshipped,
                        marketplace_id, raw_json, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, rows)
                conn.commit()
                return len(rows)
            finally:
                conn.close()

        return await db_write(_write)

    async def _fetch_and_upsert_items(self, orders: list[dict], marketplace_id: str) -> int:
        now = datetime.now(tz=timezone.utc)
        all_item_rows = []

        for order in orders:
            order_id = order.get("AmazonOrderId")
            if not order_id:
                continue
            try:
                items = await self._fetch_order_items(order_id, marketplace_id)
                await asyncio.sleep(0.4)  # SP-API Order Items rate limit: 0.5 req/sec
            except Exception as e:
                log.warning("orders.items_fetch_failed", order_id=order_id, error=str(e))
                await asyncio.sleep(1.0)  # 失敗時多等一秒再繼續
                continue

            for item in items:
                price = item.get("ItemPrice", {})
                discount = item.get("PromotionDiscount", {})
                all_item_rows.append((
                    order_id,
                    item.get("OrderItemId"),
                    item.get("ASIN"),
                    item.get("SellerSKU"),
                    item.get("Title"),
                    item.get("QuantityOrdered", 0),
                    item.get("QuantityShipped", 0),
                    float(price.get("Amount", 0)) if price.get("Amount") else None,
                    price.get("CurrencyCode"),
                    float(discount.get("Amount", 0)) if discount.get("Amount") else None,
                    json.dumps(item),
                    now,
                ))

        if not all_item_rows:
            return 0

        def _write():
            conn = new_conn()
            try:
                conn.executemany("""
                    INSERT OR REPLACE INTO order_items (
                        amazon_order_id, order_item_id, asin, sku, title,
                        quantity_ordered, quantity_shipped,
                        item_price_amount, item_price_currency,
                        promotion_discount, raw_json, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, all_item_rows)
                conn.commit()
                return len(all_item_rows)
            finally:
                conn.close()

        return await db_write(_write)

    async def get_count(self) -> dict:
        def _query():
            conn = new_conn()
            try:
                orders_count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]  # type: ignore
                items_count  = conn.execute("SELECT COUNT(*) FROM order_items").fetchone()[0]  # type: ignore
                return {"orders": orders_count, "order_items": items_count}
            finally:
                conn.close()
        return await asyncio.to_thread(_query)
