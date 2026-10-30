"""
匯出欄位定義 —— 唯一來源（Single source of truth）

以下三處都讀這裡，欄位順序與名稱永遠一致：
  1. Replenishment 分頁「⬇ 匯出 CSV」（dashboard.html → GET /reports/columns）
  2. Excess Inventory 分頁「⬇ 匯出 CSV」（excess_inventory.html → GET /reports/columns）
  3. 每月 Excel 報表（services/monthly_report_service.py）

調整 dashboard 表格欄位順序時，請同步更新這裡（前面是畫面上的欄位，後面是只在匯出出現的診斷欄位）。
"""

# ── Replenishment（每站一份）───────────────────────────────────────────────────
# Cap 警示的標題依 Controls → total_cap_months 動態顯示，例如 "4M Cap 警示"
CAP_HEADER_TEMPLATE = "{cap}M Cap 警示"

REPLENISHMENT_COLUMNS: list[tuple[str, str]] = [
    ('collection',             'Collection'),
    ('parent_asin',            'Parent ASIN'),
    ('asin',                   'ASIN'),
    ('sku',                    'SKU'),
    ('product_name',           'Name'),
    ('product_type',           '産品類型'),
    ('awd_target_months',      'AWD目標月'),
    ('sales_qty',              'Sales Qty (M)'),
    ('fba_available',          'FBA Available (現貨)'),
    ('fba_inbound',            'FBA Inbound (在途)'),
    ('fba_total',              'FBA Total (現貨+在途)'),
    ('fba_month',              'FBA Total Month'),
    ('awd_available',          'AWD Available'),
    ('awd_inbound',            'AWD Inbound'),
    ('awd_outbound',           'AWD Outbound'),
    ('awd_total',              'AWD Total (Avail + Inbound)'),
    ('awd_month',              'AWD Month'),
    ('total_coverage',         'Total Coverage FBA+AWD(M)'),
    ('cap_alert',              'Cap 警示'),
    ('excess_qty',             'AMZN滯銷數量'),
    ('excess_value',           'AMZN滯銷金額 (USD)'),
    ('safety_stock',           '安全庫存 門檻'),
    ('jl_qty',                 '佳樂倉'),
    ('us_qty',                 '美國倉'),
    ('pending_qty',            '欠數 (已下單)'),
    ('order_date',             '下單日期'),
    ('expected_date',          '約定交期'),
    ('factory_confirmed_date', '工廠回覆交期'),
    ('unit_per_case',          'Unit/Case'),
    ('us_month',               '美國倉 Month'),
    ('us_virtual_month',       '美國虛擬 Month'),
    ('sz_total_month',         '深圳倉庫總量 Month'),
    ('transfer_qty',           '移倉數建議 (佳樂→美國)'),
    ('reorder_alert',          '返單警示'),
    ('reorder_qty',            '建議返單 數量'),
    ('case_reorder_qty',       '整箱返單 數量'),
    ('suggested_order_date',   '建議 下單日'),
    ('days_until_late',        '交期倒數 (天)'),
    ('air_alert',              '🚨 空運警示'),
    ('immediate_coverage',     'Immediate Coverage'),
    ('awd_replen_qty',         'AWD 需補貨量'),
    ('total_pipeline',         'Total Pipeline (M)'),
    # ── 以下為匯出才有的診斷欄位 ──
    ('delivery_advance_days', '提前/延後天數'),
    ('overdue_days',          '逾期天數'),
    ('awd_replen_case_qty',   'AWD 需補量 (整箱)'),
    ('awd_replen_case_cnt',   'AWD 需補箱數'),
    ('stock_alert',           '📦 庫存警示'),
    ('sz_reorder_level',      'SZ 返單水位保留數'),
    ('sz_available_qty',      'SZ 可出量'),
    ('sea_shipment_qty',      '建議海運量'),
    ('days_can_last',         '現有可撐天數'),
    ('lead_time_days',        '生產交期 (天)'),
    ('case_reorder_count',    '返單箱數'),
]

# ── Excess Inventory（全通路，一份）────────────────────────────────────────────
# 布林欄位（仍在補貨）匯出為 "Y" / 空白
EXCESS_COLUMNS: list[tuple[str, str]] = [
    ('parent_asin',          'Parent ASIN'),
    ('asin',                 'Child ASIN'),
    ('sku',                  'SKU'),
    ('collection',           'Collection'),
    ('product_name',         'Name'),
    ('product_type',         '產品類型'),
    ('amazon_30d',           'Amazon 銷量 (30天)'),
    ('annual_sales',         '全通路年銷量'),
    ('sales_trend_pct',      '銷量趨勢 % (vs 前30天)'),
    ('fba_available',        'FBA Available'),
    ('awd_available',        'AWD Available'),
    ('us_qty',               '美國倉'),
    ('stock_total',          '庫存池合計'),
    ('months_of_supply',     '可銷月數'),
    ('supply_tier',          '等級'),
    ('cog',                  'COG (USD)'),
    ('excess_qty',           '全通路滯銷數量'),
    ('excess_value',         '全通路滯銷金額 (USD)'),
    ('excess_in_us_wh',      '滯銷位置-美國倉'),
    ('excess_in_awd',        '滯銷位置-AWD'),
    ('excess_in_fba_us',     '滯銷位置-FBA US'),
    ('excess_in_fba_uk',     '滯銷位置-FBA UK'),
    ('excess_in_fba_de',     '滯銷位置-FBA DE'),
    ('still_replenishing',   '仍在補貨'),
    ('pending_qty',          '欠數'),
    ('fba_inbound',          'FBA 在途'),
    ('awd_inbound',          'AWD 在途'),
    ('sales_us_30d',         '銷量 US (30天)'),
    ('sales_ca_30d',         '銷量 CA (30天)'),
    ('sales_mx_30d',         '銷量 MX (30天)'),
    ('sales_uk_30d',         '銷量 UK (30天)'),
    ('sales_de_30d',         '銷量 DE (30天)'),
    ('all_channel_prev_30d', '全通路銷量 (前30天)'),
    ('fba_us',               'FBA US'),
    ('fba_uk',               'FBA UK'),
    ('fba_de',               'FBA DE'),
]


def cap_label(total_cap_months: float) -> str:
    """與 dashboard 相同：整數顯示 4，非整數顯示 4.5"""
    m = float(total_cap_months)
    txt = str(int(m)) if m.is_integer() else f"{m:.1f}"
    return CAP_HEADER_TEMPLATE.format(cap=txt)


def replenishment_headers(total_cap_months: float) -> list[tuple[str, str]]:
    """回傳 (key, header)；Cap 欄標題套用目前的上限月數"""
    return [(k, cap_label(total_cap_months) if k == "cap_alert" else h)
            for k, h in REPLENISHMENT_COLUMNS]
