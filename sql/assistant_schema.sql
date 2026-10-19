-- ============================================================
-- 卖家经营控制台 —— AI 助手 DDL（阶段二）
-- 用途：承载「经营助手」的对话原文与**待确认动作**。
-- 幂等：全部 IF NOT EXISTS，可重复执行。
-- ⚠️ 与售后域的表分开：售后对话在 conversation_message，
--    助手对话在 assistant_message —— 两条对话的语义与生命周期不同，混在一起会污染彼此的审计链。
-- ============================================================

-- ------------------------------------------------------------
-- 1) assistant_message —— 助手对话原文
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assistant_message (
    id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT  COMMENT '自增主键',
    seller_id  VARCHAR(64)     NOT NULL DEFAULT 'default' COMMENT '卖家标识（预留多卖家）',
    thread_id  VARCHAR(64)     NOT NULL                 COMMENT '会话线程（单卖家阶段为 default）',
    seq        INT UNSIGNED    NOT NULL                 COMMENT '线程内序号（从 1 递增；幂等键）',
    role       VARCHAR(16)     NOT NULL                 COMMENT '角色：seller 卖家 / assistant 助手 / tool 工具结果',
    content    TEXT            NULL                     COMMENT '消息正文',
    tool_name  VARCHAR(64)     NULL                     COMMENT '本条涉及的工具名（role=tool 时）',
    tool_args  JSON            NULL                     COMMENT '工具入参（审计与回放用）',
    created_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '写入时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_thread_seq (thread_id, seq)           COMMENT '同线程内序号唯一（天然幂等）',
    KEY idx_thread (thread_id, created_at)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_general_ci
  COMMENT='AI 助手对话原文（与售后对话分表，避免污染彼此审计链）';

-- ------------------------------------------------------------
-- 2) pending_action —— **待确认的破坏性动作**（人在环）
--    为什么必须有它：模型提出的"删除商品 / 批量改价 / 退回工单"等不可逆动作，
--    在卖家点头之前**一律不得执行**。本表就是"停在那里等确认"的落点。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pending_action (
    id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT  COMMENT '自增主键',
    seller_id  VARCHAR(64)     NOT NULL DEFAULT 'default' COMMENT '卖家标识',
    thread_id  VARCHAR(64)     NOT NULL                 COMMENT '所属对话线程',
    tool_name  VARCHAR(64)     NOT NULL                 COMMENT '待执行的工具名',
    arguments  JSON            NOT NULL                 COMMENT '待执行的入参（确认后原样执行）',
    summary    VARCHAR(255)    NOT NULL                 COMMENT '一句话说明（界面主文案）',
    detail     TEXT            NULL                     COMMENT '影响明细（界面展开显示，如"将删除 3 件商品及 5 张图片"）',
    status     VARCHAR(16)     NOT NULL DEFAULT 'pending' COMMENT '状态：pending / confirmed / cancelled / expired',
    result     TEXT            NULL                     COMMENT '确认后执行的结果摘要（回写到对话）',
    created_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '提出时间',
    decided_at DATETIME        NULL                     COMMENT '决定时间',
    PRIMARY KEY (id),
    KEY idx_pending (seller_id, status, created_at)     COMMENT '查待确认列表',
    KEY idx_thread (thread_id)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_general_ci
  COMMENT='AI 提出的破坏性动作；卖家确认后才执行（人在环）';
