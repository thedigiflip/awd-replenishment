-- ============================================================
-- hub_product_map — 跨平台 SKU 對應表
-- 這是跨平台聚合的核心，手動維護或透過 UI 匯入
-- ============================================================

CREATE TABLE IF NOT EXISTS hub_product_map (
    hub_sku             VARCHAR NOT NULL,       -- 內部統一品號（跨平台主鍵）
    collection          VARCHAR,                -- 產品系列（如 MagEasy Armor360）
    product_name        VARCHAR,                -- 產品名稱（人類可讀）

    -- Amazon
    amzn_asin           VARCHAR,
    amzn_sku            VARCHAR,
    amzn_marketplace    VARCHAR DEFAULT 'us',   -- us / jp / uk / de / ca

    -- Shopify
    shop_product_id     VARCHAR,
    shop_variant_id     VARCHAR,
    shop_sku            VARCHAR,

    -- 未來平台欄位（建立時預留，需要時取消註解）
    -- tiktok_product_id   VARCHAR,
    -- ebay_item_id        VARCHAR,
    -- walmart_item_id     VARCHAR,

    is_active           BOOLEAN DEFAULT TRUE,
    created_at          TIMESTAMP DEFAULT current_timestamp,
    updated_at          TIMESTAMP DEFAULT current_timestamp,

    PRIMARY KEY (hub_sku, amzn_marketplace)
);

-- 從現有 amzn_product_catalog 初始化（Amazon US 資料）
-- 執行前請確認 amzn_product_catalog 已完成 rename migration
INSERT OR IGNORE INTO hub_product_map (hub_sku, collection, product_name, amzn_asin, amzn_sku, amzn_marketplace)
SELECT
    sku             AS hub_sku,
    collection,
    name            AS product_name,
    asin            AS amzn_asin,
    sku             AS amzn_sku,
    'us'            AS amzn_marketplace
FROM amzn_product_catalog
WHERE marketplace = 'us';
