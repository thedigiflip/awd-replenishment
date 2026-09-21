# MAGEASY Replenishment & Advertising Dashboard

Amazon FBA 補貨與廣告資料整合系統，支援多站點（US / UK / DE）自動同步、庫存分析、返單建議、廣告效益追蹤。

---

## 快速給 Claude 看的 3 分鐘簡介

**這個系統做什麼？** 每天自動從 Amazon SP-API 抓資料（庫存、銷售、訂單、費用、廣告），寫入本地 DuckDB，透過 FastAPI 提供給網頁 Dashboard 顯示。Dashboard 綜合 Anchor 表（產品主檔）、SZ 倉（工廠端庫存）、Amazon FBA/AWD 庫存，計算每個 SKU 的補貨建議（移倉數、返單量、AWD 補貨量）。

**架構：** n8n (排程) → FastAPI (ETL 端點) → DuckDB (儲存) → 靜態 HTML Dashboard  
**執行方式：** `docker compose up -d`，服務跑在 port 8000（etl）、5678（n8n）  
**多站點：** 一套 code 支援 3 站，各站資料獨立，跨站共用商品主檔 & 深圳倉庫存

---

## 系統架構

### 服務容器（`docker-compose.yml`）

| 容器 | Port | 說明 |
|---|---|---|
| `sp_etl` | 8000 | FastAPI 主服務（`etl/main.py`）+ 靜態 Dashboard |
| `sp_n8n` | 5678 | n8n 排程執行 workflow |

### 資料層

**DuckDB**（`/data/sp_api.duckdb`）—— 主要業務資料
- `inventory` — Amazon FBA 庫存快照（`PRIMARY KEY (snapshot_date, asin, sku, marketplace_id)`）
- `awd_inventory` — Amazon Warehouse & Distribution 庫存（**US-only**，no marketplace column）
- `sales_summary` — 每日銷售彙總（`PRIMARY KEY (report_date, sku, marketplace_id)`）
- `orders` / `order_items` — 訂單 & 訂單明細
- `sales_traffic` — Sales & Traffic 月報（Session、Buy Box、CVR）
- `ads_sponsored_products` — Sponsored Products 廣告資料
- `sz_warehouse` — 深圳倉庫存（工廠端，跨站共用）
- `product_catalog` — MAGEASY Anchor 產品主檔（跨站共用，SKU→ASIN 主要對應）
- `sku_config` — 產品類型 & 箱規（可從 Dashboard 編輯）
- `replenishment_controls` — 補貨水位參數（動態調整）
- `sync_anomalies` — 資料異常偵測紀錄

**SQLite**（`/data/sp_api_meta.db`）—— pipeline 執行紀錄（run_id / status / error）

---

## 多站點設計（US / UK / DE）

**設定於 `.env`**：
```
SP_API_MARKETPLACE_IDS=["ATVPDKIKX0DER","A1F83G8C2ARO7P","A1PA6795UKMFR9"]
SP_API_REFRESH_TOKEN=Atzr|...       # NA region (US/CA/MX)
SP_API_REFRESH_TOKEN_EU=Atzr|...    # EU region (UK/DE/FR/IT/ES)
SP_API_REFRESH_TOKEN_FE=            # FE region (JP/AU/SG)  留空
```

Marketplace IDs：
- US = `ATVPDKIKX0DER`（NA region）
- UK = `A1F83G8C2ARO7P`（EU region）
- DE = `A1PA6795UKMFR9`（EU region）

### 資料共用 vs 獨立

| 資料類型 | US | UK | DE | 說明 |
|---|---|---|---|---|
| FBA 庫存 | ✅ 獨立 | ✅ 獨立 | ✅ 獨立 | `inventory.marketplace_id` |
| AWD 庫存 | ✅ 顯示 | 0 | 0 | AWD 只有美國有 |
| Sales | ✅ 獨立 | ✅ 獨立 | ✅ 獨立 | 依 `currency` 分流（USD/GBP/EUR）|
| Orders | ✅ 獨立 | ✅ 獨立 | ✅ 獨立 | `orders.marketplace_id` |
| 深圳倉 / 佳樂倉 | 共用 | 共用 | 共用 | 工廠端庫存 |
| Anchor / SKU 對應 | 共用 | 共用 | 共用 | 一份商品主檔 |
| Fees / Traffic | ✅ 獨立 | ✅ 獨立 | ✅ 獨立 | Router loop 全站 |

