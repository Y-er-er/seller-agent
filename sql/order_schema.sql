-- 本地订单快照。仅保存演示或商家导入的数据，不代表已写入外部平台。
-- 迁移由管理员显式执行；应用启动不自动建表。
CREATE TABLE IF NOT EXISTS merchant_order (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    seller_id VARCHAR(64) NOT NULL,
    source VARCHAR(16) NOT NULL,
    external_order_id VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    paid_cent BIGINT UNSIGNED NOT NULL,
    order_amount_cent BIGINT UNSIGNED NOT NULL,
    placed_at DATETIME NULL,
    content_hash CHAR(64) NOT NULL,
    imported_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_seller_source_order (seller_id, source, external_order_id),
    KEY idx_seller_imported (seller_id, imported_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS merchant_order_import_payload (
    order_id BIGINT UNSIGNED NOT NULL,
    source_rows JSON NOT NULL,
    PRIMARY KEY (order_id),
    CONSTRAINT fk_merchant_payload_order FOREIGN KEY (order_id) REFERENCES merchant_order(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS merchant_order_line (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    order_id BIGINT UNSIGNED NOT NULL,
    line_id VARCHAR(64) NOT NULL,
    goods_no VARCHAR(64) NOT NULL DEFAULT '',
    sku_id VARCHAR(64) NOT NULL DEFAULT '',
    title VARCHAR(255) NOT NULL,
    quantity INT UNSIGNED NOT NULL,
    unit_price_cent BIGINT UNSIGNED NOT NULL,
    paid_cent BIGINT UNSIGNED NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_order_line (order_id, line_id),
    KEY idx_goods_no (goods_no),
    CONSTRAINT fk_merchant_line_order FOREIGN KEY (order_id) REFERENCES merchant_order(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS merchant_order_event (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    order_id BIGINT UNSIGNED NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    detail VARCHAR(255) NOT NULL,
    simulated TINYINT(1) NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_order_event (order_id, id),
    CONSTRAINT fk_merchant_event_order FOREIGN KEY (order_id) REFERENCES merchant_order(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
