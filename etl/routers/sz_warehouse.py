"""
SZ Warehouse Router — 深圳倉庫（美國倉 + 佳樂倉 + Unit/Case）
─────────────────────────────────────────────────────────────
- POST /etl/sz/upload    → 上傳 Excel/CSV
     • 預設 replace=true：清空 sz_warehouse 舊資料後寫入新資料
     • 同步更新 sku_config.unit_per_case（單一 source of truth）
- GET  /etl/sz/template  → 下載 SZ Upload Template（依當下 Anchor 動態生成）
- GET  /etl/sz/status    → 回傳最新快照時間 + 筆數 + 資料新鮮度（days_old）
- GET  /etl/sz/unmatched → SZ 有但 Anchor 找不到的 SKU（依 ASIN mapping 判斷）
"""

import asyncio
import io
from datetime import datetime, timezone, timedelta

import structlog
import pandas as pd
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from core.database import new_conn, db_write

log = structlog.get_logger()
router = APIRouter()

# ── 欄位別名（新格式：SKU + 美國倉庫存 + 佳樂倉庫存 + Unit/Case）─────────────
# 保留舊別名以便舊 template 也能上傳
_SKU_ALIASES    = ["SKU", "sku", "商品编码", "商品編碼", "Seller SKU"]
_US_ALIASES     = ["美國倉庫存", "美國倉", "us_qty", "US Warehouse", "US_qty",
                   # 舊格式相容：把「可出庫」當美國倉
                   "可出库数量（基本）", "可出庫數量", "shippable_qty"]
_JL_ALIASES     = ["佳樂倉庫存", "佳樂倉", "jl_qty", "JL Warehouse", "Jiale"]
_UC_ALIASES     = ["Unit/Case", "unit_per_case", "unit/case", "Unit per Case",
                   "箱規", "箱裝"]
# 欠數（已下單但工廠未交付）
_PENDING_ALIASES = ["欠數", "已下單", "PO Pending", "pending_qty", "pending",
                    "Pending", "已下單未到"]
# 下單日期（欠數的下單日；用來計算約定交期日）
_ORDER_DATE_ALIASES = ["下單日期", "PO Date", "order_date", "Order Date",
                       "訂單日期", "下訂日期", "採購日期"]
# 工廠回覆交期（工廠實際承諾的交貨日期，優先於約定交期）
_FACTORY_DATE_ALIASES = ["工廠回覆交期", "工廠回覆", "Factory ETA", "Confirmed ETA",
                         "factory_confirmed_date", "工廠交期", "承諾交期"]
# ASIN 別名（用於「以 ASIN 為 Key」的 fallback 對應）
_ASIN_ALIASES   = ["(Child) ASIN", "Child ASIN", "ASIN", "asin", "ChildASIN"]


def _parse_date(val):
    """
    多格式日期解析：
    - Excel 日期序號（float/int like 45123）
    - ISO 字串 '2026-08-20'
    - 美式 '08/20/2026' / '8/20/26'
    - 歐式 '2026/08/20' / '2026.08.20'
    - datetime object
    回傳 datetime.date 或 None
    """
    from datetime import date, datetime, timedelta
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    # Excel serial number
    try:
        num = float(val)
        if num > 1000 and num < 100000:  # reasonable Excel serial range
            # Excel epoch is 1899-12-30 (accounting for bug)
            return (datetime(1899, 12, 30) + timedelta(days=num)).date()
    except (ValueError, TypeError):
        pass
    # String parsing
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", ""):
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
                "%m/%d/%Y", "%m-%d-%Y",
                "%m/%d/%y", "%d/%m/%Y",
                "%Y%m%d"):
        try:
            return datetime.strptime(s[:10] if len(s) > 10 else s, fmt).date()
        except ValueError:
            continue
    return None


def _find(df: pd.DataFrame, aliases: list[str]) -> str | None:
    for a in aliases:
        if a in df.columns:
            return a
    return None


def _safe_int(val, default=0):
    try:
        if pd.isna(val):
            return default
        return int(float(str(val).replace(",", "")))
    except Exception:
        return default


