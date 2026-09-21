"""
AWD Inventory Report Service —— GET_AWD_INVENTORY_REPORT
─────────────────────────────────────────────────────────────
比 list_inventory API 更完整、更權威（Amazon Seller Central 匯出的就是這個）。
每天凌晨透過 n8n 觸發一次，用來覆蓋 list_inventory 抓漏的 SKU。

流程：
1. create_report(GET_AWD_INVENTORY_REPORT) → 得 reportId
2. 輪詢 get_report(reportId) 直到 DONE
3. get_report_document(docId) → 下載 CSV
4. 解析 CSV（自動偵測 header 行，跳過 metadata）
5. 對比 DB 現有值 → 差異寫 sync_anomalies
6. upsert 到 awd_inventory（Report 值 always wins）

Report 值永遠取代（Strategy A：Report 是權威真相）
"""

import asyncio
import csv
import gzip
import io
import time
from datetime import datetime, timezone

import requests
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from sp_api.base import SellingApiException

from core.database import new_conn, db_write
from core.sp_api_client import get_reports_api
from services.base_service import BaseService
from services.anomaly_detector import detect_and_record_anomalies, snapshot_before

log = structlog.get_logger()

REPORT_TYPE     = "GET_AWD_INVENTORY_REPORT"
POLL_INTERVAL   = 30    # seconds
POLL_TIMEOUT    = 1200  # 20 min max


