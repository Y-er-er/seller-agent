-- 用户确认后手动执行；应用不会自动变更 MySQL 结构。
-- 旧订单只有 paid_cent，回填为旧订单金额以保持原有展示语义。
ALTER TABLE merchant_order
    ADD COLUMN order_amount_cent BIGINT UNSIGNED NULL AFTER paid_cent;
UPDATE merchant_order SET order_amount_cent = paid_cent WHERE order_amount_cent IS NULL;
ALTER TABLE merchant_order
    MODIFY COLUMN order_amount_cent BIGINT UNSIGNED NOT NULL;

CREATE TABLE IF NOT EXISTS merchant_order_import_payload (
    order_id BIGINT UNSIGNED NOT NULL,
    source_rows JSON NOT NULL,
    PRIMARY KEY (order_id),
    CONSTRAINT fk_merchant_payload_order FOREIGN KEY (order_id) REFERENCES merchant_order(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