# ═══════════════════════════════════════════════════════════════════════════
# POST /etl/sz/upload
# ═══════════════════════════════════════════════════════════════════════════
@router.post("/upload", summary="上傳 SZ 倉庫存 Template（含美國倉 / 佳樂倉 / Unit/Case）")
async def upload_sz(
    file: UploadFile = File(...),
    replace: bool = Query(
        True,
        description="True（預設）= 完全取代 sz_warehouse；False = 只 upsert 新資料，保留舊 SKU",
    ),
):
    if not file.filename:
        raise HTTPException(400, "No file provided")

    content = await file.read()
    try:
        if file.filename.lower().endswith(".csv"):
            df = pd.read_csv(io.BytesIO(content), dtype=str)
        else:
            xl = pd.ExcelFile(io.BytesIO(content))
            # 優先找 MAGEASY Anchor / SZ / Template sheet
            sheet = next(
                (s for s in xl.sheet_names
                 if any(k in s.lower() for k in ("anchor", "mageasy", "sz", "template", "warehouse"))),
                xl.sheet_names[0],
            )
            df = pd.read_excel(io.BytesIO(content), sheet_name=sheet, dtype=str)
            log.info("sz.sheet_selected", sheet=sheet)
    except Exception as e:
        raise HTTPException(400, f"Failed to parse file: {e}")

    df.columns = [str(c).strip() for c in df.columns]

    sku_col  = _find(df, _SKU_ALIASES)
    asin_col = _find(df, _ASIN_ALIASES)   # ASIN 做 fallback key
    us_col   = _find(df, _US_ALIASES)
    pd_col   = _find(df, _PENDING_ALIASES)  # 欠數
    od_col   = _find(df, _ORDER_DATE_ALIASES)  # 下單日期
    fd_col   = _find(df, _FACTORY_DATE_ALIASES)  # 工廠回覆交期
    jl_col   = _find(df, _JL_ALIASES)
    uc_col   = _find(df, _UC_ALIASES)

    if not sku_col and not asin_col:
        raise HTTPException(
            422,
            f"找不到 SKU 或 ASIN 欄位。實際欄位: {list(df.columns)}\n"
            f"支援 SKU 欄位: {_SKU_ALIASES}\n"
            f"支援 ASIN 欄位: {_ASIN_ALIASES}"
        )

    # ─── 讀取 Anchor 的 ASIN → 現行 SKU 對應（權威 mapping）─────────────────
    # 每次上傳都用當下最新 Anchor 為準：
    #   - 若 template 的 SKU 已不在 Anchor，用 ASIN 找回 Anchor 現行 SKU
    #   - 這樣就算 SKU 換版，也不會存到過期的 SKU
    def _load_anchor_maps():
        conn = new_conn()
        try:
            rows = conn.execute(
                "SELECT sku, asin FROM product_catalog WHERE asin != ''"
            ).fetchall()
            sku_set  = {r[0] for r in rows}
            asin2sku = {r[1]: r[0] for r in rows}
            return sku_set, asin2sku
        finally:
            conn.close()

    anchor_skus, anchor_asin_to_sku = _load_anchor_maps()

    now = datetime.now(tz=timezone.utc)
    sz_rows: list[tuple] = []
    uc_rows: list[tuple] = []
    remapped = 0   # 統計：透過 ASIN 對回 Anchor SKU 的筆數
    skipped  = 0   # 無法對應（既非 Anchor SKU 也非 Anchor ASIN）

    for _, row in df.iterrows():
        raw_sku  = str(row[sku_col]).strip()  if sku_col  and pd.notna(row[sku_col])  else ""
        raw_asin = str(row[asin_col]).strip() if asin_col and pd.notna(row[asin_col]) else ""
        if raw_sku.lower() in ("nan", ""):  raw_sku  = ""
        if raw_asin.lower() in ("nan", ""): raw_asin = ""

        # 決定最終要存的 SKU（以 Anchor 現行 SKU 為權威）
        final_sku = ""
        if raw_sku and raw_sku in anchor_skus:
            # SKU 本身在 Anchor → 直接用
            final_sku = raw_sku
        elif raw_asin and raw_asin in anchor_asin_to_sku:
            # SKU 對不到（換版了？），用 ASIN 找回 Anchor 現行 SKU
            final_sku = anchor_asin_to_sku[raw_asin]
            if raw_sku and raw_sku != final_sku:
                remapped += 1
        elif raw_sku:
            # 兩個都對不到 Anchor，姑且存原 SKU（下次 Anchor 更新後可能對到）
            final_sku = raw_sku
            skipped += 1
        else:
            continue  # 完全沒 key，跳過

        us = _safe_int(row.get(us_col)) if us_col else 0
        pending = _safe_int(row.get(pd_col)) if pd_col else 0
        jl = _safe_int(row.get(jl_col)) if jl_col else 0
        uc = _safe_int(row.get(uc_col), default=1) if uc_col else 1
        order_date = _parse_date(row.get(od_col)) if od_col else None
        factory_date = _parse_date(row.get(fd_col)) if fd_col else None
        if uc < 1:
            uc = 1

        sz_rows.append((final_sku, us, jl, uc, pending, order_date, factory_date, now))
        if uc_col:
            uc_rows.append((final_sku, uc, now))

    if not sz_rows:
        raise HTTPException(422, "沒有有效資料列")

    def _write():
        conn = new_conn()
        try:
            deleted = 0
            if replace:
                deleted = conn.execute("SELECT COUNT(*) FROM sz_warehouse").fetchone()[0]
                conn.execute("DELETE FROM sz_warehouse")

            # sz_warehouse：以新 template 欄位為主
            # sz_rows 結構: (sku, us_qty, jl_qty, unit_per_case, pending_qty,
            #               order_date, factory_confirmed_date, synced_at)
            conn.executemany("""
                INSERT INTO sz_warehouse
                    (sku, us_qty, jl_qty, unit_per_case, pending_qty,
                     order_date, factory_confirmed_date, synced_at,
                     product_code, product_name, available_qty, shippable_qty)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '', 0, ?)
            """, [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[1]) for r in sz_rows])

            # sku_config：同步 unit_per_case（單一 source of truth）
            if uc_rows:
                conn.executemany("""
                    INSERT INTO sku_config (sku, product_type, unit_per_case, updated_at)
                    VALUES (?, '', ?, ?)
                    ON CONFLICT (sku) DO UPDATE SET
                        unit_per_case = excluded.unit_per_case,
                        updated_at    = now()
                """, uc_rows)
            conn.commit()
            return {"upserted": len(sz_rows), "cleared_before": deleted}
        finally:
            conn.close()

    result = await db_write(_write)
    log.info("sz.uploaded",
             rows=result["upserted"],
             cleared=result["cleared_before"],
             remapped_by_asin=remapped,
             skipped_no_match=skipped,
             file=file.filename,
             cols={"sku": sku_col, "asin": asin_col, "us": us_col, "pending": pd_col,
                   "order_date": od_col, "factory_date": fd_col, "jl": jl_col, "uc": uc_col},
             replace=replace)
    return JSONResponse({
        "status": "ok",
        "mode": "replace" if replace else "upsert",
        "rows_upserted": result["upserted"],
        "cleared_before": result["cleared_before"],
        "remapped_by_asin": remapped,
        "skipped_no_match": skipped,
        "file": file.filename,
        "columns_detected": {
            "SKU": sku_col, "ASIN": asin_col,
            "美國倉庫存": us_col, "欠數": pd_col,
            "下單日期": od_col, "工廠回覆交期": fd_col,
            "佳樂倉庫存": jl_col, "Unit/Case": uc_col,
        },
    })


