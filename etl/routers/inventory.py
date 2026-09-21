"""
Inventory ETL Router
POST /etl/inventory/sync  — 同步 FBA 庫存快照（每日執行）
GET  /etl/inventory/status
"""

import io
from datetime import date, datetime, timezone
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import pandas as pd
import structlog

from services.inventory_service import InventoryService
from services.base_service import BaseService
from core.database import new_conn, db_write

router = APIRouter()
log = structlog.get_logger()


class InventorySyncRequest(BaseModel):
    snapshot_date: date | None = None       # 若不填則使用今天
    granularity: str = "Marketplace"        # Marketplace | ASIN
    start_datetime: str | None = None       # ISO8601, 增量同步用


class SyncResponse(BaseModel):
    run_id: int
    status: str
    message: str


@router.post("/sync", response_model=SyncResponse, summary="觸發庫存快照同步")
async def sync_inventory(req: InventorySyncRequest, background_tasks: BackgroundTasks):
    snapshot_date = req.snapshot_date or date.today()
    service = InventoryService()
    run_id = await service.start_run(snapshot_date=snapshot_date)

    background_tasks.add_task(
        service.run,
        run_id=run_id,
        snapshot_date=snapshot_date,
        granularity=req.granularity,
        start_datetime=req.start_datetime,
    )

    log.info("inventory.sync_triggered", run_id=run_id, snapshot_date=str(snapshot_date))
    return SyncResponse(run_id=run_id, status="running", message="Inventory sync started")


@router.get("/status", summary="查詢最近庫存同步狀態")
async def get_status():
    """回傳最新一次執行紀錄（單一物件，供 n8n Success? 節點判斷用）。"""
    service = InventoryService()
    return await service.get_latest_run(pipeline="inventory")


@router.get("/history", summary="查詢最近 N 次庫存同步紀錄")
async def get_history(limit: int = 5):
    service = InventoryService()
    return await service.get_recent_runs(pipeline="inventory", limit=limit)


@router.get("/count", summary="查詢 DuckDB 庫存筆數")
async def get_count():
    service = InventoryService()
    return await service.get_count()


@router.get("/debug/{asin_or_sku}", summary="Diagnose: 依 ASIN 或 SKU 查完整庫存歷史")
async def debug_asin_or_sku(asin_or_sku: str):
    """
    診斷用 — 依 ASIN 為主 key 查完整庫存歷史。
    支援 ASIN (優先) or SKU 都能查。
    """
    def _query():
        conn = new_conn()
        try:
            # 先試 ASIN 查
            rows = conn.execute("""
                SELECT snapshot_date, marketplace_id, asin, sku,
                       fulfillable_quantity, inbound_working, inbound_shipped,
                       inbound_receiving, reserved_fc_transfers, reserved_fc_processing,
                       total_quantity, synced_at
                FROM inventory
                WHERE asin = ? OR sku = ?
                ORDER BY snapshot_date DESC, marketplace_id, synced_at DESC
                LIMIT 30
            """, [asin_or_sku, asin_or_sku]).fetchall()
            latest = conn.execute("SELECT MAX(snapshot_date) FROM inventory").fetchone()[0]

            # 也看 Anchor 有沒有這個 ASIN
            anchor = conn.execute("""
                SELECT sku, asin, product_name FROM product_catalog
                WHERE asin = ? OR sku = ?
            """, [asin_or_sku, asin_or_sku]).fetchall()

            return {
                "query":                asin_or_sku,
                "latest_snapshot_in_db": str(latest),
                "anchor_match":         [{"sku": r[0], "asin": r[1], "name": r[2]} for r in anchor],
                "inventory_rows":       [
                    {"snapshot_date": str(r[0]), "marketplace_id": r[1], "asin": r[2], "sku": r[3],
                     "fulfillable": r[4], "inbound_working": r[5], "inbound_shipped": r[6],
                     "inbound_receiving": r[7], "fc_transfer": r[8], "fc_processing": r[9],
                     "total": r[10], "synced_at": str(r[11])}
                    for r in rows
                ],
            }
        finally:
            conn.close()
    import asyncio
    return await asyncio.to_thread(_query)


