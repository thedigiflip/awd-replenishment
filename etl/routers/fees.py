"""
FBA Fees Router
  POST /etl/fees/sync   → 從 SP-API 抓所有 ASIN 的 FBA fulfillment fee，寫入 product_catalog
  GET  /etl/fees/status → 最新同步狀態
  GET  /etl/fees/data   → 查看目前 catalog 的 FBA fee 資料
"""

import asyncio
from fastapi import APIRouter, BackgroundTasks
import structlog

from core.database import new_conn
from services.fees_service import FeesService

router = APIRouter()
log    = structlog.get_logger()


async def _run_fees_all_marketplaces(svc, run_id: int):
    """依序跑每個 marketplace 的 fees sync；單站失敗不影響其他站。"""
    from core.config import settings
    mps = settings.SP_API_MARKETPLACE_IDS or ["ATVPDKIKX0DER"]
    results = []
    total = 0
    for mp in mps:
        try:
            n = await svc.run(run_id=run_id, marketplace_id=mp)
            results.append({"marketplace_id": mp, "status": "ok", "rows": n})
            total += (n or 0) if isinstance(n, int) else 0
        except Exception as e:
            log.error("fees.marketplace_failed", marketplace_id=mp, error=str(e))
            results.append({"marketplace_id": mp, "status": "error", "error": str(e)[:200]})
    log.info("fees.run_all_done", run_id=run_id, results=results, total=total)


@router.post("/sync", summary="從 SP-API 同步 FBA Fee（不傳 marketplace_id → loop 全站）")
async def sync_fees(background_tasks: BackgroundTasks,
                    marketplace_id: str | None = None):
    """
    逐批呼叫 SP-API Product Fees API，取得每個 ASIN 的 FBA fulfillment fee。
    - marketplace_id 不傳 → loop 所有 SP_API_MARKETPLACE_IDS
    - marketplace_id 有傳 → 只跑該站
    - 建議每月跑一次（Amazon fee 會定期調整）
    """
    svc    = FeesService()
    run_id = await svc.start_run()
    if marketplace_id:
        background_tasks.add_task(svc.run, run_id=run_id, marketplace_id=marketplace_id)
        log.info("fees.sync_triggered", run_id=run_id, marketplace_id=marketplace_id, mode="single")
        return {
            "run_id":         run_id,
            "status":         "running",
            "message":        "FBA fee sync started. Check /etl/fees/status for progress.",
            "marketplace_id": marketplace_id,
            "estimated_minutes": 5,
        }
    background_tasks.add_task(_run_fees_all_marketplaces, svc, run_id)
    from core.config import settings
    mps = settings.SP_API_MARKETPLACE_IDS or ["ATVPDKIKX0DER"]
    log.info("fees.sync_triggered", run_id=run_id, marketplaces=mps, mode="all")
    return {
        "run_id":  run_id,
        "status":  "running",
        "message": f"FBA fee sync started for {len(mps)} marketplaces.",
        "marketplaces": mps,
        "estimated_minutes": 5 * len(mps),
    }


@router.get("/status", summary="最新 FBA Fee 同步狀態")
async def fees_status():
    svc = FeesService()
    return await svc.get_latest_run(pipeline="fees")


@router.get("/test/{asin}", summary="Debug：查看單一 ASIN 的 SP-API fees 原始回應")
async def test_fees(asin: str, marketplace_id: str = "ATVPDKIKX0DER"):
    """用來 debug fees API 回應格式，確認 parse 邏輯正確。"""
    import asyncio
    from core.sp_api_client import get_fees_api

    def _call():
        api  = get_fees_api(marketplace_id)
        resp = api.get_product_fees_estimate_for_asin(
            asin, price=25.0, currency='USD', is_fba=True
        )
        return resp.payload

    try:
        payload = await asyncio.to_thread(_call)
        return {"asin": asin, "payload": payload}
    except Exception as e:
        return {"error": str(e)}


@router.get("/data", summary="目前 catalog 的 FBA fee 資料")
async def fees_data():
    def _query():
        conn = new_conn()
        try:
            rows = conn.execute("""
                SELECT
                    sku, asin, collection, product_name,
                    cog, fba_fee,
                    ROUND(cog + fba_fee, 4) AS total_unit_cost
                FROM product_catalog
                WHERE sku != '' AND (cog > 0 OR fba_fee > 0)
                ORDER BY collection, sku
            """).fetchall()
            total     = conn.execute("SELECT COUNT(*) FROM product_catalog WHERE sku != ''").fetchone()[0]
            has_fee   = conn.execute("SELECT COUNT(*) FROM product_catalog WHERE fba_fee > 0").fetchone()[0]
            return {
                "summary": {
                    "total_skus":    total,
                    "with_fba_fee":  has_fee,
                    "without_fee":   total - has_fee,
                },
                "items": [
                    {
                        "sku":             r[0],
                        "asin":            r[1],
                        "collection":      r[2],
                        "product_name":    r[3],
                        "cog":             float(r[4] or 0),
                        "fba_fee":         float(r[5] or 0),
                        "total_unit_cost": float(r[6] or 0),
                    }
                    for r in rows
                ]
            }
        finally:
            conn.close()
    return await asyncio.to_thread(_query)
