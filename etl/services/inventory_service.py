"""
Inventory ETL Service
- Calls SP-API FBA Inventory Summaries (全量快照，每日一次)
- Upserts into DuckDB inventory table (PRIMARY KEY: snapshot_date + asin + sku)
"""

import asyncio
import json
from datetime import date, datetime, timezone

import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from sp_api.base import SellingApiException

from core.config import settings
from core.database import new_conn, db_write
from core.sp_api_client import get_inventory_api  # returns Inventories instance
from services.base_service import BaseService

log = structlog.get_logger()


class InventoryService(BaseService):
    pipeline = "inventory"

    async def run(
        self,
        run_id: int,
        snapshot_date: date,
        granularity: str = "Marketplace",
        start_datetime: str | None = None,
    ) -> None:
        log.info("inventory.run_start", run_id=run_id, snapshot_date=str(snapshot_date),
                 marketplaces=settings.SP_API_MARKETPLACE_IDS)
        total_rows = 0
        try:
            for marketplace_id in settings.SP_API_MARKETPLACE_IDS:
                log.info("inventory.marketplace_start", marketplace_id=marketplace_id)
                items = await self._fetch_all_inventory(marketplace_id, granularity, start_datetime)
                total_rows += await self._upsert_inventory(items, snapshot_date, marketplace_id)
            await self.finish_run(run_id, total_rows)
            log.info("inventory.run_done", run_id=run_id, rows=total_rows)
        except Exception as e:
            log.error("inventory.run_failed", run_id=run_id, error=str(e))
            await self.fail_run(run_id, str(e))
            raise

    async def _fetch_all_inventory(
        self,
        marketplace_id: str,
        granularity: str,
        start_datetime: str | None,
    ) -> list[dict]:
        """Each SP-API call runs in a thread so it never blocks the event loop."""
        api = get_inventory_api(marketplace_id)
        all_items: list[dict] = []
        next_token: str | None = None

        while True:
            kwargs: dict = {
                "details":         True,
                "granularityType": granularity,
                "granularityId":   marketplace_id,
                "marketplaceIds":  [marketplace_id],
            }
            if start_datetime:
                kwargs["startDateTime"] = start_datetime
            if next_token:
                kwargs["nextToken"] = next_token

            resp = await asyncio.to_thread(api.get_inventory_summary_marketplace, **kwargs)
            summaries = resp.payload.get("inventorySummaries", [])
            all_items.extend(summaries)

            # pagination 是回應的頂層欄位，不在 payload 裡
            pagination = getattr(resp, 'pagination', None) or {}
            if isinstance(pagination, str):
                import json as _json
                pagination = _json.loads(pagination)
            next_token = pagination.get('nextToken') if isinstance(pagination, dict) else None
            log.info("inventory.page_fetched", marketplace=marketplace_id, count=len(summaries), has_next=bool(next_token))
            if not next_token:
                break

        log.info("inventory.total_fetched", marketplace=marketplace_id, total=len(all_items))
        return all_items

    async def _upsert_inventory(self, items: list[dict], snapshot_date: date, marketplace_id: str) -> int:
        if not items:
            return 0

        now = datetime.now(tz=timezone.utc)
        rows = []
        for item in items:
            asin = item.get("asin") or ""
            sku  = item.get("sellerSku") or ""
            if not asin and not sku:
                continue  # skip items with no identifiers
            qty = item.get("inventoryDetails", {}) or {}
            reserved = qty.get("reservedQuantity", {}) or {}
            rows.append((
                snapshot_date,
                asin,
                item.get("fnsku"),
                sku,
                item.get("productName"),
                item.get("condition"),
                qty.get("fulfillableQuantity", 0),          # 正確欄位：fulfillable_quantity
                qty.get("inboundWorkingQuantity", 0),
                qty.get("inboundShippedQuantity", 0),
                qty.get("inboundReceivingQuantity", 0),
                reserved.get("fcTransferQuantity", 0),       # reserved_fc_transfers
                reserved.get("fcProcessingQuantity", 0),     # reserved_fc_processing
                item.get("totalQuantity", 0),                # total_quantity（來自 API 頂層）
                marketplace_id,
                json.dumps(item),
                now,
            ))

        if not rows:
            return 0

        def _write():
            conn = new_conn()
            try:
                conn.executemany("""
                    INSERT OR REPLACE INTO inventory (
                        snapshot_date, asin, fnsku, sku, product_name,
                        condition, fulfillable_quantity,
                        inbound_working, inbound_shipped, inbound_receiving,
                        reserved_fc_transfers, reserved_fc_processing,
                        total_quantity, marketplace_id, raw_json, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, rows)
                conn.commit()
                return len(rows)
            finally:
                conn.close()

        return await db_write(_write)

    async def get_count(self) -> dict:
        def _query():
            conn = new_conn()
            try:
                count = conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]  # type: ignore
                latest = conn.execute(
                    "SELECT MAX(snapshot_date) FROM inventory"
                ).fetchone()[0]  # type: ignore
                return {"inventory_records": count, "latest_snapshot": str(latest)}
            finally:
                conn.close()
        return await asyncio.to_thread(_query)