### 關鍵設計決策

1. **`inventory` PK 包含 `marketplace_id`**：避免 SP-API loop 三站時互相覆蓋
2. **Sales 報告是 region-level**（Amazon 帳號級）：一次 EU 呼叫回傳所有 EU 站資料 → 依 `currency` 欄分流至 UK / DE
3. **AWD 是 US-only 服務**：Replenishment 查詢用 `WHERE ? = 'ATVPDKIKX0DER'` 守門，非 US 站 AWD 全為 0
4. **Fees / Traffic router 不傳 marketplace_id → loop 全站**；有傳 → 只跑該站（相容單站呼叫）

---

## 核心業務邏輯

### 補貨計算（`services/replenishment_service.py`）

**Key = ASIN**（不是 SKU），因為同一個 ASIN 可能對應多個 SKU（換版）。

```
月銷量 (H) = Sales 30 days
FBA Total (J) = FBA Available + FBA Inbound  
                     ↑ fulfillable + reserved_fc_transfers + reserved_fc_processing
FBA Month (K) = FBA Total ÷ 月銷量

AWD Total (O) = AWD Available + AWD Inbound − AWD Outbound
AWD Month (P) = AWD Total ÷ 月銷量

Total Coverage (Q) = (FBA Total + AWD Total) ÷ 月銷量

美國虛擬庫存 = 美國倉 (us_qty) + 欠數 (pending_qty)      -- 未來可用
美國虛擬 Month = 美國虛擬庫存 ÷ 月銷量

安全庫存門檻 = 月銷量 × 移倉水位（依產品類型）
    - 一般款: sz_transfer_level_normal
    - 流量款: sz_transfer_level_hero  
    - Rocket 🚀: sz_transfer_level_rocket (預設 2.5)

美國倉缺口 = max(0, 安全庫存門檻 − 美國虛擬庫存)

移倉數建議 = min(佳樂倉庫存, 美國倉缺口)
    抑制條件: Total Coverage ≥ 移倉觸發門檻（預設 4.0）→ 移倉 = 0

建議返單數量 = max(0, 美國倉缺口 − 佳樂倉庫存)
    抑制條件: Total Coverage ≥ 返單觸發門檻（預設 4.0）→ 返單 = 0

AWD 補貨量:
    need = awd_target × 月銷量 − AWD Total
    cap  = 3.0 × 月銷量 − (FBA Total + AWD Total)   -- 總上限 3 個月
    awd_replen = min(need, cap)
```

### 三級補貨警示（優先度 🔴 > 🟡 > 🔵）

- 🔴 **需返單**：`reorder_qty > 0`
- 🟡 **佳樂告急**：`transfer_qty > 0 且 佳樂 − transfer < 安全庫存 × 0.5`
- 🔵 **已下單待收**：`pending_qty > 0 且 reorder_qty = 0`

### 產品類型（`sku_config.product_type`）
- `一般款` / `流量款` / `Rocket` / `Discontinued`（停產 → 不建議返單/移倉/AWD補貨）

---

## 主要 API 端點

**Inventory (FBA)**
- `POST /etl/inventory/sync` — 全站 loop 同步
- `POST /etl/inventory/upload?marketplace_id=US` — 手動上傳 Amazon Manage FBA Inventory CSV（也接受 form field）
- `POST /etl/inventory/manual-adjust` — 單 SKU 手動修正
- `POST /etl/inventory/remap-marketplace` — 修正錯站點的 rows
- `GET  /etl/inventory/debug/{asin_or_sku}` — 診斷單一 ASIN/SKU

**AWD** (US only)
- `POST /etl/awd/sync` — API 同步
- `POST /etl/awd/upload` — 手動上傳 AWD Excel
- `POST /etl/awd/report-sync` — 觸發 SP-API AWD Report（權威）+ reconcile

