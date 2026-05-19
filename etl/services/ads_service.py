"""
Ads ETL Service
使用 Amazon Advertising API v3 的 Report API（非同步報表）：
1. POST /reporting/reports → 建立報表請求
2. GET /reporting/reports/{reportId} → 輪詢直到 COMPLETED
3. GET report downloadUrl → 下載 gzipped JSON / CSV
4. 解析寫入 DuckDB ads_sponsored_products

注意：Ads API 和 SP-API 使用不同的 credentials（ADS_* 環境變數）
"""

import gzip
import json
import time
from datetime import date, datetime, timezone

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from core.config import settings
from core.database import new_conn
from services.base_service import BaseService

log = structlog.get_logger()

ADS_API_BASE = "https://advertising-api.amazon.com"  # JP: advertising-api-fe.amazon.com
ADS_TOKEN_URL = "https://api.amazon.com/auth/o2/token"


class AdsService(BaseService):
    pipeline = "ads"

    def _get_access_token(self) -> str:
        """Exchange refresh token for access token (Ads API)."""
        resp = httpx.post(ADS_TOKEN_URL, data={
            "grant_type":    "refresh_token",
            "refresh_token": settings.ADS_REFRESH_TOKEN,
            "client_id":     settings.ADS_CLIENT_ID,
            "client_secret": settings.ADS_CLIENT_SECRET,
        })
        resp.raise_for_status()
        return resp.json()["access_token"]

    def _headers(self, token: str) -> dict:
        return {
            "Authorization":          f"Bearer {token}",
            "Amazon-Advertising-API-ClientId": settings.ADS_CLIENT_ID,
            "Amazon-Advertising-API-Scope":    settings.ADS_PROFILE_ID,
            "Content-Type":           "application/json",
        }

    async def run(
        self,
        run_id: int,
        report_date: date,
        report_type: str = "spAdvertisedProduct",
    ) -> None:
        log.info("ads.run_start", run_id=run_id, report_date=str(report_date))
        try:
            records = await self._fetch_report(report_date, report_type)
            rows = await self._upsert_ads(records, report_date)
            await self.finish_run(run_id, rows)
            log.info("ads.run_done", run_id=run_id, rows=rows)
        except Exception as e:
            log.error("ads.run_failed", run_id=run_id, error=str(e))
            await self.fail_run(run_id, str(e))
            raise

    async def _fetch_report(self, report_date: date, report_type: str) -> list[dict]:
        if not settings.ADS_PROFILE_ID:
            log.warning("ads.no_profile_id — skipping Ads sync")
            return []

        token = self._get_access_token()
        headers = self._headers(token)

        # 1. 建立報表請求
        report_payload = {
            "name":        f"SP_{report_date.isoformat()}",
            "startDate":   report_date.isoformat(),
            "endDate":     report_date.isoformat(),
            "configuration": {
                "adProduct":   "SPONSORED_PRODUCTS",
                "groupBy":     ["advertiser"],
                "columns":     [
                    "date", "campaignId", "campaignName",
                    "adGroupId", "adGroupName", "asin", "sku",
                    "impressions", "clicks", "spend",
                    "sales1d", "sales7d", "sales14d", "sales30d",
                    "unitsSoldClicks1d", "unitsSoldClicks7d",
                ],
                "reportTypeId": "spAdvertisedProduct",
                "timeUnit":    "DAILY",
                "format":      "GZIP_JSON",
            },
        }
        resp = httpx.post(
            f"{ADS_API_BASE}/reporting/reports",
            headers=headers,
            json=report_payload,
        )
        resp.raise_for_status()
        report_id = resp.json()["reportId"]
        log.info("ads.report_created", report_id=report_id)

        # 2. 輪詢報表狀態
        download_url = self._poll_report(report_id, headers)

        # 3. 下載報表
        data_resp = httpx.get(download_url, follow_redirects=True)
        data_resp.raise_for_status()
        records = json.loads(gzip.decompress(data_resp.content))
        log.info("ads.report_downloaded", records=len(records))
        return records

    @retry(stop=stop_after_attempt(20), wait=wait_exponential(min=10, max=60))
    def _poll_report(self, report_id: str, headers: dict) -> str:
        resp = httpx.get(f"{ADS_API_BASE}/reporting/reports/{report_id}", headers=headers)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status")
        log.info("ads.report_poll", report_id=report_id, status=status)
        if status == "COMPLETED":
            return data["url"]
        if status == "FAILED":
            raise ValueError(f"Ads report failed: {data}")
        raise Exception(f"Report not ready yet: {status}")

    async def _upsert_ads(self, records: list[dict], report_date: date) -> int:
        if not records:
            return 0
        conn = new_conn()
        try:
            rows_written = 0
            for r in records:
                conn.execute("""
                    INSERT OR REPLACE INTO ads_sponsored_products (
                        report_date, profile_id, campaign_id, campaign_name,
                        ad_group_id, ad_group_name, asin, sku,
                        impressions, clicks, spend,
                        sales_1d, sales_7d, sales_14d, sales_30d,
                        units_sold_clicks_1d, units_sold_clicks_7d,
                        currency, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    report_date,
                    settings.ADS_PROFILE_ID,
                    r.get("campaignId"),
                    r.get("campaignName"),
                    r.get("adGroupId"),
                    r.get("adGroupName"),
                    r.get("asin"),
                    r.get("sku"),
                    r.get("impressions", 0),
                    r.get("clicks", 0),
                    float(r.get("spend", 0)),
                    float(r.get("sales1d", 0)),
                    float(r.get("sales7d", 0)),
                    float(r.get("sales14d", 0)),
                    float(r.get("sales30d", 0)),
                    r.get("unitsSoldClicks1d", 0),
                    r.get("unitsSoldClicks7d", 0),
                    "JPY",   # marketplace-dependent, pull from profile if needed
                    datetime.now(tz=timezone.utc),
                ])
                rows_written += 1
            conn.commit()
            return rows_written
        finally:
            conn.close()

    async def get_count(self) -> dict:
        conn = new_conn()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM ads_sponsored_products"
            ).fetchone()[0]  # type: ignore
            latest = conn.execute(
                "SELECT MAX(report_date) FROM ads_sponsored_products"
            ).fetchone()[0]  # type: ignore
            return {"ads_records": count, "latest_date": str(latest)}
        finally:
            conn.close()
