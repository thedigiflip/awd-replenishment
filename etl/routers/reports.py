"""
Reports Router
  GET  /reports/columns                 → 匯出欄位定義（dashboard CSV 與月報共用）
  POST /reports/monthly?period=YYYY-MM  → 產生月報 Excel（預設 = 上個月），回傳信件主旨 / 內文 / 摘要
  GET  /reports/monthly/{period}/file   → 下載已產生的月報 Excel（n8n 寄信附件用）
"""

import re

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from core.report_columns import EXCESS_COLUMNS, replenishment_headers
from services.monthly_report_service import MonthlyReportService, report_path
from services.replenishment_service import ReplenishmentService

router = APIRouter()
_svc = MonthlyReportService()
_rep = ReplenishmentService()

_PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/columns", summary="匯出欄位定義（CSV / 月報共用）")
async def get_columns():
    controls = await _rep.get_controls()
    cap = controls["total_cap_months"]["value"]
    return {
        "replenishment": [[k, h] for k, h in replenishment_headers(cap)],
        "excess":        [[k, h] for k, h in EXCESS_COLUMNS],
    }


@router.post("/monthly", summary="產生每月滯銷庫存報表（Excel 5 分頁）")
async def generate_monthly(period: str | None = Query(None, description="YYYY-MM；預設上個月")):
    if period and not _PERIOD.match(period):
        raise HTTPException(422, "period 格式須為 YYYY-MM")
    return await _svc.generate(period)


@router.get("/monthly/{period}/file", summary="下載月報 Excel")
async def download_monthly(period: str):
    if not _PERIOD.match(period):
        raise HTTPException(422, "period 格式須為 YYYY-MM")
    path = report_path(period)
    if not path.exists():
        raise HTTPException(404, f"{period} 的月報尚未產生")
    return FileResponse(path, media_type=XLSX_MIME, filename=path.name)
