"""
FBA Fees Service — 從 SP-API Product Fees API 取得每個 ASIN 的 FBA Fulfillment Fee
並更新 product_catalog.fba_fee

流程：
  1. 從 product_catalog 取出所有有效 ASIN
  2. 從 sales_traffic 計算每個 ASIN 的平均售價（MSRP）作為費用估算的基準價格
  3. 每批 10 個 ASIN 呼叫 SP-API get_my_fees_estimates
  4. 解析 FBAFees 寫回 product_catalog.fba_fee
  5. 每批之間等待 2 秒（rate limit: 0.5 req/s）

注意：
  - Referral Fee 固定 15%，直接在 Dashboard 計算，不需要 API
  - FBA fee 依商品尺寸/重量不同，必須從 API 取得才準確
"""

import asyncio
import time

import structlog

from core.database import new_conn, db_write
from core.sp_api_client import get_fees_api
from services.base_service import BaseService

log = structlog.get_logger()

REFERRAL_RATE   = 0.15   # 固定 15%（在 dashboard 計算，這裡不用）
BATCH_SIZE      = 20     # 每批處理的 ASIN 數（記錄 log 用，API 仍一次一個）
BATCH_SLEEP_SEC = 0      # ASIN 之間的等待已在 _fetch_fees_batch 內處理


class FeesService(BaseService):
    pipeline = "fees"

    async def run(self, run_id: int, marketplace_id: str) -> None:
        log.info("fees.run_start", run_id=run_id, marketplace_id=marketplace_id)
        try:
            # ── 1. 取出所有有效 ASIN + 平均售價 ────────────────────────────
            asin_prices = await asyncio.to_thread(self._load_asins_with_prices)
            log.info("fees.asins_loaded", total=len(asin_prices))

            if not asin_prices:
                await self.finish_run(run_id, 0)
                return

            # ── 2. 分批呼叫 SP-API ──────────────────────────────────────────
            results = {}  # asin → fba_fee
            asins   = list(asin_prices.keys())
            batches = [asins[i:i+BATCH_SIZE] for i in range(0, len(asins), BATCH_SIZE)]

            for idx, batch in enumerate(batches):
                log.info("fees.batch", batch=f"{idx+1}/{len(batches)}", size=len(batch))
                batch_results = await asyncio.to_thread(
                    self._fetch_fees_batch, marketplace_id, batch, asin_prices
                )
                results.update(batch_results)

                if idx < len(batches) - 1:
                    await asyncio.sleep(BATCH_SLEEP_SEC)

            # ── 3. 寫回 product_catalog ──────────────────────────────────────
            updated = await db_write(lambda: self._upsert_fees(results))
            await self.finish_run(run_id, updated)
            log.info("fees.run_done", run_id=run_id, updated=updated,
                     total_asins=len(asin_prices), fetched=len(results))

        except Exception as e:
            log.error("fees.run_failed", run_id=run_id, error=str(e))
            await self.fail_run(run_id, str(e))
            raise

    # ── Load ASINs + avg MSRP from DB ────────────────────────────────────────

    def _load_asins_with_prices(self) -> dict[str, float]:
        """
        從 product_catalog 取出有效 ASIN，並從 sales_traffic 取得平均售價。
        若 ASIN 沒有 traffic 資料則使用預設價格 $25（Amazon 費用估算用）。
        """
        conn = new_conn()
        try:
            rows = conn.execute("""
                SELECT
                    pc.asin,
                    COALESCE(
                        SUM(st.total_ordered_product_sales) /
                        NULLIF(SUM(st.total_units_ordered), 0),
                        25.0
                    ) AS avg_price
                FROM product_catalog pc
                LEFT JOIN sales_traffic st ON pc.asin = st.child_asin
                WHERE pc.asin != '' AND pc.sku != ''
                GROUP BY pc.asin
            """).fetchall()
            return {r[0]: max(float(r[1] or 25.0), 1.0) for r in rows}
        finally:
            conn.close()

    # ── Fetch fees from SP-API ────────────────────────────────────────────────

    def _fetch_fees_batch(
        self,
        marketplace_id: str,
        asins: list[str],
        asin_prices: dict[str, float],
    ) -> dict[str, float]:
        """
        逐一呼叫 SP-API get_product_fees_estimate_for_asin，
        取得每個 ASIN 的 FBA Fulfillment Fee。
        每個 ASIN 一次請求，之間等待 2 秒（rate limit: 0.5 req/s）。
        """
        import time
        api     = get_fees_api(marketplace_id)
        results = {}

        for i, asin in enumerate(asins):
            try:
                resp    = api.get_product_fees_estimate_for_asin(
                    asin,
                    price=asin_prices.get(asin, 25.0),
                    currency='USD',
                    is_fba=True,
                )
                fba_fee = self._parse_single_fee(asin, resp.payload)
                if fba_fee > 0:
                    results[asin] = fba_fee
                    log.debug("fees.parsed", asin=asin, fba_fee=fba_fee)
            except Exception as e:
                log.warning("fees.asin_error", asin=asin, error=str(e))

            # Rate limit: 0.5 req/s → wait 2.1s between calls
            if i < len(asins) - 1:
                time.sleep(2.1)

        return results

    def _parse_single_fee(self, asin: str, payload: dict) -> float:
        """
        從單一 ASIN 的 API response 解析 FBA Fulfillment Fee。

        Response 結構（確認自實際 API）：
          payload.FeesEstimateResult.FeesEstimate.FeeDetailList
            → FeeType = "FBAFees"  ← 這是 FBA fulfillment fee 總和
              FinalFee.Amount = 3.86  ← 使用 FinalFee（扣促銷後的實際費用）
              IncludedFeeDetailList → 子費用明細（不要再加，避免重複計算）
        """
        try:
            result = payload.get("FeesEstimateResult", {})
            status = result.get("Status", "")
            if status != "Success":
                err_msg = result.get("Error", {}).get("Message", status)
                log.warning("fees.api_status", asin=asin, status=status, error=err_msg)
                return 0.0

            fee_list = (
                result.get("FeesEstimate", {})
                      .get("FeeDetailList", [])
            )
            for detail in fee_list:
                if detail.get("FeeType") == "FBAFees":
                    # 使用 FinalFee（已扣除 Amazon 促銷/優惠後的實際費用）
                    amount = float(
                        detail.get("FinalFee", {}).get("Amount", 0) or 0
                    )
                    return round(amount, 4)

            return 0.0  # FBAFees 不在 list 中（例如 Merchant fulfilled）
        except Exception as e:
            log.warning("fees.parse_error", asin=asin, error=str(e))
            return 0.0

    # ── Write back to product_catalog ─────────────────────────────────────────

    def _upsert_fees(self, results: dict[str, float]) -> int:
        if not results:
            return 0
        conn = new_conn()
        try:
            rows = [(fee, asin) for asin, fee in results.items()]
            conn.executemany(
                "UPDATE product_catalog SET fba_fee = ? WHERE asin = ?",
                rows
            )
            conn.commit()
            return len(rows)
        finally:
            conn.close()
