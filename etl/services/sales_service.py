"""
Sales Service — Reports API Pipeline
- 呼叫 SP-API Reports.create_report (GET_FLAT_FILE_ORDERS_DATA)
- 輪詢直到報告完成（最多等 15 分鐘）
- 下載 TSV，篩選 Shipped 訂單，按日期 × SKU 加總
- Upsert into DuckDB sales_summary

使用 sales_summary 取代 order_items JOIN orders 計算銷量，
不受訂單 Pending 狀態影響，數據來源是 Amazon 確認出貨的紀錄。
"""

import asyncio
import csv
import gzip
import io
import time
from datetime import datetime, timezone, timedelta, date

import requests
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from sp_api.base import SellingApiException

from core.database import new_conn, db_write
from core.sp_api_client import get_reports_api
from services.base_service import BaseService

log = structlog.get_logger()

REPORT_TYPE    = "GET_FBA_FULFILLMENT_CUSTOMER_SHIPMENT_SALES_DATA"
POLL_INTERVAL  = 20   # seconds between polls
POLL_TIMEOUT   = 900  # 15 minutes max wait


class SalesService(BaseService):
    pipeline = "sales"

    async def run(self, run_id: int, marketplace_id: str, days: int = 30) -> None:
        log.info("sales.run_start", run_id=run_id, marketplace_id=marketplace_id, days=days)
        try:
            report_id = await self._create_report(marketplace_id, days)
            log.info("sales.report_created", report_id=report_id)

            doc_id = await self._poll_until_done(marketplace_id, report_id)
            log.info("sales.report_ready", doc_id=doc_id)

            raw_rows = await self._download_and_parse(marketplace_id, doc_id)
            log.info("sales.parsed", raw_rows=len(raw_rows))

            total = await self._upsert_sales(raw_rows, marketplace_id)
            await self.finish_run(run_id, total)
            log.info("sales.run_done", run_id=run_id, rows=total)

        except Exception as e:
            log.error("sales.run_failed", run_id=run_id, error=str(e))
            await self.fail_run(run_id, str(e))
            raise

    # ── Step 1: Create Report ─────────────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=4, max=30),
        retry=retry_if_exception_type(SellingApiException),
    )
    def _call_create_report(self, api, report_type: str, marketplace_id: str,
                             start_str: str, end_str: str):
        # sp-api-python 1.9.x: 直接傳 kwargs，不用 body={}
        return api.create_report(
            reportType=report_type,
            marketplaceIds=[marketplace_id],
            dataStartTime=start_str,
            dataEndTime=end_str,
        )

    async def _create_report(self, marketplace_id: str, days: int) -> str:
        api   = get_reports_api(marketplace_id)
        now   = datetime.now(tz=timezone.utc)
        start = now - timedelta(days=days)

        start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_str   = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        resp = await asyncio.to_thread(
            self._call_create_report, api, REPORT_TYPE, marketplace_id, start_str, end_str
        )
        return resp.payload["reportId"]

    # ── Step 2: Poll until DONE ───────────────────────────────────────────────

    async def _poll_until_done(self, marketplace_id: str, report_id: str) -> str:
        api      = get_reports_api(marketplace_id)
        deadline = time.monotonic() + POLL_TIMEOUT

        while time.monotonic() < deadline:
            resp   = await asyncio.to_thread(api.get_report, report_id)
            status = resp.payload.get("processingStatus", "")
            log.info("sales.poll", report_id=report_id, status=status)

            if status == "DONE":
                doc_id = resp.payload.get("reportDocumentId")
                if not doc_id:
                    raise RuntimeError("Report DONE but no reportDocumentId returned")
                return doc_id
            elif status in ("CANCELLED", "FATAL"):
                raise RuntimeError(f"Report {report_id} ended with status={status}")

            await asyncio.sleep(POLL_INTERVAL)

        raise TimeoutError(f"Report {report_id} not ready after {POLL_TIMEOUT}s")

    # ── Step 3: Download & Parse TSV ─────────────────────────────────────────

    async def _download_and_parse(self, marketplace_id: str, doc_id: str) -> list[dict]:
        api  = get_reports_api(marketplace_id)
        resp = await asyncio.to_thread(api.get_report_document, doc_id)
        url  = resp.payload["url"]
        compression = resp.payload.get("compressionAlgorithm", "")

        # Download presigned S3 URL
        dl = await asyncio.to_thread(
            lambda: requests.get(url, timeout=120)
        )
        dl.raise_for_status()

        content = dl.content
        if compression == "GZIP":
            content = gzip.decompress(content)

        text   = content.decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")

        rows = []
        for row in reader:
            sku = (row.get("sku") or "").strip()
            if not sku:
                continue

            # FBA Shipment Sales Report 用 shipment-date，格式 YYYY-MM-DD
            raw_date = (row.get("shipment-date") or row.get("purchase-date") or "")[:10]
            try:
                report_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                continue

            # FBA report 用 quantity-shipped；一般 orders report 用 quantity
            try:
                qty = int(row.get("quantity-shipped") or row.get("quantity") or 0)
            except (ValueError, TypeError):
                qty = 0

            try:
                revenue = float(row.get("item-price") or 0)
            except (ValueError, TypeError):
                revenue = 0.0

            rows.append({
                "report_date": report_date,
                "sku":         sku,
                "asin":        (row.get("asin") or "").strip(),
                "units_sold":  qty,
                "revenue":     revenue,
            })

        return rows

    # ── Step 4: Upsert into DuckDB ────────────────────────────────────────────

    async def _upsert_sales(self, rows: list[dict], marketplace_id: str) -> int:
        if not rows:
            return 0

        now = datetime.now(tz=timezone.utc)

        # Aggregate by (report_date, sku) before writing
        agg: dict[tuple, dict] = {}
        for r in rows:
            key = (r["report_date"], r["sku"])
            if key not in agg:
                agg[key] = {"asin": r["asin"], "units": 0, "revenue": 0.0}
            agg[key]["units"]   += r["units_sold"]
            agg[key]["revenue"] += r["revenue"]

        insert_rows = [
            (report_date, sku, v["asin"], v["units"], round(v["revenue"], 4), marketplace_id, now)
            for (report_date, sku), v in agg.items()
        ]

        def _write():
            conn = new_conn()
            try:
                conn.executemany("""
                    INSERT OR REPLACE INTO sales_summary
                        (report_date, sku, asin, units_sold, revenue, marketplace_id, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, insert_rows)
                conn.commit()
                return len(insert_rows)
            finally:
                conn.close()

        return await db_write(_write)

    # ── Query helpers ─────────────────────────────────────────────────────────

    async def get_count(self) -> dict:
        def _query():
            conn = new_conn()
            try:
                row = conn.execute(
                    "SELECT COUNT(*), MIN(report_date), MAX(report_date) FROM sales_summary"
                ).fetchone()
                return {
                    "sales_records": row[0],
                    "date_from":     str(row[1]) if row[1] else None,
                    "date_to":       str(row[2]) if row[2] else None,
                }
            finally:
                conn.close()
        return await asyncio.to_thread(_query)
