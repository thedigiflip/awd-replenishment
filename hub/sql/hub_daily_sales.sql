-- ============================================================
-- hub_daily_sales — 統一每日銷售視圖
-- 將各平台銷售數據展開成一致格式
-- ============================================================

CREATE OR REPLACE VIEW hub_daily_sales AS

-- Amazon（從 amzn_sales_traffic 月報轉換成日均，或直接用 amzn_orders 日數據）
SELECT
    'amazon'                AS platform,
    amzn_marketplace        AS market,
    date_trunc('day', o.purchase_date) AS date,
    m.hub_sku,
    m.collection,
    m.product_name,
    o.sku                   AS platform_sku,
    SUM(o.quantity_ordered) AS units,
    SUM(o.item_price)       AS gross_revenue,
    NULL                    AS discounts,
    SUM(o.item_price)       AS net_revenue
FROM amzn_orders o
LEFT JOIN hub_product_map m
    ON o.sku = m.amzn_sku AND o.marketplace = m.amzn_marketplace
GROUP BY
    o.marketplace,
    date_trunc('day', o.purchase_date),
    m.hub_sku, m.collection, m.product_name, o.sku

UNION ALL

-- Shopify
SELECT
    'shopify'               AS platform,
    'global'                AS market,
    date_trunc('day', o.created_at) AS date,
    m.hub_sku,
    m.collection,
    m.product_name,
    li.sku                  AS platform_sku,
    SUM(li.quantity)        AS units,
    SUM(li.price * li.quantity) AS gross_revenue,
    SUM(li.total_discount)  AS discounts,
    SUM((li.price * li.quantity) - li.total_discount) AS net_revenue
FROM shop_line_items li
JOIN shop_orders o ON li.order_id = o.order_id
LEFT JOIN hub_product_map m ON li.sku = m.shop_sku
WHERE o.financial_status IN ('paid', 'partially_refunded')
GROUP BY
    date_trunc('day', o.created_at),
    m.hub_sku, m.collection, m.product_name, li.sku;