# ═══════════════════════════════════════════════════════════════════════════
# GET /etl/sz/template  →  下載 SZ Upload Template.xlsx（依 Anchor 生成）
# ═══════════════════════════════════════════════════════════════════════════
@router.get("/template", summary="下載 SZ Upload Template（依當前 Anchor 動態生成）")
async def download_sz_template():
    """
    產出 SZ Warehouse Upload Template.xlsx，含 Anchor 全部 SKU + 空白庫存欄位。
    使用者下載後填入「美國倉庫存 / 佳樂倉庫存 / Unit/Case」再上傳即可。
    """
    def _generate():
        conn = new_conn()
        try:
            # v2 — 用 ASIN 對應：舊 sz_warehouse 的數字透過 sku→ASIN 對應保留
            # 就算 Anchor 換 SKU（例：CoverBuddyLite 1.2 → 1.3），舊倉庫數字也不會遺失
            rows = conn.execute("""
                WITH sku_asin AS (
                    -- 建立所有已知 SKU → ASIN 對應（Anchor + FBA + Sales，涵蓋歷史 SKU）
                    SELECT DISTINCT sku, asin FROM product_catalog
                    WHERE asin != '' AND sku != ''
                    UNION
                    SELECT DISTINCT sku, asin FROM inventory
                    WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM inventory)
                      AND sku IS NOT NULL AND asin IS NOT NULL AND sku != '' AND asin != ''
                    UNION
                    SELECT DISTINCT sku, asin FROM sales_summary
                    WHERE report_date >= CURRENT_DATE - INTERVAL 90 DAYS
                      AND sku != '' AND asin != ''
                ),
                sz_by_asin AS (
                    -- 依 ASIN 加總（涵蓋所有 SKU 變體：新 + 舊 + Amazon.Found 等）
                    -- 下單日期 & 工廠回覆交期 (Option A)：取最新那筆
                    SELECT
                        sa.asin,
                        SUM(sz.us_qty)        AS us_qty,
                        SUM(sz.pending_qty)   AS pending_qty,
                        SUM(sz.jl_qty)        AS jl_qty,
                        MAX(sz.unit_per_case) AS unit_per_case,
                        MAX(sz.order_date)    AS order_date,
                        MAX(sz.factory_confirmed_date) AS factory_confirmed_date
                    FROM sz_warehouse sz
                    JOIN sku_asin sa ON sz.sku = sa.sku
                    GROUP BY sa.asin
                )
                SELECT
                    COALESCE(pc.parent_asin, '')  AS parent_asin,
                    COALESCE(pc.asin, '')         AS asin,
                    pc.sku                        AS sku,
                    COALESCE(pc.collection, '')   AS collection,
                    COALESCE(pc.product_name, '') AS product_name,
                    COALESCE(sz.us_qty, 0)        AS us_qty,
                    COALESCE(sz.pending_qty, 0)   AS pending_qty,
                    sz.order_date                 AS order_date,
                    sz.factory_confirmed_date     AS factory_confirmed_date,
                    COALESCE(sz.jl_qty, 0)        AS jl_qty,
                    COALESCE(sz.unit_per_case, 1) AS unit_per_case
                FROM product_catalog pc
                LEFT JOIN sz_by_asin sz ON pc.asin = sz.asin
                WHERE pc.asin != ''
                ORDER BY pc.collection NULLS LAST, pc.sku
            """).fetchall()
            return rows
        finally:
            conn.close()

    try:
        rows = await asyncio.to_thread(_generate)
    except Exception as e:
        log.error("sz.template_query_failed", error=str(e))
        raise HTTPException(500, f"Template query failed: {e}")

    # 生成 xlsx
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError as e:
        raise HTTPException(500, f"openpyxl not installed: {e}")

    wb = Workbook()
    ws = wb.active
    ws.title = "SZ Upload"

    # Header — 「工廠回覆交期」放在「下單日期」後面（同一 PO 的資訊放一起）
    headers = ["(Parent) ASIN", "(Child) ASIN", "SKU", "Collection", "Name",
               "美國倉庫存", "欠數", "下單日期", "工廠回覆交期",
               "佳樂倉庫存", "Unit/Case"]
    ws.append(headers)

    hdr_font = Font(bold=True, color="FFFFFF")
    hdr_fill = PatternFill("solid", fgColor="366092")
    for cell in ws[1]:
        cell.font = hdr_font
        cell.fill = hdr_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    # Data rows — 已有的欄位會預填
    # SELECT 欄位順序：parent_asin, asin, sku, collection, product_name,
    #                us_qty, pending_qty, order_date, factory_confirmed_date, jl_qty, unit_per_case
    for r in rows:
        ws.append([r[0], r[1], r[2], r[3], r[4],
                   r[5] if r[5] else None,   # 美國倉庫存
                   r[6] if r[6] else None,   # 欠數
                   r[7] if r[7] else None,   # 下單日期
                   r[8] if r[8] else None,   # 工廠回覆交期
                   r[9] if r[9] else None,   # 佳樂倉庫存
                   r[10] if r[10] and r[10] != 1 else None])  # Unit/Case

    # Column widths
    widths = [15, 15, 22, 25, 45, 14, 12, 14, 14, 14, 12]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + i)].width = w

    ws.freeze_panes = "A2"

    try:
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
    except Exception as e:
        log.error("sz.template_save_failed", error=str(e))
        raise HTTPException(500, f"Failed to serialize xlsx: {e}")

    log.info("sz.template_generated", rows=len(rows), bytes=buf.getbuffer().nbytes)

    fname = f"SZ_Warehouse_Upload_Template_{datetime.now():%Y%m%d}.xlsx"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


