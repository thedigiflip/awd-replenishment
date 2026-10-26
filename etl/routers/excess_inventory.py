"""
跨境滯銷水位（Excess Inventory）Router
  GET  /excess-inventory            → 全通路滯銷明細 + 摘要 + 參數 + 資料新鮮度
  GET  /excess-inventory/trend      → 滯銷水位趨勢（grain=month|day，來自每日快照）
  POST /excess-inventory/snapshot   → 寫入今天（台灣日期）的快照；n8n 每天呼叫
  GET  /excess-inventory/params     → 查詢 Benchmark 等參數
  POST /excess-inventory/params     → 更新參數（立即生效）

定義（vw_gold_excess_inventory）：
  全通路滯銷數量 = max(0, FBA Available(US+UK+DE) + AWD Available + 美國倉
                        − 全通路近 30 天銷量 × 12)，逐 Child ASIN 計算後加總
  全通路滯銷金額 = 滯銷數量 × Anchor COG（USD）
"""

from fastapi import APIRouter, HTTPException, Query

from services.excess_inventory_service import ExcessInventoryService

router = APIRouter()
_svc = ExcessInventoryService()


@router.get("", summary="全通路滯銷明細 + 摘要")
async def get_excess_inventory():
    return await _svc.get_table()


@router.get("/trend", summary="滯銷水位趨勢（每日快照）")
async def get_trend(grain: str = Query("month", pattern="^(month|day)$")):
    return await _svc.get_trend(grain)


@router.post("/snapshot", summary="寫入今日快照（n8n 每日呼叫）")
async def take_snapshot():
    return await _svc.take_snapshot()


@router.get("/params", summary="查詢滯銷參數（Benchmark、年化月數）")
async def get_params():
    return await _svc.get_params()


@router.post("/params", summary="更新滯銷參數（立即生效）")
async def update_params(body: dict[str, float]):
    """
    範例：
    ```json
    {"excess_benchmark_usd": 150000}
    ```
    """
    try:
        return await _svc.update_params(body)
    except ValueError as e:
        raise HTTPException(422, str(e))
