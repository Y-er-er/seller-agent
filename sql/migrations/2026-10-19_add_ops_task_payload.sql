-- ============================================================
-- 迁移（2026-10-19）：ops_task 增加 payload（重试所需参数）
-- ------------------------------------------------------------
-- 为什么需要：任务失败后想"重试一次"，但任务的执行体（handler 是函数引用）
-- **不能持久化**，进程重启/请求结束就没了。
-- 所以改为持久化**重建 handler 所需的参数**（payload），重试时按 kind 重新构造。
--
-- ⚠️ 只有**可安全重试**的 kind 才允许重试（当前只有 ai_cover）：
--    批量操作重试会重复执行，风险大于收益。
--
-- 幂等：通过 information_schema 判断列是否存在。
-- ============================================================

SET @col_exists := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'ops_task'
      AND COLUMN_NAME = 'payload'
);

SET @ddl := IF(
    @col_exists = 0,
    'ALTER TABLE ops_task
        ADD COLUMN payload JSON NULL
            COMMENT ''重建执行体所需参数（仅可重试的 kind 会写）''
            AFTER detail',
    'SELECT ''ops_task.payload 已存在，跳过'' AS migration_note'
);

PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
