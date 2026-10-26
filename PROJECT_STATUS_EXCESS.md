# MAGEASY eCom Dashboard — 跨境滯銷水位（Excess Inventory）狀態與交接文件

> 需求來源：市管處（跨境市場部門的滯銷金額水位管理）
> 第一階段上線：2026-10-26
> 相關文件：`ARCHITECTURE.md`（資料層規範）、`PROJECT_STATUS_REPLENISHMENT.md`（補貨）

---

## 1. 定義（市管處版本）

| 指標 | 公式 |
|---|---|
| 庫存池 | FBA Available（US + UK + DE）+ AWD Available + 美國倉 |
| 全通路 30 天銷量 | Amazon US + CA + MX + UK + DE（之後加 Shopify、TikTok Shop） |
| 全通路年銷量 | 全通路 30 天銷量 × 12（`dim_params.excess_annual_months`，可調） |
| **全通路滯銷數量** | max(0, 庫存池 − 全通路年銷量)，**逐 Child ASIN 計算後才加總** |
| **全通路滯銷金額** | 滯銷數量 × Anchor COG（USD） |

- FBA Available = fulfillable + reserved FC transfers + reserved FC processing（與補貨相同）
- 不含：FBA / AWD 在途、工廠欠數、佳樂倉
- **不重複計算**：
  - 美國倉每個 ASIN 只有一個數字（深圳倉 template 沒有站點之分），只加一次
  - FBA 只列舉 US / UK / DE；CA / MX 若日後被同步（北美遠程配送會回報 US 的同一份庫存），也不會被加進來
  - 指標不隨站點切換，全公司一個數字
- 與補貨頁「AMZN滯銷」的差別：AMZN 是**單一站點**（該站 FBA + AWD vs 該站銷量）；全通路是**全公司一個池子**。兩者並列，可看出貨卡在哪裡。

### 滯銷位置（分配規則）
一年內預計賣掉的貨，會先從最靠近客戶的位置消化（FBA → AWD → 美國倉），
所以滯銷依序歸在 **美國倉 → AWD → FBA**：
`美國倉滯銷 = min(滯銷, 美國倉)`；`AWD 滯銷 = min(剩餘, AWD)`；`FBA 滯銷 = 剩餘`

### 可銷月數分級
| 等級 | 條件 |
|---|---|
| 12–18M / 18–24M / 24M+ | 已滯銷，依 庫存池 ÷ 月銷量 |
| 無銷量 | 有庫存、近 30 天 0 銷量 |
| 潛在滯銷 9–12M | 尚未滯銷，提早預警 |
| 正常 / 無庫存 | — |

### 其他欄位
- **銷量趨勢**：近 30 天 vs 前 30 天（第 31–60 天），±20% 為 加速 / 放緩
- **⚠️ 滯銷仍在補貨**：已滯銷，但仍有 工廠欠數 / FBA 在途 / AWD 在途。注意 FBA 在途也包含 AWD→FBA 移倉

---

## 2. 架構（符合 ARCHITECTURE.md）

| 層 | 物件 | 檔案 |
|---|---|---|
| Dimension | `dim_product`（Anchor，一 ASIN 一列）、`dim_params`（Benchmark、年化月數） | `etl/core/warehouse_views.py` |
| Silver | `vw_silver_amazon_fba_by_asin`、`vw_silver_amazon_awd_by_asin`、`vw_silver_amazon_sales_by_asin`、`vw_silver_sz_by_asin` | 同上 |
| Gold | `vw_gold_excess_inventory`（一 Child ASIN 一列，所有計算都在這裡） | 同上 |
| Fact | `fact_excess_inventory_daily`（每日快照，存組成數字） | 同上 |
| Service / API | `/excess-inventory`、`/trend`、`/snapshot`、`/params` | `etl/services/excess_inventory_service.py`、`etl/routers/excess_inventory.py` |
| 頁面 | 獨立 HTML，以 iframe 嵌在 dashboard 分頁「📉 跨境滯銷水位」 | `etl/static/excess_inventory.html` |
| 排程 | 每天 23:00（台灣）寫快照 | `n8n/workflow_excess_snapshot.json` |

- Silver view 的定義與補貨 CTE 完全相同；上線前逐 ASIN 比對 3 站 FBA / AWD / 美國倉 / 銷量 / COG：**0 差異**
- 補貨仍用自己的 CTE（未改讀 Silver），依 ARCHITECTURE.md §7 之後再遷移

### 新增通路（Shopify / TikTok Shop）步驟
1. Bronze：`fact_shopify_orders_daily`（或同等）寫入同一個 DuckDB
2. Silver：`vw_silver_shopify_sales_by_asin`（asin, units_30d, units_prev_30d），產品用 Anchor 對到 Child ASIN
3. Gold：在 `vw_gold_excess_inventory` 加一欄 `sales_shopify_30d`，`all_channel_30d` / `all_channel_prev_30d` 加上它
4. Fact：`fact_excess_inventory_daily` 加對應欄位（migration）
5. 頁面：明細表加一欄通路銷量
6. 驗證：逐 ASIN 快照比對，差異只能來自新通路銷量
7. 注意：加入新通路後，趨勢線會出現「定義改變」的跳動 → 在圖上標註上線日

---

## 3. 第一階段範圍（2026-10-26）

- [x] KPI：全通路滯銷數量、全通路滯銷金額（含 Benchmark 使用率）、滯銷 ASIN 數、滯銷仍在補貨、潛在滯銷
- [x] 趨勢折線圖（月 / 日）+ 市管處 Benchmark 紅色虛線
- [x] Collection 圓餅圖（預設金額，可切數量；前 8 名 + 其他；點擊篩選）
- [x] 滯銷位置（美國倉 / AWD / FBA）
- [x] 可銷月數分級（點擊篩選）
- [x] 明細表：欄位篩選、排序、合計列、CSV 匯出
- [x] Benchmark 設定（`dim_params`，畫面上可改）

### 已知限制
- **趨勢線從 2026-10-26 開始累積**：AWD 與美國倉沒有歷史資料，無法回補過去月份
- 全通路目前只有 Amazon（US / CA / MX / UK / DE）；Shopify、TikTok Shop 尚未串接 → 年銷量偏低、滯銷偏高
- 30 天 × 12 在旺季（12 月）會高估年銷量 → 滯銷偏低（與補貨同一個待辦）
- 新品上架初期銷量低，可能被誤判為滯銷（需要 Anchor 上架日期）

---

## 4. 第二 / 三階段（待辦）

- [ ] 流量與轉換：滯銷品是「沒人看」還是「看了不買」（`sales_traffic`）
- [ ] 廣告投入：滯銷品的廣告花費 / ACOS（`ads_sponsored_products`）
- [ ] FBA 庫齡與預估超齡附加費（FBA Inventory Planning report）
- [ ] 新品上架日期排除
- [ ] 月增減拆解（新進貨 vs 銷售消化）— 需 2 個月以上快照
- [ ] 串接 Shopify、TikTok Shop
