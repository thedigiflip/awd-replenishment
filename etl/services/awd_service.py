"""
AWD Inventory ETL Service
- 呼叫 SP-API AmazonWarehousingAndDistribution.list_inventory
- 分頁抓取所有 AWD 庫存 (details=SHOW)
- Upsert into DuckDB awd_inventory table

AWD API response 結構（details=SHOW）:
  inventory[].sku                   → sku
  inventory[].totalOnhandQuantity   → awd_available（AWD 倉內可用）
  inventory[].totalInboundQuantity  → awd_inbound（運往 AWD 途中）
  inventory[].details.distributedInventory → outbound to FBA（各 FC 的補貨在途）
"""

import asyncio
import json
from datetime import datetime, timezone

import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from sp_api.base import SellingApiException

from core.database import new_conn, db_write
from core.sp_api_client import get_awd_api
from services.base_service import BaseService

log = structlog.get_logger()


class AwdService(BaseService):
    pipeline = "awd_inventory"

    async def run(self, run_id: int) -> None:
        log.info("awd.run_start", run_id=run_id)
        total_rows = 0
        try:
            items = await self._fetch_all_inventory()
            total_rows = await self._upsert_awd(items)
            await self.finish_run(run_id, total_rows)
            log.info("awd.run_done", run_id=run_id, rows=total_rows)
        except Exception as e:
            log.error("awd.run_failed", run_id=run_id, error=str(e))
            await self.fail_run(run_id, str(e))
            raise

    # ── SP-API fetch ──────────────────────────────────────────────────────────

    async def _fetch_all_inventory(self) -> list[dict]:
        """分頁拉取所有 AWD 庫存，每次呼叫在 thread 中執行避免阻塞 event loop。"""
        # AWD API 不分 marketplace，直接用 US client（AWD 目前僅支援 NA）
        api = get_awd_api("ATVPDKIKX0DER")
        all_items: list[dict] = []
        next_token: str | None = None

        while True:
            kwargs: dict = {
                "details":    "SHOW",
                "maxResults": 100,
            }
            if next_token:
                kwargs["nextToken"] = next_token

            resp = await asyncio.to_thread(self._call_list_inventory, api, kwargs)
            payload  = resp.payload or {}
            items    = payload.get("inventory", [])
            all_items.extend(items)

            next_token = payload.get("nextToken")
            log.info("awd.page_fetched", count=len(items), has_next=bool(next_token))
            if not next_token:
                break

        log.info("awd.total_fetched", total=len(all_items))
        return all_items

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=4, max=30),
        retry=retry_if_exception_type(SellingApiException),
    )
    def _call_list_inventory(self, api, kwargs):
        return api.list_inventory(**kwargs)

    # ── DuckDB upsert ─────────────────────────────────────────────────────────

    async def _upsert_awd(self, items: list[dict]) -> int:
        if not items:
            return 0

        now = datetime.now(tz=timezone.utc)
        rows = []
        for item in items:
            sku = (item.get("sku") or "").strip()
            if not sku:
                continue

            def _qty(val):
                """quantity 可能是純數字或 {"amount": N, "unitOfMeasurement": "UNIT"}"""
                if isinstance(val, dict):
                    return int(val.get("amount", 0) or 0)
                return int(val or 0)

            avail    = _qty(item.get("totalOnhandQuantity", 0))
            inbound  = _qty(item.get("totalInboundQuantity", 0))

            # outbound = units distributed to FBA (in transit from AWD → FC)
            # quantity 可能是純數字或 {"amount": N, "unitOfMeasurement": "UNIT"}
            outbound = 0
            details  = item.get("details") or {}
            for dist in details.get("distributedInventory", []):
                qty = dist.get("quantity", 0) or 0
                if isinstance(qty, dict):
                    outbound += int(qty.get("amount", 0) or 0)
                else:
                    outbound += int(qty)

            rows.append((sku, avail, inbound, outbound, json.dumps(item), now))

        if not rows:
            return 0

        def _write():
            conn = new_conn()
            try:
                conn.executemany("""
                    INSERT OR REPLACE INTO awd_inventory
                        (sku, awd_available, awd_inbound, awd_outbound, synced_at)
                    VALUES (?, ?, ?, ?, ?)
                """, [(r[0], r[1], r[2], r[3], r[5]) for r in rows])
                conn.commit()
                return len(rows)
            finally:
                conn.close()

        return await db_write(_write)

    async def get_count(self) -> dict:
        def _query():
            conn = new_conn()
            try:
                row = conn.execute(
                    "SELECT COUNT(*), MAX(synced_at) FROM awd_inventory"
                ).fetchone()
                return {"awd_skus": row[0], "last_sync": str(row[1]) if row[1] else None}
            finally:
                conn.close()
        return await asyncio.to_thread(_query)
