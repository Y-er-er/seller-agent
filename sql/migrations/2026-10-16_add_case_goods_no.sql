-- ============================================================
-- 迁移（2026-10-16）：conversation_case 增加 goods_no
-- ------------------------------------------------------------
-- 目的：建立「商品 × 售后」的关联 —— 商品由卖家自建、订单来自外部系统，
--       两者本无关联；没有这一列就算不出"这个商品退货率多少"。
--
-- 幂等：通过 information_schema 判断列是否已存在，可重复执行。
-- 说明：新库由 chat_schema.sql 直接建出该列；本文件只服务**已有库**。
-- ============================================================

SET @col_exists := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'conversation_case'
      AND COLUMN_NAME = 'goods_no'
);

SET @ddl := IF(
    @col_exists = 0,
    'ALTER TABLE conversation_case
        ADD COLUMN goods_no VARCHAR(64) NULL
            COMMENT ''该订单对应的商品编号（商品×售后联动的桥；受理时从订单事实带入）''
            AFTER assignee,
        ADD KEY idx_goods_no (goods_no)',
    'SELECT ''conversation_case.goods_no 已存在，跳过'' AS migration_note'
);

PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
