-- ============================================================
-- 迁移（2026-10-19）：goods 增加 deleted_at（软删除 / 回收站）
-- ------------------------------------------------------------
-- 语义变更：删除从"物理删除"改为"软删除（回收站）"。
--   · deleted_at IS NULL      → 正常商品
--   · deleted_at IS NOT NULL  → 在回收站（status 同时置 'deleted'）
--   · 超过保留期（默认 7 天）→ 彻底删除（商品行 + 图片 + media 行）
--
-- 为什么：AI 可以代卖家执行删除，而删除是**不可逆**的。
-- 有了回收站，"AI 误删"从"数据没了"变成"捞回来"。
--
-- 幂等：通过 information_schema 判断列是否已存在，可重复执行。
-- ============================================================

SET @col_exists := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'goods'
      AND COLUMN_NAME = 'deleted_at'
);

SET @ddl := IF(
    @col_exists = 0,
    'ALTER TABLE goods
        ADD COLUMN deleted_at DATETIME NULL
            COMMENT ''软删除时间（NULL = 正常；非 NULL = 在回收站）''
            AFTER status,
        ADD KEY idx_deleted_at (deleted_at)',
    'SELECT ''goods.deleted_at 已存在，跳过'' AS migration_note'
);

PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