**Sales**
- `POST /etl/sales/sync` — 不傳 marketplace_id → loop 全站 region-level
- `GET  /etl/sales/marketplace-breakdown` — 診斷各站點分布
- `POST /etl/sales/purge?marketplace_id=X&since_days=90` — 清除資料

**Orders / Fees / Traffic / Ads**（都支援 loop 全站）

**Replenishment**
- `GET  /replenishment?marketplace_id=US` — Dashboard 主資料
- `GET  /replenishment/daily-alert` — 每日 email 警示（n8n 呼叫）

**Health**
- `GET  /etl/health/summary?marketplace_id=US` — 各 pipeline 狀態
- `GET  /etl/health/anomalies?marketplace_id=US` — 資料異常清單

**SZ Warehouse**
- `POST /etl/sz/upload?replace=true` — 上傳深圳倉 template
- `GET  /etl/sz/download-template` — 下載空白 template

**Catalog (Anchor)**
- `POST /etl/catalog/upload` — 上傳 MAGEASY Anchor xlsx

---

## Dashboard（`etl/static/dashboard.html`）

單一 HTML file，開在 `http://localhost:8000/dashboard`。

**主要 panel：**
1. **站點切換下拉**（🇺🇸 US / 🇬🇧 UK / 🇩🇪 DE）— 切換時 loadData + loadHealthPanels
2. **上傳工具區** — Anchor / SZ / AWD / FBA CSV
   - FBA 上傳有**獨立紅框站點選擇器**（不跟 filter 綁定，防止上錯站）
   - 每次上傳前 confirm 對話框顯示目標站點
3. **資料串接健康度** — 每個 pipeline 的最新同步、覆蓋率、異常
4. **資料異常** panel — 顯示過去 24h 的 sync_anomalies
5. **補貨主表** — 每 SKU 一列，含返單建議、移倉建議、AWD 補貨、警示等

**Controls（浮動齒輪按鈕）** — 動態調整水位參數，寫入 `replenishment_controls` 表。

---

## n8n Workflows（`n8n/*.json`）

排程與檔案：

| Workflow | Cron | 說明 |
|---|---|---|
| `awd_replenishment_workflow.json` | `0 10 * * *` (10:00 UTC = 台灣 18:00) | 主 ETL：FBA + AWD + Orders + Sales + Alert |
| `workflow_orders.json` | 每 4 小時 | 訂單同步 |
| `workflow_finance.json` | `0 23 * * *` (23:00 UTC) | Finance 同步 |
| `workflow_ads.json` | `0 0 * * *` | Ads 同步 |
| `workflow_fees.json` | `0 4 2 * *` | 每月 2 號 Fees 同步 |
| `workflow_traffic.json` | `0 3 2 * *` | 每月 2 號 Traffic |
| `workflow_traffic_mtd.json` | `0 9 * * *` | 每日 MTD Traffic |
| `workflow_awd_report.json` | `0 3 * * *` | AWD Report reconcile |

**⚠️ 重要**：n8n 端多個 workflow 都不要 hardcode marketplace_id（除非明確要單站）。後端會自動 loop。

**Amazon 24h Rate Limit：** 每個 (report type × marketplace) 每 24 小時只能成功呼叫 1 次。今天 10:00 UTC 成功 → 明天 10:00 UTC 之後才能再跑。

---

## 環境變數（`.env`）

```
# SP-API
SP_API_REFRESH_TOKEN=...              # NA region
SP_API_REFRESH_TOKEN_EU=...           # EU region
SP_API_REFRESH_TOKEN_FE=              # FE region (未用)
SP_API_CLIENT_ID=amzn1.application-oa2-client.<your_client_id>
SP_API_CLIENT_SECRET=amzn1.oa2-cs.v1.<your_client_secret>
SP_API_MARKETPLACE_IDS=["ATVPDKIKX0DER","A1F83G8C2ARO7P","A1PA6795UKMFR9"]

# Ads API
ADS_CLIENT_ID=...
ADS_CLIENT_SECRET=...
ADS_REFRESH_TOKEN=...
ADS_PROFILE_ID=...

# DuckDB
DUCKDB_PATH=/data/sp_api.duckdb

# n8n
N8N_USER=...
N8N_PASSWORD=...
```

