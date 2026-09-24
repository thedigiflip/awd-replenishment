#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# 把雲端 (GCP VM) 的正式 DuckDB 下載到本機，用來驗證 ETL 計算。
#
# 資料流向永遠是：雲端 → 本機（絕不反向上傳覆蓋雲端）
#
# 用法（在專案根目錄）：
#   ./scripts/pull-cloud-db.sh
#
# 前置：Mac 已安裝 gcloud CLI 並登入（gcloud auth login）
# 注意：避開台灣時間 18:00–18:30（雲端主排程同步中）
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROJECT="mageasy-dashboard-ecom"
ZONE="asia-east1-a"
VM="execmarket@replenishment-server"
REMOTE_APP="/home/execmarket/app"
COMPOSE_PROD="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

cd "$(dirname "$0")/.."   # 回到專案根目錄
LOCAL_DIR="duckdb"
STAMP=$(date +%Y%m%d_%H%M%S)

echo "① 雲端：暫停 etl（約 5 秒）→ 複製 DB 快照到 /tmp → 重啟 etl"
gcloud compute ssh "$VM" --project "$PROJECT" --zone "$ZONE" --command "
  set -e
  cd $REMOTE_APP
  $COMPOSE_PROD stop etl
  cp /data/sp_api.duckdb   /tmp/sp_api.duckdb
  cp /data/sp_api_meta.db  /tmp/sp_api_meta.db
  $COMPOSE_PROD start etl
  ls -lh /tmp/sp_api.duckdb /tmp/sp_api_meta.db
"

echo "② 本機：停止 etl，備份目前的本機 DB"
docker compose stop etl || true
mkdir -p "$LOCAL_DIR/backups"
[ -f "$LOCAL_DIR/sp_api.duckdb" ]  && mv "$LOCAL_DIR/sp_api.duckdb"  "$LOCAL_DIR/backups/sp_api_$STAMP.duckdb"
[ -f "$LOCAL_DIR/sp_api_meta.db" ] && mv "$LOCAL_DIR/sp_api_meta.db" "$LOCAL_DIR/backups/sp_api_meta_$STAMP.db"

echo "③ 下載雲端快照到本機"
gcloud compute scp --project "$PROJECT" --zone "$ZONE" \
  "$VM:/tmp/sp_api.duckdb" "$VM:/tmp/sp_api_meta.db" "$LOCAL_DIR/"

echo "④ 清掉雲端 /tmp 的快照"
gcloud compute ssh "$VM" --project "$PROJECT" --zone "$ZONE" \
  --command "rm -f /tmp/sp_api.duckdb /tmp/sp_api_meta.db"

echo "⑤ 本機只啟動 etl（不啟動 n8n，避免跟雲端重複打 Amazon API）"
docker compose up -d etl

ls -lh "$LOCAL_DIR/sp_api.duckdb"
echo "✅ 完成。本機 DB 已是雲端 $(date '+%Y-%m-%d %H:%M') 的快照"
echo "   舊的本機 DB 備份在 $LOCAL_DIR/backups/（確認沒問題後可自行刪除）"
