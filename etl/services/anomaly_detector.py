"""
Sync Anomaly Detector
─────────────────────────────────────────────────────────────────
每個資料 pipeline (AWD / FBA / Sales) 都在 upsert 前後呼叫
`detect_and_record_anomalies`，若偵測到異常會寫入 sync_anomalies 表。

觸發規則（可依需求調整）：
- CRITICAL：從有變無（>0 → 0，且 old_value >= MIN_QTY_FOR_CRITICAL）
- WARNING ：跌超過 SIG_DROP_PCT（如 50%），且 old_value >= MIN_QTY_FOR_WARNING
- INFO    ：本次 sync 總 SKU 數 vs 上次跌 >= TOTAL_DROP_PCT
"""

import structlog
from datetime import datetime, timezone

from core.database import new_conn, db_write

log = structlog.get_logger()

# 閾值設定 —— 可以之後改成從 Controls 讀
_MIN_QTY_FOR_CRITICAL = 20       # 老值至少要 20 以上才視為 critical（過濾雜訊）
_MIN_QTY_FOR_WARNING  = 50       # 老值至少要 50 才追蹤 warning
_SIG_DROP_PCT         = 0.5      # 跌 50% 以上算 warning
_TOTAL_DROP_PCT       = 0.05     # 總 SKU 數跌 5% 以上警示


async def detect_and_record_anomalies(
    pipeline: str,
    field: str,
    before: dict[str, int],   # {sku: old_value}  或  {(sku, marketplace_id): old_value}
    after:  dict[str, int],   # 同上
    marketplace_id: str = "", # 若整批同屬單一站點可直接傳；否則 key 用 (sku, mp) tuple
) -> dict:
    """
    對比 before / after 兩個 dict，產生 anomalies 並寫入 sync_anomalies 表。
    - key 可為單一 sku（跨站或不分站）或 (sku, marketplace_id) tuple
    - 若整批單一 marketplace → 傳 marketplace_id 參數即可
    回傳統計摘要（供 log / API 用）。
    """
    anomalies: list[tuple] = []
    now = datetime.now(tz=timezone.utc)

    all_keys = set(before.keys()) | set(after.keys())
    critical_ct = warning_ct = info_ct = 0

    def _split_key(k):
        """key 可能是 sku(str) 或 (sku, mp)"""
        if isinstance(k, tuple):
            return k[0], k[1] or marketplace_id
        return k, marketplace_id

    for key in all_keys:
        sku, mp = _split_key(key)
        old_v = int(before.get(key, 0) or 0)
        new_v = int(after.get(key, 0) or 0)
        if old_v == new_v:
            continue

        change_pct = None
        if old_v > 0:
            change_pct = round((new_v - old_v) / old_v * 100, 1)

        severity = None
        reason   = None

        if new_v == 0 and old_v >= _MIN_QTY_FOR_CRITICAL:
            severity = "critical"
            reason = f"{field} 從 {old_v} 掉到 0（疑似 API 漏傳或實際清空）"
            critical_ct += 1
        elif old_v >= _MIN_QTY_FOR_WARNING and change_pct is not None and change_pct <= -_SIG_DROP_PCT * 100:
            severity = "warning"
            reason = f"{field} 從 {old_v} 掉到 {new_v} ({change_pct}%)"
            warning_ct += 1

        if severity:
            anomalies.append((
                pipeline, sku, mp, field, old_v, new_v,
                change_pct, severity, reason, now
            ))

    total_before = sum(1 for v in before.values() if v and v > 0)
    total_after  = sum(1 for v in after.values() if v and v > 0)
    if total_before > 0:
        total_drop = (total_before - total_after) / total_before
        if total_drop >= _TOTAL_DROP_PCT:
            anomalies.append((
                pipeline, "__aggregate__", marketplace_id, field,
                total_before, total_after,
                round(-total_drop * 100, 1),
                "warning",
                f"總 SKU 數（{field}>0）從 {total_before} 掉到 {total_after} ({round(-total_drop*100,1)}%)",
                now
            ))
            info_ct += 1

    if not anomalies:
        return {"pipeline": pipeline, "field": field,
                "critical": 0, "warning": 0, "info": 0, "recorded": 0}

    def _write():
        conn = new_conn()
        try:
            conn.executemany("""
                INSERT INTO sync_anomalies
                    (pipeline, sku, marketplace_id, field, old_value, new_value,
                     change_pct, severity, reason, detected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, anomalies)
            conn.commit()
            return len(anomalies)
        finally:
            conn.close()

    recorded = await db_write(_write)
    log.warning(
        "sync.anomaly_detected",
        pipeline=pipeline, field=field, marketplace_id=marketplace_id,
        critical=critical_ct, warning=warning_ct, info=info_ct,
        recorded=recorded,
    )
    return {"pipeline": pipeline, "field": field,
            "critical": critical_ct, "warning": warning_ct, "info": info_ct,
            "recorded": recorded}


def snapshot_before(pipeline: str, field: str, table: str, where: str = "",
                    include_marketplace: bool = False) -> dict:
    """
    在 sync 開始前，讀取當前資料當快照。
    - include_marketplace=True → key 為 (sku, marketplace_id)，適合 inventory / sales_summary
    - include_marketplace=False → key 為 sku（跨站聚合，適合 AWD）
    """
    conn = new_conn()
    try:
        if include_marketplace:
            sql = f"SELECT sku, marketplace_id, {field} FROM {table}"
            if where:
                sql += f" WHERE {where}"
            rows = conn.execute(sql).fetchall()
            return {(r[0], r[1]): int(r[2] or 0) for r in rows if r[0]}
        else:
            sql = f"SELECT sku, {field} FROM {table}"
            if where:
                sql += f" WHERE {where}"
            rows = conn.execute(sql).fetchall()
            return {r[0]: int(r[1] or 0) for r in rows if r[0]}
    finally:
        conn.close()
