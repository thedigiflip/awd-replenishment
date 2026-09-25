"""
Replenishment Router
  GET  /replenishment              → 完整補貨清單（JSON）
  GET  /replenishment/export       → 下載 CSV
  POST /replenishment/sku-config   → 批量更新 product_type + unit_per_case
  GET  /replenishment/sku-config   → 查詢所有 SKU 設定
"""

import csv
import io
from typing import Any

import structlog
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, StreamingResponse

from services.replenishment_service import ReplenishmentService

log = structlog.get_logger()
router = APIRouter()
_svc = ReplenishmentService()

# ─── CSV 欄位順序（對應 Dashboard 主表 group 順序）───────────────────────────
_CSV_FIELDS = [
    # 識別
    ("collection",            "Collection"),
    ("parent_asin",           "Parent ASIN"),
    ("asin",                  "ASIN"),
    ("sku",                   "SKU"),
    ("product_name",          "Name"),
    # 設定
    ("product_type",          "産品類型"),
    ("awd_target_months",     "AWD 目標月數"),
    # 銷售
    ("sales_qty",             "Sales Qty (M)"),
    # FBA
    ("fba_available",         "FBA Available（現貨）"),
    ("fba_inbound",           "FBA Inbound（在途）"),
    ("fba_total",             "FBA Total"),
    ("fba_month",             "FBA Total Month"),
    # AWD
    ("awd_available",         "AWD Available"),
    ("awd_inbound",           "AWD Inbound"),
    ("awd_outbound",          "AWD Outbound"),
    ("awd_total",             "AWD Total"),
    ("awd_month",             "AWD Month"),
    # Coverage
    ("total_coverage",        "Total Coverage FBA+AWD (M)"),
    ("cap_alert",             "Cap 警示（FBA+AWD 總上限）"),
    # 深圳倉庫（美國倉 + 欠數 + 佳樂倉）
    ("safety_stock",          "安全庫存門檻"),
    ("us_qty",                "美國倉"),
    ("pending_qty",           "欠數 (已下單)"),
    ("order_date",            "下單日期"),
    ("expected_date",         "約定交期"),
    ("factory_confirmed_date","工廠回覆交期"),
    ("delivery_advance_days", "提前/延後天數"),
    ("overdue_days",          "逾期天數"),
    ("jl_qty",                "佳樂倉"),
    ("unit_per_case",         "Unit/Case"),
    ("us_month",              "美國倉 Month"),
    ("us_virtual_month",      "美國虛擬 Month (含欠數)"),
    ("sz_total_month",        "深圳倉庫總量 Month"),
    ("transfer_qty",          "移倉數建議 (佳樂→美國)"),
    # 返單計畫
    ("reorder_alert",         "返單警示"),
    ("reorder_qty",           "建議返單數量"),
    ("case_reorder_qty",      "整箱返單數量"),
    ("suggested_order_date",  "建議下單日"),
    ("days_until_late",       "交期倒數 (天)"),
    # AWD Replenishment
    ("air_alert",             "🚨 空運警示"),
    ("immediate_coverage",    "Immediate Coverage"),
    ("awd_replen_qty",        "AWD 需補貨量"),
    ("awd_replen_case_qty",   "AWD 需補量 (整箱)"),
    ("awd_replen_case_cnt",   "AWD 需補箱數"),
    ("total_pipeline",        "Total Pipeline (M)"),
    # 其他警示 / 診斷欄位（dashboard 沒顯示，但保留於 CSV 供查詢）
    ("stock_alert",           "📦 庫存警示"),
    ("sz_reorder_level",      "SZ 返單水位保留數"),
    ("sz_available_qty",      "SZ 可出量"),
    ("sea_shipment_qty",      "建議海運量"),
    ("days_can_last",         "現有可撐天數"),
    ("lead_time_days",        "生產交期 (天)"),
    ("case_reorder_count",    "返單箱數"),
    # 同 ASIN 多 SKU 資訊
    ("sku_count",             "SKU 數 (同 ASIN)"),
    ("all_skus",              "全部 alias SKUs"),
]


