"""
Finance ETL Service
- Calls SP-API listFinancialEvents（分頁抓取結算事件）
- Covers: ShipmentEvents, RefundEvents, ServiceFeeEvents, AdjustmentEvents
- Flattens all event types into a unified finance_events table
"""

import asyncio
import json
from datetime import datetime, timezone

import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from sp_api.base import SellingApiException

from core.config import settings
from core.database import new_conn, db_write
from core.sp_api_client import get_finance_api
from services.base_service import BaseService

log = structlog.get_logger()


class FinanceService(BaseService):
    pipeline = "finance"

    async def run(
        self,
        run_id: int,
        posted_after: datetime,
        posted_before: datetime,
    ) -> None:
        log.info("finance.run_start", run_id=run_id, marketplaces=settings.SP_API_MARKETPLACE_IDS)
        total_rows = 0
        try:
            for marketplace_id in settings.SP_API_MARKETPLACE_IDS:
                log.info("finance.marketplace_start", marketplace_id=marketplace_id)
                events = await self._fetch_all_events(marketplace_id, posted_after, posted_before)
                total_rows += await self._upsert_events(events, marketplace_id)
            await self.finish_run(run_id, total_rows)
            log.info("finance.run_done", run_id=run_id, rows=total_rows)
        except Exception as e:
            log.error("finance.run_failed", run_id=run_id, error=str(e))
            await self.fail_run(run_id, str(e))
            raise

    async def _fetch_all_events(
        self,
        marketplace_id: str,
        posted_after: datetime,
        posted_before: datetime,
    ) -> list[dict]:
        """Fetch all pages of financial events. Each SP-API call runs in a thread
        so it never blocks the async event loop."""
        api = get_finance_api(marketplace_id)
        all_events: list[dict] = []
        next_token: str | None = None

        while True:
            kwargs: dict = {
                "PostedAfter":  posted_after.strftime('%Y-%m-%dT%H:%M:%SZ'),
                "PostedBefore": posted_before.strftime('%Y-%m-%dT%H:%M:%SZ'),
            }
            if next_token:
                kwargs["NextToken"] = next_token

            # SP-API call is synchronous/blocking — run in thread pool
            resp = await asyncio.to_thread(api.list_financial_events, **kwargs)
            payload = resp.payload.get("FinancialEvents", {})
            batch = self._flatten_events(payload)
            all_events.extend(batch)

            next_token = resp.payload.get("NextToken")
            log.info("finance.page_fetched", marketplace=marketplace_id, count=len(batch), has_next=bool(next_token))
            if not next_token:
                break

        log.info("finance.total_fetched", marketplace=marketplace_id, total=len(all_events))
        return all_events

    def _flatten_events(self, financial_events: dict) -> list[dict]:
        """
        Flatten the nested SP-API FinancialEvents structure into
        a list of uniform dicts ready for DuckDB insertion.
        """
        flat: list[dict] = []

        # ── Shipment Events ──────────────────────────────────────────────────
        for event in financial_events.get("ShipmentEventList", []):
            order_id = event.get("AmazonOrderId", "")
            for item in event.get("ShipmentItemList", []):
                for charge in item.get("ItemChargeList", []):
                    amt = charge.get("ChargeAmount", {})
                    flat.append({
                        "event_id":    f"ship_{order_id}_{item.get('OrderItemId')}_{charge.get('ChargeType')}",
                        "posted_date": event.get("PostedDate"),
                        "event_type":  f"Shipment_{charge.get('ChargeType')}",
                        "amount":      float(amt.get("CurrencyAmount", 0)),
                        "currency":    amt.get("CurrencyCode"),
                        "order_id":    order_id,
                        "description": charge.get("ChargeType"),
                        "raw":         event,
                    })

        # ── Refund Events ────────────────────────────────────────────────────
        for event in financial_events.get("RefundEventList", []):
            order_id = event.get("AmazonOrderId", "")
            for item in event.get("ShipmentItemAdjustmentList", []):
                for charge in item.get("ItemChargeAdjustmentList", []):
                    amt = charge.get("ChargeAmount", {})
                    flat.append({
                        "event_id":    f"refund_{order_id}_{item.get('OrderItemId')}_{charge.get('ChargeType')}",
                        "posted_date": event.get("PostedDate"),
                        "event_type":  "Refund",
                        "amount":      float(amt.get("CurrencyAmount", 0)),
                        "currency":    amt.get("CurrencyCode"),
                        "order_id":    order_id,
                        "description": charge.get("ChargeType"),
                        "raw":         event,
                    })

        # ── Service Fee Events ───────────────────────────────────────────────
        for event in financial_events.get("ServiceFeeEventList", []):
            for fee in event.get("FeeList", []):
                amt = fee.get("FeeAmount", {})
                flat.append({
                    "event_id":    f"fee_{event.get('AmazonOrderId','')}_{fee.get('FeeType','')}_{amt.get('CurrencyAmount','')}",
                    "posted_date": None,
                    "event_type":  f"ServiceFee_{fee.get('FeeType')}",
                    "amount":      float(amt.get("CurrencyAmount", 0)),
                    "currency":    amt.get("CurrencyCode"),
                    "order_id":    event.get("AmazonOrderId"),
                    "description": fee.get("FeeType"),
                    "raw":         event,
                })

        # ── Adjustment Events ─────────────────────────────────────────────────
        for event in financial_events.get("AdjustmentEventList", []):
            for item in event.get("AdjustmentItemList", []):
                amt = item.get("TotalAmount", {})
                flat.append({
                    "event_id":    f"adj_{event.get('AdjustmentType','')}_{item.get('SellerSKU','')}_{amt.get('CurrencyAmount','')}",
                    "posted_date": event.get("PostedDate"),
                    "event_type":  f"Adjustment_{event.get('AdjustmentType')}",
                    "amount":      float(amt.get("CurrencyAmount", 0)),
                    "currency":    amt.get("CurrencyCode"),
                    "order_id":    None,
                    "description": event.get("AdjustmentType"),
                    "raw":         event,
                })

        return flat

    async def _upsert_events(self, events: list[dict], marketplace_id: str) -> int:
        if not events:
            return 0

        now = datetime.now(tz=timezone.utc)
        rows = [
            (
                e["event_id"],
                e.get("posted_date"),
                e.get("event_type"),
                e.get("amount"),
                e.get("currency"),
                e.get("order_id"),
                marketplace_id,
                e.get("description"),
                json.dumps(e.get("raw", {})),
                now,
            )
            for e in events
        ]

        def _write():
            conn = new_conn()
            try:
                conn.executemany("""
                    INSERT OR REPLACE INTO finance_events (
                        event_id, posted_date, event_type, amount, currency,
                        order_id, marketplace_id, description, raw_json, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                count = conn.execute(
                    "SELECT COUNT(*) FROM finance_events"
                ).fetchone()[0]  # type: ignore
                by_type = conn.execute("""
                    SELECT event_type, COUNT(*) as cnt
                    FROM finance_events
                    GROUP BY event_type
                    ORDER BY cnt DESC
                    LIMIT 10
                """).fetchall()
                return {
                    "total": count,
                    "by_type": [{"type": r[0], "count": r[1]} for r in by_type],
                }
            finally:
                conn.close()
        return await asyncio.to_thread(_query)