# ═══════════════════════════════════════════════════════════════════════════
# GET /etl/sz/status
# ═══════════════════════════════════════════════════════════════════════════
@router.get("/status", summary="SZ 資料狀態（含資料新鮮度）")
async def sz_status():
    def _query():
        conn = new_conn()
        try:
            row = conn.execute("""
                SELECT
                    COUNT(*)                                          AS total,
                    MAX(synced_at)                                    AS last,
                    SUM(CASE WHEN us_qty > 0 THEN 1 ELSE 0 END)       AS us_stocked,
                    SUM(CASE WHEN jl_qty > 0 THEN 1 ELSE 0 END)       AS jl_stocked,
                    SUM(CASE WHEN pending_qty > 0 THEN 1 ELSE 0 END)  AS pending_skus,
                    SUM(us_qty)                                       AS us_total,
                    SUM(jl_qty)                                       AS jl_total,
                    SUM(pending_qty)                                  AS pending_total
                FROM sz_warehouse
            """).fetchone()

            last_dt = row[1]
            days_old = None
            is_stale = False
            if last_dt:
                delta = datetime.now(tz=timezone.utc) - last_dt
                days_old = delta.days
                is_stale = days_old > 7  # 7 天以上視為過期

            return {
                "total_skus":       row[0] or 0,
                "us_stocked_skus":  row[2] or 0,
                "jl_stocked_skus":  row[3] or 0,
                "pending_skus":     row[4] or 0,
                "us_total_qty":     int(row[5] or 0),
                "jl_total_qty":     int(row[6] or 0),
                "pending_total_qty": int(row[7] or 0),
                "last_upload":      str(last_dt) if last_dt else None,
                "days_old":         days_old,
                "is_stale":         is_stale,
                "stale_threshold_days": 7,
            }
        finally:
            conn.close()

    return await asyncio.to_thread(_query)