@router.get("", summary="補貨計算清單（JSON）")
async def get_replenishment(
    marketplace_id: str = Query("ATVPDKIKX0DER", description="Amazon Marketplace ID"),
    air_only: bool = Query(False, description="只顯示有空運警示的 SKU"),
    hero_only: bool = Query(False, description="只顯示流量款"),
):
    data = await _svc.get_table(marketplace_id)
    if air_only:
        data = [r for r in data if r["air_alert"]]
    if hero_only:
        data = [r for r in data if r["product_type"] == "流量款"]
    # 附上 Controls 閾值，讓前端做動態著色
    controls = await _svc.get_controls()
    prod_d   = controls.get("production_days",   {}).get("value", 30.0)
    sea_d    = controls.get("sea_days",          {}).get("value", 40.0)
    cap_m    = controls.get("total_cap_months",  {}).get("value", 3.0)
    return JSONResponse({
        "count": len(data), "data": data,
        "thresholds": {
            "air_threshold":     round((prod_d + sea_d) / 30, 2),
            "total_cap_months":  cap_m,
        },
    })


@router.get("/export", summary="匯出 CSV")
async def export_csv(
    marketplace_id: str = Query("ATVPDKIKX0DER"),
):
    data = await _svc.get_table(marketplace_id)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=[f for f, _ in _CSV_FIELDS],
                            extrasaction="ignore")
    # 寫入中文 header
    writer.writerow({f: h for f, h in _CSV_FIELDS})
    writer.writerows(data)

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": "attachment; filename=replenishment.csv"},
    )


@router.post("/sku-config", summary="批量更新 SKU 設定（產品類型 + 箱規）")
async def update_sku_config(configs: list[dict[str, Any]]):
    """
    Body: [{"sku": "ABC-123", "product_type": "流量款", "unit_per_case": 48}, ...]
    """
    count = await _svc.upsert_sku_config(configs)
    return {"status": "ok", "updated": count}


@router.get("/controls", summary="查詢目前補貨水位參數")
async def get_controls():
    """
    回傳所有 Controls 參數目前的值與說明。
    - production_days    生產天數（影響空運觸發閾值）
    - sea_days           海運天數（影響空運觸發閾值）
    - awd_target_normal  AWD 目標月數（一般款）
    - awd_target_hero    AWD 目標月數（流量款）
    - total_cap_months   FBA+AWD 總上限月數
    - sz_level_normal    SZ 返單水位（一般款）
    - sz_level_hero      SZ 返單水位（流量款）
    """
    return await _svc.get_controls()


@router.post("/controls", summary="更新補貨水位參數（不需重啟）")
async def update_controls(body: dict[str, float]):
    """
    傳入要修改的 key/value，其他 key 保持不變。立即生效，不需重啟 container。

    範例：
    ```json
    {
      "awd_target_hero": 2.0,
      "sz_level_hero": 2.0,
      "total_cap_months": 4.0
    }
    ```
    """
    try:
        return await _svc.upsert_controls(body)
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(422, str(e))


@router.get("/unmatched-skus", summary="FBA/AWD 有但 Anchor 沒有的 SKU")
async def get_unmatched_skus():
    """
    找出 FBA inventory 或 AWD inventory 裡有、但 product_catalog (MAGEASY Anchor) 沒有的 SKU。
    這些 SKU 可能是新品、測試品、或 Anchor 尚未建立紀錄。
    """
    return await _svc.get_unmatched_skus()


@router.get("/daily-alert", summary="每日庫存警示（供 n8n 發送 email）")
async def daily_alert(
    marketplace_id: str = Query("ATVPDKIKX0DER", description="Amazon Marketplace ID"),
):
    """
    整合兩類警示：
    1. 🔴🟠🟡 庫存斷貨的 SKU（stock_alert）
    2. FBA/AWD 有但 Anchor 找不到的 SKU（unmatched）

    n8n 可以每天早上呼叫此端點，根據 has_issues 決定是否發送 email 通知。
    """
    return await _svc.get_daily_alert(marketplace_id)


@router.get("/sku-config", summary="查詢所有 SKU 設定")
async def get_sku_config():
    import asyncio
    from core.database import new_conn

    def _q():
        conn = new_conn()
        try:
            rows = conn.execute(
                "SELECT sku, product_type, unit_per_case, eta, updated_at FROM sku_config ORDER BY sku"
            ).fetchall()
            return [{"sku": r[0], "product_type": r[1], "unit_per_case": r[2],
                     "eta": r[3], "updated_at": str(r[4])} for r in rows]
        finally:
            conn.close()

    return await asyncio.to_thread(_q)
