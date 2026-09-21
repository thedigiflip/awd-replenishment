"""
Sync Health & Anomalies Router
─────────────────────────────────────────────────────────────
- GET  /etl/health/summary       → 各 pipeline 狀態摘要（Dashboard 用）
- GET  /etl/anomalies             → 資料異常清單（含 severity 過濾）
- POST /etl/anomalies/ack         → 標記異常已處理（acknowledged）
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

import structlog
from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel

from core.database import new_conn, db_write

log = structlog.get_logger()
router = APIRouter()


# ═══════════════════════════════════════════════════════════════════════════
# GET /etl/health/summary
# ═══════════════════════════════════════════════════════════════════════════
@router.get("/summary", summary="各資料源同步健康度摘要")
async def health_summary(
    marketplace_id: Optional[str] = Query(None, description="限定 marketplace（不傳→全站點聚合）"),
):
    """
    給 Dashboard「同步健康度」panel 用。
    - marketplace_id 傳入 → 只計算該站點的 FBA / Sales（AWD/SZ 跨站共用不受影響）
    - marketplace_id 不傳 → 全站聚合（相容舊行為）
    """
    def _q():
        conn = new_conn()
        try:
            # Anchor total
            anchor_total = conn.execute(
                "SELECT COUNT(*) FROM product_catalog WHERE asin != ''"
            ).fetchone()[0]

            # AWD（跨站共用，US-only 服務）
            awd = conn.execute("""
                SELECT COUNT(*), MAX(synced_at),
                       SUM(CASE WHEN awd_available > 0 OR awd_inbound > 0 THEN 1 ELSE 0 END),
                       COUNT(DISTINCT a.sku) FILTER (WHERE pc.sku IS NOT NULL)
                FROM awd_inventory a
                LEFT JOIN product_catalog pc ON a.sku = pc.sku
            """).fetchone()

            # FBA — 依 marketplace 限縮（若沒指定則全站聚合）
            if marketplace_id:
                fba = conn.execute("""
                    WITH latest AS (
                        SELECT * FROM inventory
                        WHERE marketplace_id = ?
                          AND snapshot_date = (
                              SELECT MAX(snapshot_date) FROM inventory
                              WHERE marketplace_id = ?
                          )
                    )
                    SELECT COUNT(DISTINCT l.sku), MAX(l.synced_at),
                           SUM(CASE WHEN l.fulfillable_quantity > 0 THEN 1 ELSE 0 END),
                           COUNT(DISTINCT l.sku) FILTER (WHERE pc.sku IS NOT NULL)
                    FROM latest l
                    LEFT JOIN product_catalog pc ON l.sku = pc.sku
                """, [marketplace_id, marketplace_id]).fetchone()
            else:
                fba = conn.execute("""
                    WITH latest AS (
                        SELECT * FROM inventory
                        WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM inventory)
                    )
                    SELECT COUNT(DISTINCT l.sku), MAX(l.synced_at),
                           SUM(CASE WHEN l.fulfillable_quantity > 0 THEN 1 ELSE 0 END),
                           COUNT(DISTINCT l.sku) FILTER (WHERE pc.sku IS NOT NULL)
                    FROM latest l
                    LEFT JOIN product_catalog pc ON l.sku = pc.sku
                """).fetchone()

            # Sales — 依 marketplace 限縮
            if marketplace_id:
                sales = conn.execute("""
                    SELECT COUNT(DISTINCT s.sku), MAX(s.synced_at),
                           COUNT(DISTINCT s.sku) FILTER (WHERE s.units_sold > 0),
                           COUNT(DISTINCT s.sku) FILTER (WHERE pc.sku IS NOT NULL)
                    FROM sales_summary s
                    LEFT JOIN product_catalog pc ON s.sku = pc.sku
                    WHERE s.report_date >= CURRENT_DATE - INTERVAL 30 DAYS
                      AND s.marketplace_id = ?
                """, [marketplace_id]).fetchone()
            else:
                sales = conn.execute("""
                    SELECT COUNT(DISTINCT s.sku), MAX(s.synced_at),
                           COUNT(DISTINCT s.sku) FILTER (WHERE s.units_sold > 0),
                           COUNT(DISTINCT s.sku) FILTER (WHERE pc.sku IS NOT NULL)
                    FROM sales_summary s
                    LEFT JOIN product_catalog pc ON s.sku = pc.sku
                    WHERE s.report_date >= CURRENT_DATE - INTERVAL 30 DAYS
                """).fetchone()

            # SZ warehouse
            sz = conn.execute("""
                SELECT COUNT(*), MAX(synced_at),
                       SUM(CASE WHEN us_qty > 0 OR jl_qty > 0 THEN 1 ELSE 0 END),
                       COUNT(DISTINCT sz.sku) FILTER (WHERE pc.sku IS NOT NULL)
                FROM sz_warehouse sz
                LEFT JOIN product_catalog pc ON sz.sku = pc.sku
            """).fetchone()

            # Anomalies count by pipeline (last 24h, unacknowledged only)
            anomaly_counts = conn.execute("""
                SELECT pipeline,
                       SUM(CASE WHEN severity='critical' THEN 1 ELSE 0 END) AS critical,
                       SUM(CASE WHEN severity='warning'  THEN 1 ELSE 0 END) AS warning
                FROM sync_anomalies
                WHERE detected_at >= now() - INTERVAL 24 HOURS
                  AND acknowledged = FALSE
                GROUP BY pipeline
            """).fetchall()
            ac = {r[0]: {"critical": r[1] or 0, "warning": r[2] or 0} for r in anomaly_counts}

            return anchor_total, awd, fba, sales, sz, ac
        finally:
            conn.close()

    anchor_total, awd, fba, sales, sz, ac = await asyncio.to_thread(_q)

    # AWD Report + FBA Report 最新一次執行（存在 SQLite db_meta，另外查）
    from core import db_meta
    try:
        report_run = await db_meta.get_latest("awd_report")
    except Exception:
        report_run = {}
    try:
        fba_report_run = await db_meta.get_latest("fba_report")
    except Exception:
        fba_report_run = {}
    now = datetime.now(tz=timezone.utc)

    def _fmt(name, row, key):
        total, last, stocked, matched = row
        total    = total or 0
        stocked  = stocked or 0
        matched  = matched or 0
        days_old = None
        if last:
            days_old = (now - last).days
        # 覆蓋率 = 這個資料源匹配到 Anchor 的 SKU 數 / Anchor 總數
        coverage = round(matched / anchor_total * 100, 1) if anchor_total > 0 else 0
        anomaly = ac.get(key, {"critical": 0, "warning": 0})
        # 狀態判定
        status = "ok"
        reason = []
        if days_old is not None and days_old > 7:
            status = "critical"; reason.append(f"{days_old} 天沒同步")
        elif days_old is not None and days_old > 2:
            status = "warning"; reason.append(f"{days_old} 天沒同步")
        if anomaly["critical"] > 0:
            status = "critical"; reason.append(f"{anomaly['critical']} 個 critical 異常")
        elif anomaly["warning"] > 0 and status == "ok":
            status = "warning"; reason.append(f"{anomaly['warning']} 個 warning 異常")
        return {
            "name":             name,
            "pipeline_key":     key,
            "total_skus":       total,
            "stocked_skus":     stocked,
            "matched_to_anchor": matched,
            "coverage_pct":     coverage,
            "last_sync":        str(last) if last else None,
            "days_old":         days_old,
            "is_stale":         (days_old or 0) > 7,
            "anomalies_24h":    anomaly,
            "status":           status,
            "reason":           " / ".join(reason) if reason else "正常",
        }

    # AWD Report 特殊處理（不是 DuckDB 表，而是執行紀錄）
    report_last_run = None
    if report_run and report_run.get("finished_at"):
        try:
            report_last_run = datetime.fromisoformat(
                report_run["finished_at"].replace("Z", "+00:00")
            )
        except Exception:
            report_last_run = None
    report_status = "ok"
    report_reason = "正常"
    report_days_old = None
    if report_last_run:
        report_days_old = (now - report_last_run).days
        if report_days_old > 2:
            report_status = "critical"; report_reason = f"{report_days_old} 天沒跑"
        elif report_days_old > 1:
            report_status = "warning"; report_reason = f"{report_days_old} 天沒跑"
    else:
        report_status = "warning"; report_reason = "從未執行"

    report_anomaly = ac.get("awd_report", {"critical": 0, "warning": 0})
    if report_anomaly["critical"] > 0:
        report_status = "critical"; report_reason = f"{report_anomaly['critical']} 個 critical 異常"

    awd_report_row = {
        "name":              "AWD Report（權威）",
        "pipeline_key":      "awd_report",
        "total_skus":        None,
        "stocked_skus":      None,
        "matched_to_anchor": None,
        "coverage_pct":      None,
        "last_sync":         str(report_last_run) if report_last_run else None,
        "days_old":          report_days_old,
        "is_stale":          (report_days_old or 0) > 2,
        "anomalies_24h":     report_anomaly,
        "status":            report_status,
        "reason":            report_reason,
    }

    # FBA Report（權威）— 手動 CSV 上傳 or Report API
    fba_report_last_run = None
    if fba_report_run and fba_report_run.get("finished_at"):
        try:
            fba_report_last_run = datetime.fromisoformat(
                fba_report_run["finished_at"].replace("Z", "+00:00")
            )
        except Exception:
            fba_report_last_run = None
    fba_report_status = "ok"
    fba_report_reason = "正常"
    fba_report_days_old = None
    if fba_report_last_run:
        fba_report_days_old = (now - fba_report_last_run).days
        if fba_report_days_old > 2:
            fba_report_status = "critical"; fba_report_reason = f"{fba_report_days_old} 天沒跑"
        elif fba_report_days_old > 1:
            fba_report_status = "warning"; fba_report_reason = f"{fba_report_days_old} 天沒跑"
    else:
        fba_report_status = "warning"; fba_report_reason = "從未執行"

    fba_report_anomaly = ac.get("fba_report", {"critical": 0, "warning": 0})
    if fba_report_anomaly["critical"] > 0:
        fba_report_status = "critical"
        fba_report_reason = f"{fba_report_anomaly['critical']} 個 critical 異常"

    fba_report_row = {
        "name":              "FBA Report（權威）",
        "pipeline_key":      "fba_report",
        "total_skus":        None,
        "stocked_skus":      None,
        "matched_to_anchor": None,
        "coverage_pct":      None,
        "last_sync":         str(fba_report_last_run) if fba_report_last_run else None,
        "days_old":          fba_report_days_old,
        "is_stale":          (fba_report_days_old or 0) > 2,
        "anomalies_24h":     fba_report_anomaly,
        "status":            fba_report_status,
        "reason":            fba_report_reason,
    }

    return {
        "anchor_total": anchor_total,
        "checked_at":   now.isoformat(),
        "pipelines": [
            _fmt("FBA Inventory (API)",   fba,   "fba_inventory"),
            fba_report_row,
            _fmt("AWD Inventory (API)",   awd,   "awd_inventory"),
            awd_report_row,
            _fmt("Sales Summary",         sales, "sales_summary"),
            _fmt("SZ Warehouse",          sz,    "sz_warehouse"),
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════
# GET /etl/anomalies
# ═══════════════════════════════════════════════════════════════════════════
@router.get("/anomalies", summary="資料異常清單")
async def list_anomalies(
    since_hours: int = Query(24, description="回傳最近 N 小時內偵測的異常"),
    pipeline: Optional[str] = Query(None, description="過濾 pipeline"),
    severity: Optional[str] = Query(None, description="critical / warning / info"),
    marketplace_id: Optional[str] = Query(None, description="過濾 marketplace_id（空字串代表跨站異常）"),
    unacked_only: bool = Query(True, description="只回未處理的"),
    limit: int = Query(200, description="最多回幾筆"),
):
    def _q():
        conn = new_conn()
        try:
            sql = """
                SELECT id, detected_at, pipeline, sku,
                       COALESCE(marketplace_id, '') AS marketplace_id,
                       field,
                       old_value, new_value, change_pct, severity, reason, acknowledged
                FROM sync_anomalies
                WHERE detected_at >= now() - INTERVAL '{h} HOURS'
            """.format(h=since_hours)
            params = []
            if pipeline:
                sql += " AND pipeline = ?"; params.append(pipeline)
            if severity:
                sql += " AND severity = ?"; params.append(severity)
            if marketplace_id:
                # 顯示指定 mp 的異常 + 跨站異常（marketplace_id 空 = AWD 這類跨站）
                sql += " AND (COALESCE(marketplace_id,'') = ? OR COALESCE(marketplace_id,'') = '')"
                params.append(marketplace_id)
            if unacked_only:
                sql += " AND acknowledged = FALSE"
            sql += " ORDER BY detected_at DESC, id DESC LIMIT ?"; params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return rows
        finally:
            conn.close()

    rows = await asyncio.to_thread(_q)
    return {
        "count":     len(rows),
        "anomalies": [
            {
                "id":             r[0],
                "detected_at":    str(r[1]),
                "pipeline":       r[2],
                "sku":            r[3],
                "marketplace_id": r[4],
                "field":          r[5],
                "old_value":      r[6],
                "new_value":      r[7],
                "change_pct":     r[8],
                "severity":       r[9],
                "reason":         r[10],
                "acknowledged":   r[11],
            }
            for r in rows
        ],
    }


class AckRequest(BaseModel):
    ids: list[int]


@router.post("/anomalies/ack", summary="標記異常為已處理")
async def ack_anomalies(body: AckRequest):
    if not body.ids:
        raise HTTPException(400, "ids 不能空")

    def _write():
        conn = new_conn()
        try:
            placeholders = ",".join(["?"] * len(body.ids))
            conn.execute(
                f"UPDATE sync_anomalies SET acknowledged = TRUE WHERE id IN ({placeholders})",
                body.ids,
            )
            conn.commit()
            return len(body.ids)
        finally:
            conn.close()

    n = await db_write(_write)
    return {"status": "ok", "acknowledged": n}