class FbaManualAdjustRequest(BaseModel):
    sku: str = Field(..., description="Seller SKU")
    asin: str = Field("", description="ASIN（可選，若同 SKU 有多 ASIN 或建立新紀錄時用）")
    fulfillable_quantity:    int = Field(0, ge=0, description="Available 現貨")
    inbound_working:          int = Field(0, ge=0)
    inbound_shipped:          int = Field(0, ge=0)
    inbound_receiving:        int = Field(0, ge=0)
    reserved_fc_transfers:    int = Field(0, ge=0)
    reserved_fc_processing:   int = Field(0, ge=0)
    marketplace_id:           str = Field("ATVPDKIKX0DER")


@router.post("/manual-adjust", summary="手動修正單一 SKU 的 FBA 庫存（SP-API 漏傳時用）")
async def fba_manual_adjust(body: FbaManualAdjustRequest):
    """
    寫入或覆蓋 inventory 表「最新 snapshot_date」該 SKU 的欄位。
    - 若最新 snapshot 已有此 SKU → 覆蓋
    - 若沒有 → 用今天日期新增一筆
    - synced_at 更新至現在
    """
    now = datetime.now(tz=timezone.utc)

    def _write():
        conn = new_conn()
        try:
            latest_date = conn.execute(
                "SELECT MAX(snapshot_date) FROM inventory WHERE marketplace_id = ?",
                [body.marketplace_id],
            ).fetchone()[0]
            if not latest_date:
                latest_date = date.today()
            # 這個 SKU 在 latest snapshot 是否存在？取 asin
            existing = conn.execute("""
                SELECT asin FROM inventory
                WHERE snapshot_date = ? AND sku = ? AND marketplace_id = ?
                LIMIT 1
            """, [latest_date, body.sku, body.marketplace_id]).fetchone()
            asin = body.asin or (existing[0] if existing else "")
            total = (body.fulfillable_quantity + body.inbound_working +
                     body.inbound_shipped + body.inbound_receiving +
                     body.reserved_fc_transfers + body.reserved_fc_processing)
            conn.execute("""
                INSERT INTO inventory
                    (snapshot_date, asin, fnsku, sku, product_name, condition,
                     fulfillable_quantity, inbound_working, inbound_shipped, inbound_receiving,
                     reserved_fc_transfers, reserved_fc_processing,
                     total_quantity, marketplace_id, raw_json, synced_at)
                VALUES (?, ?, '', ?, '', 'New', ?, ?, ?, ?, ?, ?, ?, ?, '{"manual":true}', ?)
                ON CONFLICT (snapshot_date, asin, sku, marketplace_id) DO UPDATE SET
                    fulfillable_quantity   = excluded.fulfillable_quantity,
                    inbound_working        = excluded.inbound_working,
                    inbound_shipped        = excluded.inbound_shipped,
                    inbound_receiving      = excluded.inbound_receiving,
                    reserved_fc_transfers  = excluded.reserved_fc_transfers,
                    reserved_fc_processing = excluded.reserved_fc_processing,
                    total_quantity         = excluded.total_quantity,
                    synced_at              = excluded.synced_at
            """, [latest_date, asin, body.sku,
                  body.fulfillable_quantity, body.inbound_working,
                  body.inbound_shipped, body.inbound_receiving,
                  body.reserved_fc_transfers, body.reserved_fc_processing,
                  total, body.marketplace_id, now])
            conn.commit()
            return {"snapshot_date": str(latest_date), "asin": asin, "total": total}
        finally:
            conn.close()

    r = await db_write(_write)
    log.info("fba.manual_adjust", sku=body.sku, asin=r["asin"], total=r["total"])
    return {"status": "ok", "sku": body.sku, **r}


# ═══════════════════════════════════════════════════════════════════════════
# POST /etl/inventory/remap-marketplace  ─ 修正錯站點的 inventory 資料
# ═══════════════════════════════════════════════════════════════════════════

