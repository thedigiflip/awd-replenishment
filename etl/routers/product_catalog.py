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
  F: COG (Cost of Goods, USD/unit)
"""

import asyncio
import io
from datetime import datetime, timezone

import structlog
import pandas as pd
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
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
_COG_ALIASES    = ["COG", "cog", "Cost", "cost", "成本", "產品成本"]


def _find(df: pd.DataFrame, aliases: list[str]) -> str | None:
    for a in aliases:
        if a in df.columns:
            return a
    return None


@router.post("/upload", summary="上傳 MAGEASY Anchor（產品目錄）")
async def upload_catalog(
    file: UploadFile = File(...),
    replace: bool = Query(
        True,
        description="True (預設) = 完全取代整個 product_catalog；False = 只 upsert (保留舊 SKU)",
    ),
):
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
    cog_col    = _find(df, _COG_ALIASES)

    if not sku_col:
        raise HTTPException(422, f"找不到 SKU 欄位。實際欄位: {list(df.columns)}")

    now = datetime.now(tz=timezone.utc)

    def _clean(row, col):
        if not col:
            return ""
        v = row.get(col, "")
        return str(v).strip() if pd.notna(v) and str(v).lower() != "nan" else ""

    def _clean_num(row, col):
        if not col:
            return 0.0
        v = row.get(col, 0)
        try:
            return float(str(v).replace(",", "")) if pd.notna(v) and str(v).lower() not in ("nan", "-", "") else 0.0
        except Exception:
            return 0.0

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
            _clean_num(row, cog_col),
            now,
        ))

    if not rows:
        raise HTTPException(422, "沒有有效資料列")

    def _write():
        conn = new_conn()
        try:
            deleted = 0
            if replace:
                # 完全取代：保留 fba_fee（來自 SP-API Product Fees，非 Anchor 提供），
                # 但把 Anchor 沒收錄的 SKU 整筆刪掉，避免舊資料殘留。
                new_skus = tuple({r[0] for r in rows})
                if new_skus:
                    # DuckDB 支援 array parameter，用 NOT IN 刪掉不在新清單中的
                    placeholders = ",".join(["?"] * len(new_skus))
                    result = conn.execute(
                        f"DELETE FROM product_catalog WHERE sku NOT IN ({placeholders})",
                        list(new_skus),
                    )
                    # 取得 rowcount（DuckDB 沒有直接 rowcount，用差值計算）
                    remaining = conn.execute(
                        "SELECT COUNT(*) FROM product_catalog"
                    ).fetchone()[0]
                    deleted = remaining  # 先記錄清理後保留數
                else:
                    conn.execute("DELETE FROM product_catalog")

            conn.executemany("""
                INSERT OR REPLACE INTO product_catalog
                    (sku, parent_asin, asin, collection, product_name, cog, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, rows)
            conn.commit()

            total_after = conn.execute(
                "SELECT COUNT(*) FROM product_catalog"
            ).fetchone()[0]
            return {"upserted": len(rows), "total_after": total_after}
        finally:
            conn.close()

    result = await db_write(_write)
    log.info(
        "catalog.uploaded",
        rows=result["upserted"],
        total_after=result["total_after"],
        file=file.filename,
        cog_col=cog_col,
        replace=replace,
    )
    return JSONResponse({
        "status": "ok",
        "mode": "replace" if replace else "upsert",
        "rows_upserted": result["upserted"],
        "total_after": result["total_after"],
        "file": file.filename,
        "columns_detected": {
            "sku": sku_col, "parent_asin": parent_col,
            "asin": asin_col, "collection": coll_col,
            "name": name_col, "cog": cog_col,
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
