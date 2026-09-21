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
from fastapi import APIRouter, BackgroundTasks, Body, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.database import new_conn, db_write
from services.awd_service import AwdService
from services.awd_report_service import AwdReportService

log = structlog.get_logger()
router = APIRouter()
_svc = AwdService()
_report_svc = AwdReportService()


# ═══════════════════════════════════════════════════════════════════════════
# GET_AWD_INVENTORY_REPORT — 每天 n8n 觸發，比 list_inventory 更完整
# ═══════════════════════════════════════════════════════════════════════════
@router.post("/report-sync", summary="觸發 SP-API AWD Report 同步（權威真相，覆蓋 list_inventory）")
async def sync_awd_report(background_tasks: BackgroundTasks):
    """
    產出 GET_AWD_INVENTORY_REPORT，等 5-15 分鐘後自動下載 + 覆蓋 DB。
    Report 值永遠贏（Strategy A：Amazon 官方 = 真相）。
    差異會自動寫入 sync_anomalies 供你在 Dashboard 查看。

    Rate limit：Amazon 每個 marketplace 每天最多 1 次同 type report。
    """
    latest = await _report_svc.get_latest_run(pipeline="awd_report")
    if latest.get("status") == "running":
        return {"run_id": latest["id"], "status": "running",
                "message": "AWD report already running"}

    run_id = await _report_svc.start_run()
    background_tasks.add_task(_report_svc.run, run_id)
    log.info("awd_report.triggered", run_id=run_id)
    return {"run_id": run_id, "status": "started",
            "message": "AWD Report sync started (5-15 min via SP-API)"}


@router.get("/report-status", summary="查詢 AWD Report 最新一次執行狀態")
async def awd_report_status():
    return await _report_svc.get_latest_run(pipeline="awd_report")


@router.get("/report-history", summary="AWD Report 歷史紀錄")
async def awd_report_history(limit: int = 10):
    return await _report_svc.get_recent_runs(pipeline="awd_report", limit=limit)


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
# 「Outbound to FBA (units)」= 從 AWD 已在途去 FBA 的量（跟 SP-API distributedInventory 一致）
# 「Outbound Order (units)」= 已下單待出貨（尚未離開 AWD）
# 優先取「Outbound to FBA」以跟 SP-API sync 語意一致
_OUTBOUND_ALIASES = ["Outbound to FBA (units)", "Outbound Order (units)",
                     "awd_outbound", "Outbound Order"]


def _find_col(df: pd.DataFrame, aliases: list[str]) -> str | None:
    for alias in aliases:
        if alias in df.columns:
            return alias
    return None


def _detect_header_row(raw_lines: list[str]) -> int:
    """
    掃前 15 行，找出「同時含 SKU 和 ASIN 的那行」當作 header。
    這樣可以自動吃 Amazon AWD Report（前 3 行是 metadata）也能吃無 metadata 的 CSV。
    """
    for i, line in enumerate(raw_lines[:15]):
        fields = [f.strip().strip('"').lower() for f in line.split(",")]
        has_sku  = "sku" in fields or "seller sku" in fields
        has_asin = "asin" in fields or any("asin" in f for f in fields)
        if has_sku and has_asin:
            return i
    return 0   # fallback：預設沒 metadata