# ═══════════════════════════════════════════════════════════════════════════
# GET /etl/sz/unmatched  →  SZ 有但 Anchor 對不到的 SKU（依 ASIN mapping）
# ═══════════════════════════════════════════════════════════════════════════
@router.get("/unmatched", summary="SZ 有資料但 Anchor 對不到的 SKU（依 SKU→ASIN 判定）")
async def sz_unmatched():
    def _query():
        conn = new_conn()
        try:
            # 先建 SKU→ASIN 對應表（Anchor + FBA + Sales）
            rows = conn.execute("""
                WITH sku_asin AS (
                    SELECT DISTINCT sku, asin FROM product_catalog WHERE asin != '' AND sku != ''
                    UNION
                    SELECT DISTINCT sku, asin FROM inventory
                    WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM inventory)
                      AND sku IS NOT NULL AND asin IS NOT NULL AND sku != '' AND asin != ''
                    UNION
                    SELECT DISTINCT sku, asin FROM sales_summary
                    WHERE report_date >= CURRENT_DATE - INTERVAL 90 DAYS
                      AND sku != '' AND asin != ''
                ),
                anchor_asins AS (
                    SELECT DISTINCT asin FROM product_catalog WHERE asin != ''
                )
                SELECT
                    sz.sku,
                    sz.us_qty,
                    sz.jl_qty,
                    sa.asin      AS sz_asin,
                    CASE WHEN aa.asin IS NULL THEN 'ASIN 不在 Anchor'
                         WHEN sa.asin IS NULL THEN 'SKU 找不到 ASIN 對應'
                         ELSE 'OK'
                    END AS reason
                FROM sz_warehouse sz
                LEFT JOIN sku_asin sa ON sz.sku = sa.sku
                LEFT JOIN anchor_asins aa ON sa.asin = aa.asin
                WHERE (aa.asin IS NULL OR sa.asin IS NULL)
                  AND (sz.us_qty > 0 OR sz.jl_qty > 0)
                ORDER BY (sz.us_qty + sz.jl_qty) DESC
            """).fetchall()
            return [
                {"sku": r[0], "us_qty": r[1], "jl_qty": r[2],
                 "sz_asin": r[3], "reason": r[4]}
                for r in rows
            ]
        finally:
            conn.close()

    unmatched = await asyncio.to_thread(_query)
    return {"count": len(unmatched), "unmatched": unmatched}
