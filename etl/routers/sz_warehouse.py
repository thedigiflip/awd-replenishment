"""
SZ Warehouse Upload Router
- POST /etl/sz/upload  → 上傳 Excel/CSV（深圳倉庫存），upsert sz_warehouse
- GET  /etl/sz/status  → 回傳最新快照時間 + 筆數
"""

import asyncio
import io
from datetime import datetime, timezone

import structlog
import pandas as pd
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from core.database import new_conn, db_write

log = structlog.get_logger()
router = APIRouter()

# ── 欄位對應（自動偵測，支援中文/英文欄位名）──────────────────────────────────
# 深圳倉 Excel 典型欄位: 商品编码, 商品名称, 可出库数量（基本）
_SKU_ALIASES    = ["商品编码", "sku", "SKU", "product_code", "商品編碼"]
_NAME_ALIASES   = ["商品名称", "product_name", "商品名稱", "name"]
_AVAIL_ALIASES  = ["可用库存（基本）", "可用庫存", "available_qty", "available", "可用"]
_SHIP_ALIASES   = ["可出库数量（基本）", "可出庫數量", "shippable_qty", "shippable", "可出庫"]


def _find_col(df: pd.DataFrame, aliases: list[str]) -> str | None:
    for alias in aliases:
        if alias in df.columns:
            return alias
    return None


@router.post("/upload", summary="上傳深圳倉庫存 Excel/CSV")
async def upload_sz_warehouse(file: UploadFile = File(...)):
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

    sku_col   = _find_col(df, _SKU_ALIASES)
    name_col  = _find_col(df, _NAME_ALIASES)
    avail_col = _find_col(df, _AVAIL_ALIASES)
    ship_col  = _find_col(df, _SHIP_ALIASES)

    if not sku_col:
        raise HTTPException(422, f"找不到 SKU 欄位，請確認欄位名稱包含: {_SKU_ALIASES}\n實際欄位: {list(df.columns)}")

    now = datetime.now(tz=timezone.utc)
    rows = []
    for _, row in df.iterrows():
        sku = str(row[sku_col]).strip() if pd.notna(row[sku_col]) else ""
        if not sku or sku.lower() in ("nan", ""):
            continue

        def _safe_int(val):
            try:
                return int(float(str(val).replace(",", ""))) if pd.notna(val) else 0
            except Exception:
                return 0

        rows.append((
            sku,
            str(row.get(sku_col, "")).strip(),
            str(row[name_col]).strip() if name_col and pd.notna(row.get(name_col)) else "",
            _safe_int(row.get(avail_col)) if avail_col else 0,
            _safe_int(row.get(ship_col)) if ship_col else 0,
            now,
        ))

    if not rows:
        raise HTTPException(422, "沒有有效資料列，請檢查檔案內容")

    def _write():
        conn = new_conn()
        try:
            conn.executemany("""
                INSERT OR REPLACE INTO sz_warehouse
                    (sku, product_code, product_name, available_qty, shippable_qty, synced_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, rows)
            conn.commit()
            return len(rows)
        finally:
            conn.close()

    count = await db_write(_write)
    log.info("sz_warehouse.uploaded", rows=count, file=file.filename)
    return JSONResponse({"status": "ok", "rows_upserted": count, "file": file.filename})


@router.get("/status", summary="深圳倉資料狀態")
async def sz_status():
    def _query():
        conn = new_conn()
        try:
            row = conn.execute("""
                SELECT COUNT(*), MAX(synced_at) FROM sz_warehouse
            """).fetchone()
            return {"total_skus": row[0], "last_upload": str(row[1]) if row[1] else None}
        finally:
            conn.close()
    return await asyncio.to_thread(_query)
