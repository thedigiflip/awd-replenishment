"""
Traffic Service — Sales & Traffic Report Pipeline

流程：
  1. 向 SP-API 提交 GET_SALES_AND_TRAFFIC_REPORT（by Child ASIN）
  2. 輪詢直到報告完成（最多等 15 分鐘）
  3. 下載 JSON 報告，解析每個 Child ASIN 的流量與銷售數據
  4. Join product_catalog 補上 SKU / Collection / Name
  5. Upsert into DuckDB sales_traffic

報告特性：
  - 每個 Marketplace 每天最多請求 1 次
  - 資料是「整個查詢區間的累計值」，不是每日明細
  - 預設查詢範圍：上個完整月份（1 日 ~ 月底）
"""

import asyncio
import gzip
import json
import time
from datetime import datetime, timezone, timedelta
from calendar import monthrange

import requests
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from sp_api.base import SellingApiException

from core.database import new_conn, db_write
from core.sp_api_client import get_reports_api
from services.base_service import BaseService

log = structlog.get_logger()

REPORT_TYPE   = "GET_SALES_AND_TRAFFIC_REPORT"
POLL_INTERVAL = 30    # seconds between status polls
POLL_TIMEOUT  = 900   # 15 minutes max


class TrafficService(BaseService):
    pipeline = "traffic"

    async def run(
        self,
        run_id: int,
        marketplace_id: str,
        data_start_date: str,   # "YYYY-MM-DD"
        data_end_date: str,     # "YYYY-MM-DD"
    ) -> None:
        log.info(
            "traffic.run_start",
            run_id=run_id,
            marketplace_id=marketplace_id,
            start=data_start_date,
            end=data_end_date,
        )
        try:
            report_id = await self._create_report(
                marketplace_id, data_start_date, data_end_date
            )
            log.info("traffic.report_created", report_id=report_id)

            doc_id = await self._poll_until_done(marketplace_id, report_id)
            log.info("traffic.report_ready", doc_id=doc_id)

            rows = await self._download_and_parse(
                marketplace_id, doc_id, data_start_date, data_end_date
            )
            log.info("traffic.parsed", raw_rows=len(rows))

            # Join product_catalog 補 SKU / Collection / Name
            rows = await self._enrich_with_catalog(rows)

            total = await self._upsert(rows)
            await self.finish_run(run_id, total)
            log.info("traffic.run_done", run_id=run_id, rows=total)

        except Exception as e:
            log.error("traffic.run_failed", run_id=run_id, error=str(e))
            await self.fail_run(run_id, str(e))
            raise

    # ── Step 1: Create Report ─────────────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=65, max=130),  # throttle 需要 60s+ 恢復
        retry=retry_if_exception_type(SellingApiException),
    )
    def _call_create_report(
        self, api, marketplace_id: str, start_str: str, end_str: str
    ):
        return api.create_report(
            reportType=REPORT_TYPE,
            marketplaceIds=[marketplace_id],
            dataStartTime=start_str,
            dataEndTime=end_str,
            reportOptions={"asinGranularity": "CHILD"},
        )

    async def _create_report(
        self, marketplace_id: str, data_start_date: str, data_end_date: str
    ) -> str:
        api = get_reports_api(marketplace_id)
        # SP-API 需要 ISO 8601 格式帶時區
        start_str = f"{data_start_date}T00:00:00Z"
        end_str   = f"{data_end_date}T23:59:59Z"

        resp = await asyncio.to_thread(
            self._call_create_report, api, marketplace_id, start_str, end_str
        )
        return resp.payload["reportId"]

    # ── Step 2: Poll until DONE ───────────────────────────────────────────────

    async def _poll_until_done(self, marketplace_id: str, report_id: str) -> str:
        api      = get_reports_api(marketplace_id)
        deadline = time.monotonic() + POLL_TIMEOUT

        while time.monotonic() < deadline:
            resp   = await asyncio.to_thread(api.get_report, report_id)
            status = resp.payload.get("processingStatus", "")
            log.info("traffic.poll", report_id=report_id, status=status)

            if status == "DONE":
                doc_id = resp.payload.get("reportDocumentId")
                if not doc_id:
                    raise RuntimeError("Report DONE but no reportDocumentId returned")
                return doc_id
            elif status in ("CANCELLED", "FATAL"):
                raise RuntimeError(
                    f"Report {report_id} ended with status={status}"
                )

            await asyncio.sleep(POLL_INTERVAL)

        raise TimeoutError(f"Report {report_id} not ready after {POLL_TIMEOUT}s")

    # ── Step 3: Download & Parse JSON ─────────────────────────────────────────

    async def _download_and_parse(
        self,
        marketplace_id: str,
        doc_id: str,
        data_start_date: str,
        data_end_date: str,
    ) -> list[dict]:
        api  = get_reports_api(marketplace_id)
        resp = await asyncio.to_thread(api.get_report_document, doc_id)
        url  = resp.payload["url"]
        compression = resp.payload.get("compressionAlgorithm", "")

        # 下載 S3 presigned URL
        dl = await asyncio.to_thread(lambda: requests.get(url, timeout=120))
        dl.raise_for_status()

        content = dl.content
        if compression == "GZIP":
            content = gzip.decompress(content)

        # GET_SALES_AND_TRAFFIC_REPORT 回傳 JSON（非 TSV）
        data = json.loads(content.decode("utf-8", errors="replace"))

        # 頂層 key: salesAndTrafficByAsin（when asinGranularity=CHILD）
        records = data.get("salesAndTrafficByAsin", [])

        rows = []
        for rec in records:
            child_asin  = (rec.get("childAsin") or "").strip()
            parent_asin = (rec.get("parentAsin") or "").strip()
            if not child_asin:
                continue

            sales   = rec.get("salesByAsin", {})
            traffic = rec.get("trafficByAsin", {})

            # orderedProductSales 是 {"amount": ..., "currencyCode": ...}
            def _amt(obj):
                if isinstance(obj, dict):
                    return float(obj.get("amount", 0) or 0)
                return float(obj or 0)

            units_b2c = int(sales.get("unitsOrdered", 0) or 0)
            units_b2b = int(sales.get("unitsOrderedB2B", 0) or 0)
            sales_b2c = _amt(sales.get("orderedProductSales"))
            sales_b2b = _amt(sales.get("orderedProductSalesB2B"))

            rows.append({
                "data_start_date":             data_start_date,
                "data_end_date":               data_end_date,
                "child_asin":                  child_asin,
                "parent_asin":                 parent_asin,
                "marketplace_id":              marketplace_id,
                # Traffic
                "sessions":                    int(traffic.get("sessions", 0) or 0),
                "page_views":                  int(traffic.get("pageViews", 0) or 0),
                "buy_box_percentage":          float(traffic.get("buyBoxPercentage", 0) or 0),
                "unit_session_percentage":     float(traffic.get("unitSessionPercentage", 0) or 0),
                "unit_session_percentage_b2b": float(traffic.get("unitSessionPercentageB2B", 0) or 0),
                # Sales
                "units_ordered":               units_b2c,
                "units_ordered_b2b":           units_b2b,
                "total_units_ordered":         units_b2c + units_b2b,
                "ordered_product_sales":       round(sales_b2c, 4),
                "ordered_product_sales_b2b":   round(sales_b2b, 4),
                "total_ordered_product_sales": round(sales_b2c + sales_b2b, 4),
                "total_order_items":           int(sales.get("totalOrderItems", 0) or 0),
                "total_order_items_b2b":       int(sales.get("totalOrderItemsB2B", 0) or 0),
                # Catalog fields（後續 enrich 填入）
                "sku":          "",
                "collection":   "",
                "product_name": "",
            })

        return rows

    # ── Step 4: Enrich with product_catalog (ASIN → SKU/Collection/Name) ──────

    async def _enrich_with_catalog(self, rows: list[dict]) -> list[dict]:
        """
        從 DuckDB product_catalog 撈出所有 asin → (sku, collection, product_name) mapping，
        在記憶體內 join，避免 N+1 query。
        """
        if not rows:
            return rows

        def _load_catalog() -> dict[str, dict]:
            conn = new_conn()
            try:
                result = conn.execute("""
                    SELECT asin, sku, collection, product_name
                    FROM product_catalog
                    WHERE asin IS NOT NULL AND asin != ''
                """).fetchall()
                return {
                    row[0]: {
                        "sku":          row[1] or "",
                        "collection":   row[2] or "",
                        "product_name": row[3] or "",
                    }
                    for row in result
                }
            finally:
                conn.close()

        catalog = await asyncio.to_thread(_load_catalog)
        log.info("traffic.catalog_loaded", catalog_entries=len(catalog))

        enriched = 0
        for row in rows:
            info = catalog.get(row["child_asin"])
            if info:
                row["sku"]          = info["sku"]
                row["collection"]   = info["collection"]
                row["product_name"] = info["product_name"]
                enriched += 1

        log.info("traffic.enrich_done", enriched=enriched, total=len(rows))
        return rows

    # ── Step 5: Upsert into DuckDB ────────────────────────────────────────────

    async def _upsert(self, rows: list[dict]) -> int:
        if not rows:
            return 0

        now = datetime.now(tz=timezone.utc)

        insert_rows = [
            (
                r["data_start_date"],
                r["data_end_date"],
                r["child_asin"],
                r["parent_asin"],
                r["marketplace_id"],
                r["sku"],
                r["collection"],
                r["product_name"],
                r["sessions"],
                r["page_views"],
                r["buy_box_percentage"],
                r["unit_session_percentage"],
                r["unit_session_percentage_b2b"],
                r["units_ordered"],
                r["units_ordered_b2b"],
                r["total_units_ordered"],
                r["ordered_product_sales"],
                r["ordered_product_sales_b2b"],
                r["total_ordered_product_sales"],
                r["total_order_items"],
                r["total_order_items_b2b"],
                now,
            )
            for r in rows
        ]

        def _write():
            conn = new_conn()
            try:
                conn.executemany("""
                    INSERT OR REPLACE INTO sales_traffic (
                        data_start_date, data_end_date,
                        child_asin, parent_asin, marketplace_id,
                        sku, collection, product_name,
                        sessions, page_views,
                        buy_box_percentage,
                        unit_session_percentage, unit_session_percentage_b2b,
                        units_ordered, units_ordered_b2b, total_units_ordered,
                        ordered_product_sales, ordered_product_sales_b2b,
                        total_ordered_product_sales,
                        total_order_items, total_order_items_b2b,
                        synced_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?
                    )
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
                row = conn.execute("""
                    SELECT
                        COUNT(*) AS total_rows,
                        COUNT(DISTINCT child_asin) AS unique_asins,
                        MIN(data_start_date) AS earliest_start,
                        MAX(data_end_date)   AS latest_end
                    FROM sales_traffic
                """).fetchone()
                return {
                    "total_rows":    row[0],
                    "unique_asins":  row[1],
                    "earliest_start": str(row[2]) if row[2] else None,
                    "latest_end":    str(row[3]) if row[3] else None,
                }
            finally:
                conn.close()
        return await asyncio.to_thread(_query)


# ── Backfill：按月份逐一請求（最多 24 個月）────────────────────────────────────

    async def backfill(
        self,
        marketplace_id: str,
        start_month: str,   # "YYYY-MM"
        end_month: str,     # "YYYY-MM"
    ) -> None:
        """
        按月份逐一跑完整 pipeline（create → poll → download → upsert）。
        每個月建立獨立的 run_id，方便透過 /etl/traffic/history 追蹤進度。
        月份之間等待 10 秒，避免觸碰 SP-API rate limit。
        """
        months = _generate_months(start_month, end_month)
        log.info("traffic.backfill_start", total_months=len(months),
                 start=start_month, end=end_month, marketplace_id=marketplace_id)

        processed = 0
        for i, month in enumerate(months):
            start_date, end_date = _month_to_range(month)

            # ── 跳過已有資料的月份（重跑 backfill 時不重複請求）──────────────
            if await self._month_exists(start_date, end_date, marketplace_id):
                log.info("traffic.backfill_skip", month=month,
                         reason="already_in_db", progress=f"{i+1}/{len(months)}")
                continue

            run_id = await self.start_run(
                month=month,
                marketplace_id=marketplace_id,
                backfill=True,
            )
            log.info("traffic.backfill_month", month=month,
                     progress=f"{i+1}/{len(months)}", run_id=run_id)
            try:
                await self.run(run_id, marketplace_id, start_date, end_date)
                processed += 1
            except Exception as e:
                # 單月失敗不中斷整個 backfill，繼續跑下一個月
                log.error("traffic.backfill_month_failed", month=month,
                          run_id=run_id, error=str(e))

            # SP-API createReport rate limit：65 秒確保每分鐘不超過 1 次
            if i < len(months) - 1:
                log.info("traffic.backfill_wait", seconds=65,
                         next_month=months[i+1] if i+1 < len(months) else "done")
                await asyncio.sleep(65)

        log.info("traffic.backfill_done", total_months=len(months), processed=processed)

    async def _month_exists(
        self, start_date: str, end_date: str, marketplace_id: str
    ) -> bool:
        """DuckDB 中是否已有該月份的資料。"""
        def _check():
            conn = new_conn()
            try:
                count = conn.execute("""
                    SELECT COUNT(*) FROM sales_traffic
                    WHERE data_start_date = ?
                      AND data_end_date   = ?
                      AND marketplace_id  = ?
                """, [start_date, end_date, marketplace_id]).fetchone()[0]
                return count > 0
            finally:
                conn.close()
        return await asyncio.to_thread(_check)


# ── 日期工具 ──────────────────────────────────────────────────────────────────

def last_month_range() -> tuple[str, str]:
    """回傳 (start, end) 字串，格式 'YYYY-MM-DD'，代表上個完整月份。"""
    today = datetime.now(tz=timezone.utc).date()
    first_of_this_month = today.replace(day=1)
    last_month_last_day = first_of_this_month - timedelta(days=1)
    last_month_first_day = last_month_last_day.replace(day=1)
    return str(last_month_first_day), str(last_month_last_day)


def _generate_months(start_month: str, end_month: str) -> list[str]:
    """
    從 start_month 到 end_month（含）產生所有月份清單。
    格式：'YYYY-MM'，由舊到新排列。
    """
    sy, sm = map(int, start_month.split("-"))
    ey, em = map(int, end_month.split("-"))
    months = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def _month_to_range(month: str) -> tuple[str, str]:
    """
    將 'YYYY-MM' 轉換為 ('YYYY-MM-01', 'YYYY-MM-DD')，DD 為該月最後一天。
    """
    y, m = map(int, month.split("-"))
    last_day = monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last_day:02d}"