@router.post("/upload", summary="上傳 AWD Inventory Report（含 Amazon 官方 report 格式）")
async def upload_awd_inventory(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(400, "No file provided")

    content = await file.read()
    header_row = 0
    try:
        if file.filename.lower().endswith(".csv"):
            raw = content.decode("utf-8", errors="ignore").splitlines()
            header_row = _detect_header_row(raw)
            df = pd.read_csv(io.BytesIO(content), dtype=str, skiprows=header_row)
        else:
            # Excel：先讀進來檢查前幾列
            probe = pd.read_excel(io.BytesIO(content), dtype=str, header=None, nrows=15)
            for i in range(len(probe)):
                row_vals = [str(v).strip().lower() for v in probe.iloc[i].tolist() if pd.notna(v)]
                if ("sku" in row_vals or "seller sku" in row_vals) and any("asin" in v for v in row_vals):
                    header_row = i
                    break
            df = pd.read_excel(io.BytesIO(content), dtype=str, skiprows=header_row)
    except Exception as e:
        raise HTTPException(400, f"Failed to parse file: {e}")
    log.info("awd.upload_parsed", file=file.filename, header_row=header_row, rows=len(df))

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

    # ── 讓 Dashboard「AWD Report (權威)」健康度也顯示本次手動上傳 ────
    # 手動 CSV upload 語意上等同 GET_AWD_INVENTORY_REPORT（都是 Amazon 官方 report）
    # 所以記錄到 awd_report pipeline，讓資料串接健康度 panel 更新
    try:
        run_id = await _report_svc.start_run(source="manual_csv", file=file.filename)
        await _report_svc.finish_run(run_id, count)
    except Exception as e:
        log.warning("awd.report_run_record_failed", error=str(e))

    log.info("awd_inventory.uploaded", rows=count, file=file.filename)
    return JSONResponse({"status": "ok", "rows_upserted": count, "file": file.filename,
                         "columns_detected": {
                             "sku": sku_col, "available": avail_col,
                             "inbound": inbound_col, "outbound": outbound_col
                         },
                         "note": "已記錄為 AWD Report 事件，健康度 panel 會反映"})


class AwdManualAdjustRequest(BaseModel):
    sku: str = Field(..., description="Seller SKU（會直接寫入 awd_inventory）")
    awd_available: int = Field(0, ge=0, description="Amazon 後台 On-hand quantity")
    awd_inbound:   int = Field(0, ge=0, description="Amazon 後台 Inbound quantity")
    awd_outbound:  int = Field(0, ge=0, description="Amazon 後台 Reserved for outbound / Distributed")


@router.post("/manual-adjust", summary="手動修正單一 SKU 的 AWD 數值（SP-API 漏傳時用）")
async def awd_manual_adjust(body: AwdManualAdjustRequest):
    """
    當 SP-API AWD list_inventory 偶爾漏傳某 SKU（分頁遺漏 / rate limit）時，
    可用此端點手動同步 Amazon 後台的實際數字。

    - 若該 SKU 已存在 → 覆蓋數字，更新 synced_at
    - 若不存在 → 新增一筆
    - synced_at 設為當下，避免軟歸零邏輯 7 天後把它清掉

    範例:
        curl -X POST http://localhost:8000/etl/awd/manual-adjust \\
          -H 'Content-Type: application/json' \\
          -d '{"sku":"MPHIPH218BK23","awd_available":0,"awd_inbound":468,"awd_outbound":0}'
    """
    now = datetime.now(tz=timezone.utc)

    def _write():
        conn = new_conn()
        try:
            conn.execute("""
                INSERT INTO awd_inventory (sku, awd_available, awd_inbound, awd_outbound, synced_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (sku) DO UPDATE SET
                    awd_available = excluded.awd_available,
                    awd_inbound   = excluded.awd_inbound,
                    awd_outbound  = excluded.awd_outbound,
                    synced_at     = excluded.synced_at
            """, [body.sku, body.awd_available, body.awd_inbound, body.awd_outbound, now])
            # 讀回確認
            row = conn.execute("""
                SELECT sku, awd_available, awd_inbound, awd_outbound, synced_at
                FROM awd_inventory WHERE sku = ?
            """, [body.sku]).fetchone()
            conn.commit()
            return row
        finally:
            conn.close()

    row = await db_write(_write)
    log.info("awd.manual_adjust",
             sku=body.sku,
             avail=body.awd_available,
             inbound=body.awd_inbound,
             outbound=body.awd_outbound)
    return {
        "status": "ok",
        "sku": row[0],
        "awd_available": row[1],
        "awd_inbound":   row[2],
        "awd_outbound":  row[3],
        "synced_at":     str(row[4]),
        "note": "已寫入，synced_at 更新至現在。下次 SP-API sync 若回傳此 SKU 會覆蓋；若沒回傳則保留（7 天內不歸零）",
    }


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
