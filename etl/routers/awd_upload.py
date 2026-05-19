"""
AWD Inventory Router
- POST /etl/awd/sync    → 自動從 SP-API 同步（推薦）
- POST /etl/awd/upload  → 手動上傳 AWD inventory report CSV/Excel（備用）
- GET  /etl/awd/status  → 回傳最新同步時間 + 筆數

AWD Report 典型欄位（Amazon 匯出）:
  SKU, FNSKU, ASIN,
  "Inbound to AWD (units)",
  "Available in AWD (units)",
  "Outbound Order (units)"
"""

import asyncio
import io
from datetime import datetime, timezone

import structlog
import pandas as pd
from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from core.database import new_conn, db_write
from services.awd_service import AwdService

log = structlog.get_logger()
router = APIRouter()
_svc = AwdService()


# ── Auto Sync (SP-API) ────────────────────────────────────────────────────────

@router.post("/sync", summary="自動從 SP-API 同步 AWD 庫存（推薦）")
async def sync_awd(background_tasks: BackgroundTasks):
    """
    觸發 AWD Inventory 自動同步。
    - 呼叫 SP-API AmazonWarehousingAndDistribution.list_inventory
    - 分頁抓取全部 SKU，upsert awd_inventory 表
    - 立即回傳 run_id，同步在背景執行
    """
    latest = await _svc.get_latest_run(pipeline="awd_inventory")
    if latest.get("status") == "running":
        return {"run_id": latest["id"], "status": "running",
                "message": "AWD sync already in progress"}

    run_id = await _svc.start_run()
    background_tasks.add_task(_svc.run, run_id)
    log.info("awd.sync_triggered", run_id=run_id)
    return {"run_id": run_id, "status": "started",
            "message": "AWD inventory sync started in background"}

_SKU_ALIASES      = ["SKU", "sku", "Seller SKU", "seller_sku"]
_AVAIL_ALIASES    = ["Available in AWD (units)", "Available Units in AWD (US)",
                     "awd_available", "available_awd", "Available in AWD"]
_INBOUND_ALIASES  = ["Inbound to AWD (units)", "awd_inbound", "Inbound to AWD"]
_OUTBOUND_ALIASES = ["Outbound Order (units)", "Outbound to FBA (units)",
                     "awd_outbound", "Outbound Order"]


def _find_col(df: pd.DataFrame, aliases: list[str]) -> str | None:
    for alias in aliases:
        if alias in df.columns:
            return alias
    return None


@router.post("/upload", summary="上傳 AWD Inventory Report")
async def upload_awd_inventory(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(400, "No file provided")

    content = await file.read()
    try:
        if file.filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(content), dtype=str)
        else:
            df = pd.read_excel(io.BytesIO(content), dtype=str)
    except Exception as e:
        raise HTTPException(400, f"Failed to parse file: {e}")

    df.columns = [str(c).strip() for c in df.columns]

    sku_col      = _find_col(df, _SKU_ALIASES)
    avail_col    = _find_col(df, _AVAIL_ALIASES)
    inbound_col  = _find_col(df, _INBOUND_ALIASES)
    outbound_col = _find_col(df, _OUTBOUND_ALIASES)

    if not sku_col:
        raise HTTPException(422, f"找不到 SKU 欄位。實際欄位: {list(df.columns)}")

    def _safe_int(val):
        try:
            return int(float(str(val).replace(",", ""))) if pd.notna(val) else 0
        except Exception:
            return 0

    now = datetime.now(tz=timezone.utc)
    rows = []
    for _, row in df.iterrows():
        sku = str(row[sku_col]).strip() if pd.notna(row[sku_col]) else ""
        if not sku or sku.lower() in ("nan", ""):
            continue
        rows.append((
            sku,
            _safe_int(row.get(avail_col))    if avail_col    else 0,
            _safe_int(row.get(inbound_col))  if inbound_col  else 0,
            _safe_int(row.get(outbound_col)) if outbound_col else 0,
            now,
        ))

    if not rows:
        raise HTTPException(422, "沒有有效資料列")

    def _write():
        conn = new_conn()
        try:
            conn.executemany("""
                INSERT OR REPLACE INTO awd_inventory
                    (sku, awd_available, awd_inbound, awd_outbound, synced_at)
                VALUES (?, ?, ?, ?, ?)
            """, rows)
            conn.commit()
            return len(rows)
        finally:
            conn.close()

    count = await db_write(_write)
    log.info("awd_inventory.uploaded", rows=count, file=file.filename)
    return JSONResponse({"status": "ok", "rows_upserted": count, "file": file.filename,
                         "columns_detected": {
                             "sku": sku_col, "available": avail_col,
                             "inbound": inbound_col, "outbound": outbound_col
                         }})


@router.get("/status", summary="AWD Inventory 資料狀態 + 最新同步紀錄")
async def awd_status():
    async def _db():
        def _q():
            conn = new_conn()
            try:
                row = conn.execute("SELECT COUNT(*), MAX(synced_at) FROM awd_inventory").fetchone()
                return {"total_skus": row[0], "last_sync": str(row[1]) if row[1] else None}
            finally:
                conn.close()
        return await asyncio.to_thread(_q)

    data   = await _db()
    latest = await _svc.get_latest_run(pipeline="awd_inventory")
    data["run"] = latest
    return data
