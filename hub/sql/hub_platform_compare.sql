-- ============================================================
-- 常用跨平台查詢範本
-- ============================================================

-- 1. 本月各平台總營收
SELECT
    platform,
    market,
    SUM(gross_revenue)  AS gross_revenue,
    SUM(discounts)      AS discounts,
    SUM(net_revenue)    AS net_revenue,
    SUM(units)          AS total_units
FROM hub_daily_sales
WHERE date >= date_trunc('month', current_date)
GROUP BY platform, market
ORDER BY net_revenue DESC;


-- 2. 近 30 天跨平台 SKU 銷量排行
SELECT
    hub_sku,
    collection,
    product_name,
    SUM(CASE WHEN platform = 'amazon' THEN units ELSE 0 END) AS amzn_units,
    SUM(CASE WHEN platform = 'shopify' THEN units ELSE 0 END) AS shop_units,
    SUM(units) AS total_units,
    SUM(net_revenue) AS total_revenue
FROM hub_daily_sales
WHERE date >= current_date - INTERVAL 30 DAY
GROUP BY hub_sku, collection, product_name
ORDER BY total_units DESC;


-- 3. 各平台月度趨勢（近 12 個月）
SELECT
    date_trunc('month', date) AS month,
    platform,
    SUM(net_revenue) AS net_revenue,
    SUM(units)       AS units
FROM hub_daily_sales
WHERE date >= current_date - INTERVAL 365 DAY
GROUP BY date_trunc('month', date), platform
ORDER BY month, platform;


-- 4. Amazon 跨 marketplace 比較（本月）
SELECT
    market,
    SUM(units)       AS units,
    SUM(net_revenue) AS net_revenue
FROM hub_daily_sales
WHERE platform = 'amazon'
  AND date >= date_trunc('month', current_date)
GROUP BY market
ORDER BY net_revenue DESC;
