"""
Product Catalog Upload Router
- POST /etl/catalog/upload → 上傳 MAGEASY Anchor Excel/CSV，upsert product_catalog
- GET  /etl/catalog/status → 回傳筆數 + 最後上傳時間

MAGEASY Anchor 欄位:
  A: SKU
  B: (Parent) ASIN
  C: (Child) ASIN
  D: Collection
  E: Name
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

# 欄位別名（支援中英文、大小寫）
_SKU_ALIASES    = ["SKU", "sku", "Seller SKU", "seller_sku"]
_PARENT_ALIASES = ["(Parent) ASIN", "Parent ASIN", "parent_asin", "ParentASIN"]
_ASIN_ALIASES   = ["(Child) ASIN", "Child ASIN", "ASIN", "asin", "ChildASIN"]
_COLL_ALIASES   = ["Collection", "collection", "系列"]
_NAME_ALIASES   = ["Name", "name", "Product Name", "product_name", "商品名稱"]


def _find(df: pd.DataFrame, aliases: list[str]) -> str | None:
    for a in aliases:
        if a in df.columns:
            return a
    return None


@router.post("/upload", summary="上傳 MAGEASY Anchor（產品目錄）")
async def upload_catalog(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(400, "No file provided")

    content = await file.read()
    try:
        # 支援多工作表 Excel：優先找 "MAGEASY Anchor" sheet，找不到就用第一個
        if file.filename.lower().endswith(".csv"):
            df = pd.read_csv(io.BytesIO(content), dtype=str)
        else:
            xl = pd.ExcelFile(io.BytesIO(content))
            sheet = next(
                (s for s in xl.sheet_names if "anchor" in s.lower() or "mageasy" in s.lower()),
                xl.sheet_names[0]
            )
            df = pd.read_excel(io.BytesIO(content), sheet_name=sheet, dtype=str)
            log.info("catalog.sheet_selected", sheet=sheet)
    except Exception as e:
        raise HTTPException(400, f"Failed to parse file: {e}")

    df.columns = [str(c).strip() for c in df.columns]

    sku_col    = _find(df, _SKU_ALIASES)
    parent_col = _find(df, _PARENT_ALIASES)
    asin_col   = _find(df, _ASIN_ALIASES)
    coll_col   = _find(df, _COLL_ALIASES)
    name_col   = _find(df, _NAME_ALIASES)

    if not sku_col:
        raise HTTPException(422, f"找不到 SKU 欄位。實際欄位: {list(df.columns)}")

    now = datetime.now(tz=timezone.utc)

    def _clean(row, col):
        if not col:
            return ""
        v = row.get(col, "")
        return str(v).strip() if pd.notna(v) and str(v).lower() != "nan" else ""

    rows = []
    for _, row in df.iterrows():
        sku = _clean(row, sku_col)
        if not sku:
            continue
        rows.append((
            sku,
            _clean(row, parent_col),
            _clean(row, asin_col),
            _clean(row, coll_col),
            _clean(row, name_col),
            now,
        ))

    if not rows:
        raise HTTPException(422, "沒有有效資料列")

    def _write():
        conn = new_conn()
        try:
            conn.executemany("""
                INSERT OR REPLACE INTO product_catalog
                    (sku, parent_asin, asin, collection, product_name, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, rows)
            conn.commit()
            return len(rows)
        finally:
            conn.close()

    count = await db_write(_write)
    log.info("catalog.uploaded", rows=count, file=file.filename)
    return JSONResponse({
        "status": "ok",
        "rows_upserted": count,
        "file": file.filename,
        "columns_detected": {
            "sku": sku_col, "parent_asin": parent_col,
            "asin": asin_col, "collection": coll_col, "name": name_col,
        },
    })


@router.get("/status", summary="產品目錄狀態")
async def catalog_status():
    def _query():
        conn = new_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*), MAX(updated_at) FROM product_catalog"
            ).fetchone()
            return {"total_skus": row[0], "last_upload": str(row[1]) if row[1] else None}
        finally:
            conn.close()
    return await asyncio.to_thread(_query)