**⚠️ `.env` 千萬不能 push 到 GitHub。** 上 GCP 部署改用 Secret Manager。

---

## 常見操作

**啟停：**
```bash
docker compose up -d     # 啟動
docker compose down      # 停（保留 volume 資料）
docker compose restart etl   # 重啟 API（不重載 .env）
docker compose down && docker compose up -d   # 完整重啟（會重載 .env）
```

**看 log：**
```bash
docker compose logs -f etl
docker compose logs --tail 100 etl | grep -i error
```

**DB 查詢（read-only，不撞 FastAPI lock）：**
```bash
docker compose exec etl python3 -c "
import duckdb
c = duckdb.connect('/data/sp_api.duckdb', read_only=True)
# ...
"
```

**Backup DB：** 直接 copy `sp_api.duckdb` 檔（記得先停 API 才能 copy）。

---

## Deployment 到 GCP

**Checklist：**
1. Compute Engine 或 Cloud Run（推薦 CE，因為 DuckDB 需要 persistent volume）
2. `.env` → Google Secret Manager
3. `/data` 掛 Persistent Disk（重要：DuckDB 檔）
4. n8n 也部署一份（可用 Docker or GCP hosted）
5. 開通 outbound firewall 到 `sellingpartnerapi-*.amazon.com`
6. 每次上線後：跑一次 `docker compose exec etl python3 -c "..."` 確認 DB migration 都跑完

---

## 已知重要架構決策紀錄

1. **`inventory` PK 加 marketplace_id**（否則 SP-API loop 會互相覆蓋）
2. **FBA CSV upload 支援 query + form marketplace_id**（防呆）
3. **Sales 依 currency 分 UK/DE**（Amazon Sales Report 沒 sales-channel 欄）
4. **AWD 只在 US 站顯示**（`awd_by_asin` CTE 有 marketplace 守門）
5. **Health / Anomaly 依 marketplace 分**（P1-2, P1-3, P1-4）
6. **Fees / Traffic router loop 全站**（P1-6, P1-7）
7. **Sales sync region-level + currency 分流**（P0-7）

---

## 待辦（P1 尚未做完）

- P1-5：`ads_sponsored_products` 加 marketplace_id 欄（等要跑 UK/DE Ads）
- P1-9：Ads n8n workflow 加 loop（跟 P1-5 綁）

---

## 資料流全景

```
Amazon SP-API ─┐
Amazon Ads API ─┼→ n8n (排程) → FastAPI (/etl/*/sync) → DuckDB
使用者手動上傳 ─┘                                          │
                                                          ▼
                                              FastAPI (/replenishment)
                                                          │
                                                          ▼
                                              Dashboard HTML
                                                          │
                                              (使用者操作 Controls)
                                                          │
                                                          ▼
                                              replenishment_controls 表
                                              → 影響下次計算
```

---

## Repo 結構

```
etl/
  main.py                    # FastAPI app
  core/
    config.py                # Pydantic Settings + region_for / refresh_token_for
    database.py              # DuckDB schema + migrations
    sp_api_client.py         # SP-API 客戶端（region-aware）
    db_meta.py               # SQLite pipeline runs
  routers/
    inventory.py             # FBA endpoints
    sales.py / orders.py / fees.py / traffic.py / ads.py
    awd_upload.py            # AWD Excel 上傳
    sz_warehouse.py          # 深圳倉 template
    catalog.py               # MAGEASY Anchor
    replenishment.py         # Dashboard 主資料
    health.py                # 健康度 / 異常
    controls.py              # 補貨參數
  services/
    inventory_service.py     # FBA sync
    awd_service.py           # AWD sync
    awd_report_service.py    # AWD Report reconcile
    sales_service.py         # Sales region-level sync + currency 分流
    orders_service.py / fees_service.py / traffic_service.py / ads_service.py
    replenishment_service.py # 補貨計算主邏輯（大 CTE 查詢）
    anomaly_detector.py      # 資料異常偵測
    base_service.py          # BaseService（start_run/finish_run/fail_run）
  static/
    dashboard.html           # 單頁 Dashboard

n8n/                         # n8n workflow JSON files
.env                         # 環境變數（不 push GitHub）
docker-compose.yml
```