class RemapMarketplaceRequest(BaseModel):
    snapshot_date: date = Field(..., description="要修的 snapshot_date")
    from_marketplace: str = Field(..., description="錯的 marketplace_id")
    to_marketplace: str = Field(..., description="正確的 marketplace_id")


@router.post("/remap-marketplace", summary="修正錯站點的 inventory 資料（單日）")
async def remap_marketplace(body: RemapMarketplaceRequest):
    """
    把某一天的 inventory row 從 from_marketplace 改成 to_marketplace。
    用途：上傳 CSV 時不小心選錯站點，事後補救。
    """
    def _write():
        conn = new_conn()
        try:
            # 先算會影響幾筆
            before = conn.execute("""
                SELECT COUNT(*) FROM inventory
                WHERE snapshot_date = ? AND marketplace_id = ?
            """, [body.snapshot_date, body.from_marketplace]).fetchone()[0]

            # 若目的地已有同一 (date, asin, sku) 資料 → 先刪除避免 PK 衝突
            # DuckDB 對多欄 IN subquery 敏感，改用 EXISTS
            conn.execute("""
                DELETE FROM inventory t
                WHERE t.snapshot_date = ? AND t.marketplace_id = ?
                  AND EXISTS (
                      SELECT 1 FROM inventory s
                      WHERE s.snapshot_date = ?
                        AND s.marketplace_id = ?
                        AND s.asin = t.asin
                        AND s.sku  = t.sku
                  )
            """, [body.snapshot_date, body.to_marketplace,
                  body.snapshot_date, body.from_marketplace])

            conn.execute("""
                UPDATE inventory
                SET marketplace_id = ?
                WHERE snapshot_date = ? AND marketplace_id = ?
            """, [body.to_marketplace, body.snapshot_date, body.from_marketplace])
            conn.commit()
            return {"moved_rows": before}
        finally:
            conn.close()

    result = await db_write(_write)
    log.info("inventory.remap_marketplace",
             date=str(body.snapshot_date),
             from_mp=body.from_marketplace, to_mp=body.to_marketplace,
             moved=result["moved_rows"])
    return {
        "status": "ok",
        "snapshot_date": str(body.snapshot_date),
        "from": body.from_marketplace,
        "to": body.to_marketplace,
        **result,
    }


# ═══════════════════════════════════════════════════════════════════════════
# POST /etl/inventory/upload  ─  上傳 Amazon FBA Inventory CSV
# ═══════════════════════════════════════════════════════════════════════════
# 支援 Amazon Seller Central「Manage FBA Inventory / Inventory Health」報告
# 欄位對應（自動偵測 alias）:
#   sku, fnsku, asin, product-name, snapshot-date, available, fc-transfer,
#   inbound-working, inbound-shipped, inbound-received, Reserved FC Processing

_FBA_SKU_ALIASES        = ["sku", "SKU", "seller-sku", "Seller SKU"]
_FBA_FNSKU_ALIASES      = ["fnsku", "FNSKU"]
_FBA_ASIN_ALIASES       = ["asin", "ASIN"]
_FBA_NAME_ALIASES       = ["product-name", "Product Name", "product_name"]
_FBA_SNAPSHOT_ALIASES   = ["snapshot-date", "snapshot_date", "Snapshot Date"]
_FBA_AVAIL_ALIASES      = ["available", "Available", "fulfillable_quantity"]
_FBA_FCTR_ALIASES       = ["fc-transfer", "reserved_fc_transfers", "FC Transfer"]
_FBA_FCPR_ALIASES       = ["Reserved FC Processing", "reserved_fc_processing", "fc-processing"]
_FBA_INB_WORK_ALIASES   = ["inbound-working", "inbound_working"]
_FBA_INB_SHIP_ALIASES   = ["inbound-shipped", "inbound_shipped"]
_FBA_INB_RECV_ALIASES   = ["inbound-received", "inbound_receiving", "inbound-receiving"]


def _find_col(df: pd.DataFrame, aliases: list[str]) -> str | None:
    for a in aliases:
        if a in df.columns:
            return a
    return None


