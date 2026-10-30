# 每月滯銷庫存報表（Email）

> 上線：2026-10-30（測試期間只寄給 Amber）
> 相關：`PROJECT_STATUS_EXCESS.md`、`PROJECT_STATUS_REPLENISHMENT.md`

## 1. 內容

每月 1 號 09:00（台北）自動寄出上個月的報表：一個 Excel、5 個分頁。

| 分頁 | 內容 |
|---|---|
| 摘要 | 全通路滯銷金額 / 數量、Benchmark 使用率、與上月月底比較、滯銷在哪裡、Collection 前 10、各站補貨重點、資料新鮮度、定義 |
| Replenishment US / UK / DE | 與 dashboard「⬇ 匯出 CSV」相同（全部 ASIN、相同欄位與順序） |
| Excess Inventory | 與 dashboard「⬇ 匯出 CSV」相同欄位；列出**全部** ASIN，依滯銷金額排序（dashboard 匯出的是目前篩選結果） |

- 任何資料來源超過 3 天沒更新 → 信件主旨加 ⚠️、摘要頁與信件最上方列出警告
- 每月 28 號 09:00 寄提醒：月底前上傳最新的深圳倉 template
- 檔案存檔：VM `/data/reports/<YYYY-MM>/MAGEASY_Excess_Report_<YYYY-MM>.xlsx`（同月重跑會覆蓋）

## 2. 架構

| 元件 | 檔案 |
|---|---|
| 欄位定義（唯一來源，dashboard CSV 與月報共用） | `etl/core/report_columns.py` → `GET /reports/columns` |
| 產生 Excel | `etl/services/monthly_report_service.py` |
| API | `POST /reports/monthly?period=YYYY-MM`（預設上個月）、`GET /reports/monthly/{period}/file` |
| 排程 + 寄信 | `n8n/workflow_monthly_report.json`（產生 → 下載 → Gmail） |

上線驗證（2026-10-30，雲端資料副本）：Excel 4 個明細分頁 vs dashboard 匯出 CSV，
121,875 格 0 差異、欄位名稱與順序完全相同；摘要數字 = 明細加總 = API。

## 3. 第一次設定（Gmail 授權，透過 SSH 通道，不需等 HTTPS）

Google 允許 OAuth 回到 `http://localhost`，所以用 SSH 通道把 VM 的 n8n 接到 Mac 的 localhost 來完成授權。

### 3-1 Google Cloud：建立 OAuth 用戶端（一次性）
1. https://console.cloud.google.com → 專案 `mageasy-dashboard-ecom`
2. **APIs & Services → Library** → 搜尋 **Gmail API** → Enable
3. **APIs & Services → OAuth consent screen**
   - User type 選 **Internal**（只有 @mageasy.co 能用；token 不會過期）
   - 若只能選 External：發布狀態要改成 **In production**，否則 Testing 模式的授權 7 天就失效
   - App name：`MAGEASY Dashboard`；support email：`execmarket@mageasy.co`
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**
   - Application type：**Web application**
   - Authorized redirect URIs：貼上 n8n 憑證視窗顯示的 **OAuth Redirect URL**（見 3-3，應為 `http://localhost:5678/rest/oauth2-credential/callback`）
   - 建立後記下 **Client ID** 與 **Client Secret**

### 3-2 Mac：建立 SSH 通道
本機 n8n 若在跑，先停掉（避免占用 5678）：
```
cd "/Users/amberhsieh/Desktop/Claude Homebase/Amazon/AWD Replenishment n8n python"
docker compose stop n8n
gcloud compute ssh execmarket@replenishment-server --project mageasy-dashboard-ecom --zone asia-east1-a -- -L 5678:localhost:5678
```
這個 Terminal 視窗保持開著。瀏覽器打開 **http://localhost:5678** → 這就是 **VM 上的 n8n**。

### 3-3 n8n：建立 Gmail 憑證
1. 左側 **Credentials → Add credential → Gmail OAuth2 API**
2. 複製視窗上的 **OAuth Redirect URL**，貼回 3-1 第 4 步
3. 貼上 Client ID / Client Secret → **Sign in with Google** → 用 `execmarket@mageasy.co` 登入並允許
4. 顯示 *Connected* 即完成。之後關掉 SSH 通道也能繼續寄信（n8n 會自動更新 token）

### 3-4 匯入並測試 workflow（VM）
```
cd ~/app && git pull && docker compose restart etl
docker cp n8n/workflow_monthly_report.json sp_n8n:/tmp/
docker exec -u node sp_n8n n8n import:workflow --input=/tmp/workflow_monthly_report.json
```
n8n 打開 **Monthly Excess Report | Email**：
1. 兩個 Gmail 節點選剛建立的憑證（收件人已預設為 amber.hsieh@mageasy.co，測試期間只寄給 Amber）
2. 點 **Test workflow**（從「1st of Month」觸發）→ 收到信、附件可開啟
3. 確認無誤後切成 **Active**

## 4. 日常
- 加收件人：n8n → 兩個 Gmail 節點的 **To**，多個信箱用逗號分隔（限 @mageasy.co）
- 補寄某個月：n8n 手動執行，或 `curl -X POST "http://localhost:8000/reports/monthly?period=2026-10"` 後從 `/data/reports/2026-10/` 取檔
- 檔案含 COG 成本：只寄公司內部信箱，不用公開分享連結
