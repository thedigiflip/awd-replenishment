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
        """單一 marketplace 同步 —— 舊行為保留給明確指定站點的 caller。"""
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

    async def run_all_marketplaces(self, run_id: int, days: int = 30) -> None:
        """
        依 region 分組跑 sales sync（region-level report）：
        - Amazon 這份 report 是「region 級別」，一個 NA/EU/FE 呼叫回傳整個 region 的資料
        - 每 region 只 call 一次（用該 region 第一個 marketplace 當代表）
        - 拉回的 rows 依 currency 分流到正確 marketplace_id
        - Amazon 每 24h 每 (report type × marketplace) 限跑 1 次
        """
        from core.config import settings
        marketplaces = settings.SP_API_MARKETPLACE_IDS or ["ATVPDKIKX0DER"]

        # 依 region 分組
        by_region: dict[str, list[str]] = {}
        for mp in marketplaces:
            region = settings.region_for(mp) if hasattr(settings, "region_for") else "na"
            by_region.setdefault(region, []).append(mp)

        log.info("sales.run_all_start", run_id=run_id,
                 marketplaces=marketplaces, regions=by_region, days=days)

        results: list[dict] = []
        total_rows = 0
        for region, mps in by_region.items():
            representative = mps[0]  # 用 region 內第一個 marketplace 當代表
            try:
                report_id = await self._create_report(representative, days)
                log.info("sales.region_report_created",
                         region=region, representative=representative, report_id=report_id)

                doc_id = await self._poll_until_done(representative, report_id)
                log.info("sales.region_report_ready",
                         region=region, doc_id=doc_id)

                raw_rows = await self._download_and_parse(representative, doc_id)
                log.info("sales.region_parsed",
                         region=region, raw_rows=len(raw_rows),
                         covers_marketplaces=mps)

                # _upsert_sales 內部會依每 row 的 marketplace_id（由 currency 決定）分流
                n = await self._upsert_sales(raw_rows, representative)
                total_rows += n
                results.append({
                    "region": region,
                    "representative": representative,
                    "covers_marketplaces": mps,
                    "status": "ok",
                    "rows": n,
                })
                log.info("sales.region_done", region=region, rows=n)
            except Exception as e:
                err = str(e)
                results.append({
                    "region": region,
                    "representative": representative,
                    "covers_marketplaces": mps,
                    "status": "error",
                    "error": err[:200],
                })
                log.error("sales.region_failed", region=region, error=err)
                # 不 raise —— 繼續下一個 region

        await self.finish_run(run_id, total_rows)
        ok = sum(1 for r in results if r["status"] == "ok")
        fail = sum(1 for r in results if r["status"] == "error")
        log.info("sales.run_all_done", run_id=run_id, total_rows=total_rows,
                 ok_regions=ok, failed_regions=fail, results=results)

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

        # Amazon FBA Sales Report 是 **region-level**（不是帳號級別）：
        # - 用 NA 站呼叫 → 回 US + CA + MX 的合併資料
        # - 用 EU 站呼叫 → 回 UK + DE + FR + IT + ES 的合併資料
        # 這份 report **沒有 sales-channel 欄位**，改用 `currency` 判斷實際 marketplace。
        # （FC 代碼比較不穩定；ship-country 需要另外 lookup；currency 最直接）
        _CURRENCY_TO_MP = {
            "USD": "ATVPDKIKX0DER",   # US
            "CAD": "A2EUQ1WTGCTBG2",  # CA
            "MXN": "A1AM78C64UM0Y8",  # MX
            "GBP": "A1F83G8C2ARO7P",  # UK
            "EUR": "A1PA6795UKMFR9",  # DE  ← 假設只賣 UK+DE；有 FR/IT/ES 需另外判斷
            "JPY": "A1VC38T7YXB528",  # JP
            "AUD": "A39IBJ37TRP1C6",  # AU
        }
        # Fulfillment-center-id 前綴後備判斷（若 currency 缺）
        _FC_PREFIX_TO_MP = {
            # US
            "BFI": "ATVPDKIKX0DER", "SEA": "ATVPDKIKX0DER", "PHX": "ATVPDKIKX0DER",
            "LAX": "ATVPDKIKX0DER", "DFW": "ATVPDKIKX0DER", "ATL": "ATVPDKIKX0DER",
            "IND": "ATVPDKIKX0DER", "MDW": "ATVPDKIKX0DER", "EWR": "ATVPDKIKX0DER",
            "MEM": "ATVPDKIKX0DER", "MCO": "ATVPDKIKX0DER", "BOS": "ATVPDKIKX0DER",
            # UK
            "LTN": "A1F83G8C2ARO7P", "MAN": "A1F83G8C2ARO7P", "EMA": "A1F83G8C2ARO7P",
            "EDI": "A1F83G8C2ARO7P", "LCY": "A1F83G8C2ARO7P", "BHX": "A1F83G8C2ARO7P",
            "DXB": "A1F83G8C2ARO7P",
            # DE
            "FRA": "A1PA6795UKMFR9", "DUS": "A1PA6795UKMFR9", "LEJ": "A1PA6795UKMFR9",
            "MUC": "A1PA6795UKMFR9", "HAM": "A1PA6795UKMFR9", "KOL": "A1PA6795UKMFR9",
            "BER": "A1PA6795UKMFR9", "STR": "A1PA6795UKMFR9",
        }
        def _resolve_marketplace(row: dict) -> str | None:
            cur = (row.get("currency") or "").strip().upper()
            if cur in _CURRENCY_TO_MP:
                return _CURRENCY_TO_MP[cur]
            fc = (row.get("fulfillment-center-id") or "").strip().upper()[:3]
            return _FC_PREFIX_TO_MP.get(fc)

        first_row = None
        currency_counter: dict = {}
        mp_counter: dict = {}
        skipped_no_mp = 0

        rows = []
        for row in reader:
            if first_row is None:
                first_row = dict(row)
            cur = (row.get("currency") or "").strip().upper()
            currency_counter[cur] = currency_counter.get(cur, 0) + 1
            sku = (row.get("sku") or "").strip()
            if not sku:
                continue

            raw_date = (row.get("shipment-date") or row.get("purchase-date") or "")[:10]
            try:
                report_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                continue

            try:
                qty = int(row.get("quantity-shipped") or row.get("quantity") or 0)
            except (ValueError, TypeError):
                qty = 0

            try:
                revenue = float(row.get("item-price-per-unit") or row.get("item-price") or 0)
            except (ValueError, TypeError):
                revenue = 0.0

            # 依 currency / FC 判斷實際 marketplace
            actual_mp = _resolve_marketplace(row)
            if actual_mp is None:
                skipped_no_mp += 1
                # fallback：用 caller 傳入的（避免遺失資料）
                actual_mp = marketplace_id
            mp_counter[actual_mp] = mp_counter.get(actual_mp, 0) + 1

            rows.append({
                "report_date":   report_date,
                "sku":           sku,
                "asin":          (row.get("asin") or "").strip(),
                "units_sold":    qty,
                "revenue":       revenue,
                "marketplace_id": actual_mp,
            })

        log.info("sales.report_columns",
                 columns=list(first_row.keys()) if first_row else [],
                 first_row_sample={k: v for k, v in (first_row.items() if first_row else [])
                                    if k in ("sku", "asin", "currency", "fulfillment-center-id",
                                            "amazon-order-id", "purchase-date", "shipment-date")})
        log.info("sales.currency_distribution",
                 distribution=dict(sorted(currency_counter.items(), key=lambda x: -x[1])[:10]))
        log.info("sales.marketplace_split",
                 caller_marketplace=marketplace_id,
                 rows_by_mp=mp_counter, unresolved=skipped_no_mp)
        return rows

    # ── Step 4: Upsert into DuckDB ────────────────────────────────────────────

    async def _upsert_sales(self, rows: list[dict], marketplace_id: str) -> int:
        if not rows:
            return 0

        now = datetime.now(tz=timezone.utc)

        # Aggregate by (report_date, sku, actual marketplace) — 每列的 marketplace_id
        # 由 sales-channel 決定，若解析不到則 fallback caller 傳入的
        agg: dict[tuple, dict] = {}
        for r in rows:
            row_mp = r.get("marketplace_id") or marketplace_id
            key = (r["report_date"], r["sku"], row_mp)
            if key not in agg:
                agg[key] = {"asin": r["asin"], "units": 0, "revenue": 0.0}
            agg[key]["units"]   += r["units_sold"]
            agg[key]["revenue"] += r["revenue"]

        insert_rows = [
            (report_date, sku, v["asin"], v["units"], round(v["revenue"], 4), row_mp, now)
            for (report_date, sku, row_mp), v in agg.items()
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