class AwdReportService(BaseService):
    pipeline = "awd_report"

    # ═══════════════════════════════════════════════════════════════════════
    async def run(self, run_id: int, marketplace_id: str = "ATVPDKIKX0DER") -> None:
        log.info("awd_report.run_start", run_id=run_id)
        try:
            report_id = await self._create_report(marketplace_id)
            log.info("awd_report.created", report_id=report_id)

            doc_id = await self._poll_until_done(marketplace_id, report_id)
            log.info("awd_report.ready", report_id=report_id, doc_id=doc_id)

            parsed = await self._download_and_parse(marketplace_id, doc_id)
            log.info("awd_report.parsed", rows=len(parsed))

            summary = await self._reconcile_and_upsert(parsed)
            await self.finish_run(run_id, summary["total"])
            log.info("awd_report.done", **summary)

        except Exception as e:
            log.error("awd_report.failed", error=str(e))
            await self.fail_run(run_id, str(e))
            raise

    # ═══════════════════════════════════════════════════════════════════════
    # Step 1 — Create report
    # ═══════════════════════════════════════════════════════════════════════
    def _call_create_report(self, api, marketplace_id: str):
        # AWD Report 不需要 dataStartTime/dataEndTime（是 snapshot 型）
        try:
            return api.create_report(
                reportType=REPORT_TYPE,
                marketplaceIds=[marketplace_id],
            )
        except SellingApiException as e:
            # 詳細記錄 Amazon 的錯誤訊息（正常會有 errors: [{code, message, details}]）
            details = ""
            for attr in ("errors", "error", "message", "response"):
                if hasattr(e, attr):
                    val = getattr(e, attr)
                    if val:
                        details = f" | {attr}={val}"
                        break
            log.error("awd_report.create_failed",
                      report_type=REPORT_TYPE,
                      marketplace=marketplace_id,
                      error=str(e),
                      details=details)
            raise

    async def _create_report(self, marketplace_id: str) -> str:
        api  = get_reports_api(marketplace_id)
        try:
            resp = await asyncio.to_thread(self._call_create_report, api, marketplace_id)
        except SellingApiException as e:
            # 把最重要的錯誤訊息寫進 exception，讓 dashboard/API 回應能看到
            errs = getattr(e, "errors", None) or getattr(e, "error", None) or []
            msg = str(errs) if errs else str(e)
            raise RuntimeError(f"Amazon SP-API 拒絕 create_report(reportType={REPORT_TYPE}): {msg}")
        return resp.payload["reportId"]

    # ═══════════════════════════════════════════════════════════════════════
    # Step 2 — Poll until DONE
    # ═══════════════════════════════════════════════════════════════════════
    async def _poll_until_done(self, marketplace_id: str, report_id: str) -> str:
        api      = get_reports_api(marketplace_id)
        deadline = time.monotonic() + POLL_TIMEOUT

        while time.monotonic() < deadline:
            resp   = await asyncio.to_thread(api.get_report, report_id)
            status = resp.payload.get("processingStatus", "")
            log.info("awd_report.poll", report_id=report_id, status=status)

            if status == "DONE":
                doc_id = resp.payload.get("reportDocumentId")
                if not doc_id:
                    raise RuntimeError("Report DONE but no reportDocumentId")
                return doc_id
            if status in ("CANCELLED", "FATAL"):
                raise RuntimeError(f"Report ended with status={status}")

            await asyncio.sleep(POLL_INTERVAL)

        raise TimeoutError(f"Report {report_id} not ready after {POLL_TIMEOUT}s")

    # ═══════════════════════════════════════════════════════════════════════
    # Step 3 — Download & Parse
    # ═══════════════════════════════════════════════════════════════════════
    async def _download_and_parse(self, marketplace_id: str, doc_id: str) -> list[dict]:
        api  = get_reports_api(marketplace_id)
        resp = await asyncio.to_thread(api.get_report_document, doc_id)
        url  = resp.payload["url"]
        compression = resp.payload.get("compressionAlgorithm", "")

        dl = await asyncio.to_thread(lambda: requests.get(url, timeout=180))
        dl.raise_for_status()

        content = dl.content
        if compression == "GZIP":
            content = gzip.decompress(content)

        text = content.decode("utf-8", errors="replace")

        # ── 自動偵測 header 行（Amazon report 前 3 行是 metadata）──────────
        lines = text.splitlines()
        header_row = 0
        for i, line in enumerate(lines[:15]):
            fields = [f.strip().strip('"').lower() for f in line.split(",")]
            if ("sku" in fields or "seller sku" in fields) and any("asin" in f for f in fields):
                header_row = i
                break

        data_text = "\n".join(lines[header_row:])
        reader    = csv.DictReader(io.StringIO(data_text))

        def _num(v):
            try: return int(float(str(v).replace(",", "")))
            except: return 0

        rows = []
        for row in reader:
            sku = (row.get("SKU") or row.get("sku") or "").strip()
            if not sku:
                continue

            # Available: 「Available in AWD (units)」
            avail = _num(row.get("Available in AWD (units)")
                         or row.get("Available Units in AWD (US)"))
            # Inbound: 「Inbound to AWD (units)」
            inbound = _num(row.get("Inbound to AWD (units)"))
            # Outbound: 用 「Outbound to FBA (units)」（跟 SP-API distributedInventory 一致）
            outbound = _num(row.get("Outbound to FBA (units)")
                            or row.get("Outbound Order (units)"))

            rows.append({
                "sku":            sku,
                "awd_available":  avail,
                "awd_inbound":    inbound,
                "awd_outbound":   outbound,
            })

        return rows

    # ═══════════════════════════════════════════════════════════════════════
    # Step 4 — Reconcile & Upsert
    # ═══════════════════════════════════════════════════════════════════════
    async def _reconcile_and_upsert(self, parsed: list[dict]) -> dict:
        if not parsed:
            return {"total": 0, "changed": 0, "critical_diff": 0, "warning_diff": 0}

        # 抓 before 快照（給 anomaly detector 用）
        before_inbound = await asyncio.to_thread(
            snapshot_before, "awd_report", "awd_inbound", "awd_inventory"
        )
        before_avail = await asyncio.to_thread(
            snapshot_before, "awd_report", "awd_available", "awd_inventory"
        )

        now = datetime.now(tz=timezone.utc)
        rows = [
            (r["sku"], r["awd_available"], r["awd_inbound"], r["awd_outbound"], now)
            for r in parsed
        ]

        def _write():
            conn = new_conn()
            try:
                # Report 值總是覆蓋（Strategy A）
                conn.executemany("""
                    INSERT INTO awd_inventory
                        (sku, awd_available, awd_inbound, awd_outbound, synced_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (sku) DO UPDATE SET
                        awd_available = excluded.awd_available,
                        awd_inbound   = excluded.awd_inbound,
                        awd_outbound  = excluded.awd_outbound,
                        synced_at     = excluded.synced_at
                """, rows)
                conn.commit()
                return len(rows)
            finally:
                conn.close()

        upserted = await db_write(_write)

        # 建立 after dicts（僅這次 Report 的值）
        after_inbound = {r["sku"]: r["awd_inbound"]    for r in parsed}
        after_avail   = {r["sku"]: r["awd_available"]  for r in parsed}

        # Anomaly：對比 Report 值 vs DB 之前值
        # 若差異大 = list_inventory API 漏了或抓錯，Report 才是真相
        try:
            r1 = await detect_and_record_anomalies(
                "awd_report", "awd_inbound",
                before_inbound, after_inbound,
            )
            r2 = await detect_and_record_anomalies(
                "awd_report", "awd_available",
                before_avail, after_avail,
            )
        except Exception as e:
            log.warning("awd_report.anomaly_failed", error=str(e))
            r1 = r2 = {"critical": 0, "warning": 0}

        return {
            "total":         upserted,
            "critical_diff": r1["critical"] + r2["critical"],
            "warning_diff":  r1["warning"]  + r2["warning"],
        }

    async def get_count(self) -> dict:
        def _q():
            conn = new_conn()
            try:
                row = conn.execute("""
                    SELECT COUNT(*), MAX(synced_at) FROM awd_inventory
                """).fetchone()
                return {"awd_skus": row[0], "last_sync": str(row[1]) if row[1] else None}
            finally:
                conn.close()
        return await asyncio.to_thread(_q)
