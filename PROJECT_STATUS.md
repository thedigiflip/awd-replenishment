# MAGEASY eCom Dashboard — 專案狀態與交接文件

> 最後更新：2026-09-25
> 用途：新開 Claude 對話或新工程師接手時，先讀這份文件（架構細節見 `README.md`，部署步驟見 `DEPLOYMENT.md`）

---

## 1. 目前狀態總覽

| 項目 | 狀態 |
|---|---|
| 補貨 Dashboard（US / UK / DE） | ✅ 已上線雲端，團隊可用 |
| 雲端自動排程（n8n） | ✅ 2026-09-25 18:00 第一次自動執行成功 |
| 本機 ↔ 雲端開發流程 | ✅ 建立完成（見第 3 節） |
| Traffic 流量分析 | 🟡 已有雛形分頁，**下一階段開發重點**（見第 6 節） |
| HTTPS + Google 登入（IAP） | ⏳ 等 IT 提供子網域 |
| Gmail 警示信 | ⏳ 等 HTTPS 設好後再啟用 |

---

## 2. 環境與存取

### 雲端（正式環境）
| 項目 | 值 |
|---|---|
| GCP 專案 | `mageasy-dashboard-ecom`（顯示名稱 MAGEASY Dashboard Ecom，組織 immageasy.com） |
| VM | `replenishment-server`，`asia-east1-a`，e2-small，Ubuntu 22.04 |
| VM 使用者 / 程式位置 | `execmarket` / `~/app`（GitHub clone） |
| 資料磁碟 | `/data`（20GB 獨立磁碟）：`sp_api.duckdb`、`sp_api_meta.db`、`n8n/` |
| 啟動指令 | `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build` |
| Dashboard | `http://<VM External IP>:8000/dashboard` |
| n8n | `http://<VM External IP>:5678`（帳號密碼為雲端專用 owner） |
| 存取限制 | 防火牆 `allow-dashboard-office` 只開放特定 IP（換網路要加 IP，或改用 IAP 通道） |

**⚠️ External IP 目前是臨時 IP**，VM 停止再啟動會變。建議改成固定 IP（見第 7 節）。

### 本機（開發環境）
| 項目 | 值 |
|---|---|
| 路徑 | `/Users/amberhsieh/Desktop/Claude Homebase/Amazon/AWD Replenishment n8n python` |
| 容器 | `sp_etl`（:8000）、`sp_n8n`（:5678） |
| DB | `duckdb/sp_api.duckdb` |
| 工具 | gcloud CLI（`~/google-cloud-sdk`）、DuckDB CLI 1.5.5（`~/.duckdb/cli/latest`） |

---

## 3. 日常作業 Runbook

### 資料與程式的流向（最重要的規則）
```
資料：雲端 ── ./scripts/pull-cloud-db.sh ──→ 本機        （只往下拉，絕不上傳覆蓋雲端）
程式：本機 ── git push ──→ GitHub ── git pull ──→ 雲端    （只往上推）
```

### 本機開發
- 只啟動 etl：`docker compose up -d etl`（**不要讓本機 n8n 的 workflow 處於 active**，會跟雲端重複打 Amazon API、吃掉每日報表額度）
- 需要最新真實資料：`./scripts/pull-cloud-db.sh`（避開台灣 18:00–18:30）
- 看 DB：`docker compose stop etl` → `duckdb -readonly duckdb/sp_api.duckdb` → 查完 `.exit` → `docker compose start etl`
  - ⚠️ CLI 是 1.5.5、伺服器是 1.1.3，**一律用 `-readonly`**，避免新版寫入後舊版讀不回來
  - DuckDB UI（`-ui`）在此環境不穩定，改用純文字 CLI 或直接請 Claude 查

### 部署到雲端（每次程式更新）
本機：
```bash
git add <檔案> && git commit -m "..." && git push origin main
```
VM（SSH 或 `gcloud compute ssh execmarket@replenishment-server --zone asia-east1-a`）：
```bash
cd ~/app
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```
只改 `dashboard.html` 時，`git pull` 後重新整理頁面即可（不需 rebuild）。

