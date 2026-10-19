-- ============================================================
-- 卖家经营控制台 —— 任务中心 DDL
-- 用途：承载**长任务**（AI 生图 30—60 秒）与**批量操作的失败明细**。
-- 为什么需要它：
--   1. AI 生图是异步长任务，原来同步等 60 秒，**关掉浮窗就看不见进度**；
--   2. 批量操作"部分失败"是常态，原来失败明细只在一次返回值里闪过，事后无法追。
-- 幂等：IF NOT EXISTS，可重复执行。
-- ============================================================

CREATE TABLE IF NOT EXISTS ops_task (
    id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    seller_id   VARCHAR(64)     NOT NULL DEFAULT 'default' COMMENT '卖家标识（预留多卖家）',
    kind        VARCHAR(32)     NOT NULL COMMENT '任务类型：ai_cover / ai_copy / ai_price / bulk_op',
    title       VARCHAR(255)    NOT NULL COMMENT '人话标题，如「为「休闲凉拖鞋」生成宣传图」',
    target      VARCHAR(64)     NULL     COMMENT '目标对象（商品编号 / 工单号）',
    status      VARCHAR(16)     NOT NULL DEFAULT 'queued'
                COMMENT 'queued 排队中 / running 执行中 / done 成功 / failed 失败 / interrupted 被中断（服务重启）',
    progress    TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '进度 0—100',
    stage       VARCHAR(64)     NULL     COMMENT '当前阶段（人话），如「生成中」「下载转存」',
    result      TEXT            NULL     COMMENT '结果摘要（成功时）',
    detail      JSON            NULL     COMMENT '结构化明细：失败项列表 / 生成物地址 / 耗时',
    error       VARCHAR(500)    NULL     COMMENT '失败原因（失败时）',
    action_id   BIGINT UNSIGNED NULL     COMMENT '触发的待确认动作 id（可追溯到哪次确认）',
    created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    started_at  DATETIME        NULL     COMMENT '开始执行时间',
    finished_at DATETIME        NULL     COMMENT '结束时间',
    PRIMARY KEY (id),
    KEY idx_task_list (seller_id, created_at),
    KEY idx_task_status (status)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_general_ci
  COMMENT='长任务与批量操作的结果记录（AI 代操作的「事后可追溯」载体）';