def _fba_detect_header_row(raw_lines: list[str]) -> int:
    """掃前 15 行找出同時含 sku 和 asin 的 header 行。"""
    for i, line in enumerate(raw_lines[:15]):
        fields = [f.strip().strip('"﻿').lower() for f in line.split(",")]
        has_sku  = "sku" in fields or "seller-sku" in fields or "seller sku" in fields
        has_asin = "asin" in fields or any("asin" in f for f in fields)
        if has_sku and has_asin:
            return i
    return 0


def _safe_int(val, default=0):
    try:
        if pd.isna(val): return default
        s = str(val).strip().replace(",", "").replace('"', '')
        if s == "" or s.lower() == "nan": return default
        return int(float(s))
    except Exception:
        return default


class _FbaReportRun(BaseService):
    """記錄 FBA CSV 上傳事件到 db_meta（讓健康度 panel 顯示）"""
    pipeline = "fba_report"


_fba_report_run = _FbaReportRun()


@router.post("/upload", summary="上傳 Amazon FBA Inventory CSV（覆蓋 upsert）")
async def upload_fba_inventory(
    file: UploadFile = File(...),
    marketplace_id_query: str | None = Query(None, alias="marketplace_id"),
    marketplace_id_form:  str | None = Form(None,  alias="marketplace_id"),
):
    """
    支援 Amazon Seller Central「Manage FBA Inventory」CSV 直接上傳。
    - marketplace_id 可從 query string 或 form field 傳入（皆可）
    - 自動偵測 header 行
    - Upsert 到 inventory 表（依 (snapshot_date, asin, sku, marketplace_id) 為 PK）
    - 記錄一筆 fba_report run，讓 Dashboard 健康度顯示「FBA Report (權威)」
    """
    # 優先順序：form > query > default，讓明確 form field 蓋掉 URL 參數
    marketplace_id = marketplace_id_form or marketplace_id_query or "ATVPDKIKX0DER"

    if not file.filename:
        raise HTTPException(400, "No file provided")

    content = await file.read()
    header_row = 0
    try:
        if file.filename.lower().endswith(".csv"):
            raw = content.decode("utf-8-sig", errors="ignore").splitlines()
            header_row = _fba_detect_header_row(raw)
            df = pd.read_csv(io.BytesIO(content), dtype=str, skiprows=header_row, encoding="utf-8-sig")
        else:
            probe = pd.read_excel(io.BytesIO(content), dtype=str, header=None, nrows=15)
            for i in range(len(probe)):
                vals = [str(v).strip().lower() for v in probe.iloc[i].tolist() if pd.notna(v)]
                if ("sku" in vals or "seller-sku" in vals) and any("asin" in v for v in vals):
                    header_row = i
                    break
            df = pd.read_excel(io.BytesIO(content), dtype=str, skiprows=header_row)
    except Exception as e:
        raise HTTPException(400, f"Failed to parse file: {e}")

    df.columns = [str(c).strip().replace('﻿', '') for c in df.columns]
    log.info("fba.upload_parsed", file=file.filename, header_row=header_row, rows=len(df))

    sku_col   = _find_col(df, _FBA_SKU_ALIASES)
    fnsku_col = _find_col(df, _FBA_FNSKU_ALIASES)
    asin_col  = _find_col(df, _FBA_ASIN_ALIASES)
    name_col  = _find_col(df, _FBA_NAME_ALIASES)
    snap_col  = _find_col(df, _FBA_SNAPSHOT_ALIASES)
    avail_col = _find_col(df, _FBA_AVAIL_ALIASES)
    fctr_col  = _find_col(df, _FBA_FCTR_ALIASES)
    fcpr_col  = _find_col(df, _FBA_FCPR_ALIASES)
    inw_col   = _find_col(df, _FBA_INB_WORK_ALIASES)
    ish_col   = _find_col(df, _FBA_INB_SHIP_ALIASES)
    irc_col   = _find_col(df, _FBA_INB_RECV_ALIASES)

    if not sku_col or not avail_col:
        raise HTTPException(
            422,
            f"必要欄位缺少。SKU 欄位={sku_col}，available 欄位={avail_col}\n"
            f"實際欄位: {list(df.columns)[:20]}...",
        )

    # snapshot_date：檔案有 → 用檔案；沒有 → 用今天
    def _parse_snap(val):
        if pd.isna(val): return None
        try:
            return datetime.strptime(str(val).strip()[:10], "%Y-%m-%d").date()
        except Exception:
            return None

    default_snap = date.today()
    now_ts = datetime.now(tz=timezone.utc)

    rows = []
    for _, row in df.iterrows():
        sku = str(row[sku_col]).strip() if pd.notna(row[sku_col]) else ""
        if not sku or sku.lower() == "nan":
            continue
        asin = str(row.get(asin_col, "")).strip() if asin_col else ""
        snap = _parse_snap(row.get(snap_col)) if snap_col else default_snap
        snap = snap or default_snap
        fulfillable = _safe_int(row.get(avail_col))
        fc_transfer = _safe_int(row.get(fctr_col)) if fctr_col else 0
        fc_processing = _safe_int(row.get(fcpr_col)) if fcpr_col else 0
        inb_working  = _safe_int(row.get(inw_col)) if inw_col else 0
        inb_shipped  = _safe_int(row.get(ish_col)) if ish_col else 0
        inb_recv     = _safe_int(row.get(irc_col)) if irc_col else 0
        total = fulfillable + fc_transfer + fc_processing + inb_working + inb_shipped + inb_recv
        rows.append((
            snap, asin,
            str(row.get(fnsku_col, "")).strip() if fnsku_col else "",
            sku,
            str(row.get(name_col, "")).strip() if name_col else "",
            "New",
            fulfillable, inb_working, inb_shipped, inb_recv,
            fc_transfer, fc_processing,
            total,
            marketplace_id,
            '{"source":"manual_csv"}',
            now_ts,
        ))

    if not rows:
        raise HTTPException(422, "沒有有效資料列")

    def _write():
        conn = new_conn()
        try:
            conn.executemany("""
                INSERT INTO inventory
                    (snapshot_date, asin, fnsku, sku, product_name, condition,
                     fulfillable_quantity, inbound_working, inbound_shipped, inbound_receiving,
                     reserved_fc_transfers, reserved_fc_processing,
                     total_quantity, marketplace_id, raw_json, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (snapshot_date, asin, sku, marketplace_id) DO UPDATE SET
                    fnsku                  = excluded.fnsku,
                    product_name           = excluded.product_name,
                    fulfillable_quantity   = excluded.fulfillable_quantity,
                    inbound_working        = excluded.inbound_working,
                    inbound_shipped        = excluded.inbound_shipped,
                    inbound_receiving      = excluded.inbound_receiving,
                    reserved_fc_transfers  = excluded.reserved_fc_transfers,
                    reserved_fc_processing = excluded.reserved_fc_processing,
                    total_quantity         = excluded.total_quantity,
                    synced_at              = excluded.synced_at
            """, rows)
            conn.commit()
            return len(rows)
        finally:
            conn.close()

    count = await db_write(_write)

    # 記錄 fba_report run 事件（讓 Dashboard 健康度顯示）
    try:
        run_id = await _fba_report_run.start_run(source="manual_csv", file=file.filename)
        await _fba_report_run.finish_run(run_id, count)
    except Exception as e:
        log.warning("fba.report_run_record_failed", error=str(e))

    log.info("fba.uploaded", rows=count, file=file.filename, header_row=header_row)
    return JSONResponse({
        "status": "ok",
        "rows_upserted": count,
        "file": file.filename,
        "header_row": header_row,
        "columns_detected": {
            "sku": sku_col, "asin": asin_col, "snapshot": snap_col,
            "available": avail_col, "fc_transfer": fctr_col,
            "fc_processing": fcpr_col,
            "inbound_working": inw_col, "inbound_shipped": ish_col,
            "inbound_received": irc_col,
        },
        "note": "已記錄為 FBA Report 事件，健康度 panel 會反映",
    })