### 設定值（Controls）
存在 DB 的 `replenishment_controls` 表，**本機與雲端各自獨立**，要在兩邊的 dashboard 各改一次（或本機跑 pull 腳本同步雲端設定）。

---

## 4. 雲端排程（n8n，全部以 UTC 設定）

| Workflow | 台灣時間 | 寫入資料表 |
|---|---|---|
| AWD Replenishment — Daily ETL | 每天 18:00 | `inventory`、`awd_inventory`、`orders`、`order_items`、`sales_summary` |
| AWD Report | 每天 11:00 | `awd_inventory`（權威覆蓋）、`sync_anomalies` |
| Traffic MTD Daily | 每天 17:00 | `sales_traffic` |
| Traffic 月報 | 每月 2 號 11:00 | `sales_traffic` |
| FBA Fees 月報 | 每月 2 號 12:00 | `product_catalog.fba_fee` |
| Traffic Backfill | 手動 | `sales_traffic` |

兩個 Gmail 節點目前**停用**（主 ETL、AWD Report），等 HTTPS 設好後再啟用。

---

## 5. 本期重要決策紀錄（2026-09 多站點上線）

| 決策 | 原因 |
|---|---|
| `inventory` PK 含 `marketplace_id` | 多站點同步互相覆蓋 |
| Sales 依**幣別**分站（USD/GBP/EUR/CAD/MXN） | Amazon 銷售報表是 region 級、沒有 sales-channel 欄位。副作用：CA / MX 銷量不再混進 US |
| AWD Total = Available + Inbound（不扣 Outbound） | Outbound 已從 available 扣除並計入 FBA inbound，再扣會重複 |
| FBA 去重：Anchor-preferred + 過濾 `XX-XXXX-XXXX` alias SKU | Amazon API 回傳重複 alias / 舊版 SKU，造成 2–3 倍虛胖。**舊 SKU 未登記在 Anchor 則不計入**（業務決定：視為出清中的死庫存） |
| Cap 警示標題跟隨 `total_cap_months` 設定 | 原本寫死「5M」與實際設定不符；目前設定 4.0 |
| 刪除 `finance_events` 表 + Ads / Finance / Orders(4h) workflow | Finance 表損壞且無使用端；Ads 未設定憑證 |
| n8n workflow 一律 `settings.timezone = "UTC"` | 容器預設台灣時區會讓 cron 提早 8 小時 |
| `docker-compose.prod.yml` 分離雲端設定 | 本機用 `./duckdb`、雲端用 `/data`，`git pull` 不衝突 |

---

## 6. 架構檢視：繼續擴充 Dashboard（Traffic 等）前的風險與建議

依優先度排列。🔴 = 建議在開發 Traffic 前或同時處理。

### 🔴 A. Traffic 資料目前會把不同幣別加在一起
- `GET /etl/traffic/data` **沒有依站點過濾**，US（USD）+ UK（GBP）+ DE（EUR）的營收會直接相加，Sessions / CVR 也會混站
- `POST /etl/traffic/sync-mtd` 預設只跑 US，但月報會跑全站 → 月趨勢的組成不一致
- **建議**：Traffic API 加 `marketplace_id` 參數（比照補貨頁的站點切換），營收只在同幣別內比較；MTD 也改成 loop 全站
- 引用費（referral fee）寫死 15%，各品類 / 站點實際不同，利潤數字僅供參考

### 🔴 B. 月成長 / 衰退的比較基準
- MTD（當月至昨天）直接跟上個完整月比會永遠「衰退」
- **建議**：成長率用「日均值」或「去年同期 / 上月同期間（1 號到同一天）」比較；圖表標示「本月至今」

### 🔴 C. `dashboard.html` 單一檔案已 3,000 行
- 補貨 + Traffic 全部擠在同一個 HTML，JavaScript 全域變數共用，改 A 容易壞 B
- **建議**：開發 Traffic 時順便拆檔：`static/css/common.css`、`static/js/common.js`（API、表格、格式化）、`static/js/replenishment.js`、`static/js/traffic.js`。每個分頁一個檔案，之後新增功能照同一模式

### 🔴 D. 沒有自動化測試
- 這一期的 bug（FBA 虛胖、UK=DE 銷量、AWD 負值）都是事後靠人工比對 Amazon 報表才發現
- **建議**：把驗證過的真實案例存成測試（例：B0CBBL9NR7 UK = 147、B0DLB1T5R8 UK = 176），每次改計算邏輯前後跑一次。Traffic 開發時同步建立 CVR / 成長率的測試案例

### 🟡 E. 雲端資料沒有自動備份
- `/data` 磁碟沒有排程快照，VM 或磁碟出問題會遺失歷史資料（Traffic 歷史月資料一旦遺失，要吃大量 API 額度回補）
- **建議**：GCP 磁碟快照排程（每天一次、保留 14 天，成本極低），約 10 分鐘設定

### 🟡 F. 正式環境仍以開發模式執行
- `Dockerfile` 使用 `uvicorn --reload`，且 `docker-compose.yml` 把程式碼資料夾掛載進容器
- **建議**：`docker-compose.prod.yml` 覆寫啟動指令移除 `--reload`、不掛載程式碼，讓雲端只跑 build 進 image 的版本，更穩定也省記憶體（e2-small 只有 2GB）

### 🟡 G. 危險操作 API 沒有權限控管
- `/etl/sales/purge`、`/etl/inventory/remap-marketplace`、各種 upload 等，任何能打開 dashboard 的人都能呼叫
- 目前靠防火牆限制 IP；IAP 上線後只有公司帳號能進，但同事之間仍無區分
- **建議**：IAP 上線後，依 IAP 傳入的 email 限制這些端點只有管理者能用

### 🟡 H. DuckDB 單一寫入鎖
- 目前規模（每天數次同步 + 少數人瀏覽）沒問題
- 風險點：Traffic 若加入重度彙總查詢（跨 12 個月 × 600 SKU × 3 站），長查詢可能與同步衝突
- **建議**：Traffic 月度彙總在同步完成時預先算好存成彙總表，dashboard 讀彙總表即可；使用者人數明顯增加再考慮升級 Postgres

### ⚪ I. 其他
- DuckDB 1.1.3 版本偏舊；升級需連同 CLI 一起評估，並先備份
- `.env` credentials 仍存在 VM 檔案中，且曾在對話中出現過 → Secret Manager + 輪替 SP-API tokens
- `sz_warehouse` 仍有舊欄位 `available_qty` / `shippable_qty`（無害，可日後清理）
- 資料庫 migration 目前是啟動時的 ad-hoc 檢查，功能變多後建議加版本表記錄已套用的 migration

---

## 7. 待辦清單

**Traffic 開發前建議先做**
- [ ] Traffic API 依站點過濾 + MTD loop 全站（6-A）
- [ ] 拆分 `dashboard.html`（6-C）
- [ ] 建立測試資料夾與第一批驗證案例（6-D）

**基礎設施**
- [ ] VM External IP 改固定 IP
- [ ] `/data` 磁碟快照排程（6-E）
- [ ] prod 關閉 `--reload`（6-F）
- [ ] 向 IT 取得子網域 → Load Balancer + HTTPS + IAP（約 US$18/月）
- [ ] 啟用 Gmail 警示節點（需 HTTPS）
- [ ] Secret Manager + 輪替 SP-API credentials
- [ ] IAP 上線後刪除 `allow-dashboard-office` 防火牆規則

**清理（不急）**
- [ ] 本機 `duckdb/sp_api.before-drop.duckdb`、`duckdb/backups/`
- [ ] 雲端 `/data/sp_api.before-drop.duckdb`

---

## 8. Traffic 開發時請遵守的圖表偏好（Amber 既有要求）

- 分佈 / 佔比類（by Collection、by SKU）用 **pie chart**；bar / line 只用於時間趨勢
- 趨勢要一眼看懂：Y 軸上限用 P90 × 1.3 避免極端值壓縮；Sessions 與 CVR 分成上下兩個 panel，不要強行雙軸
- CVR 圖加平均值虛線，高於平均綠色、低於平均橘紅色
- 每個資料點直接標數值（data label），不用 hover 才看得到
