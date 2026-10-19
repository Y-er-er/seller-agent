-- ============================================================
-- 电商售后智能体 —— 对话层 DDL（P1.1）
-- 用途：承载「买家侧对话」的会话原文、模型抽取槽位、护栏触发与模型用量。
-- 与判定层的关系：
--   · 判定与业务审计在 decision_audit（见 decision_audit.sql）；
--   · 本文件是**对话层**的表，属"过程留痕"，与判定结论分开存放。
-- 幂等：全部使用 IF NOT EXISTS，可重复执行。
-- 字符集：utf8mb4（中文与 emoji 安全）。
-- ============================================================

-- ------------------------------------------------------------
-- 1) conversation_case —— 会话（工单）主体：状态机与轮次计数
--    说明：消息/槽位/护栏是"过程明细"，本表是"会话当前态"，
--          跨进程重启后据此恢复（P1 实施设计 §九 数据模型原缺此表，P1.1 补齐）。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversation_case (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT  COMMENT '自增主键',
    case_id          VARCHAR(64)     NOT NULL                 COMMENT '会话 = 工单号（= 受理号 = 判定图 thread_id）',
    order_id         VARCHAR(64)     NOT NULL                 COMMENT '绑定的订单号（会话绑定这笔单，用户不必重复描述）',
    status           VARCHAR(24)     NOT NULL DEFAULT 'CREATED' COMMENT '状态机：CREATED / CLARIFYING / COLLECTING / SUBMITTED / DECIDED / ESCALATED / CLOSED',
    round            INT UNSIGNED    NOT NULL DEFAULT 0       COMMENT '已消耗轮数（一个往返 = 一轮）',
    last_seq         INT UNSIGNED    NOT NULL DEFAULT 0       COMMENT '已写入的最大消息序号（续写时 +1）',
    escalated_reason VARCHAR(32)     NULL                     COMMENT '转人工原因码：round_limit / msg_too_long / cost_paused / rule_to_human / tool_failed / buyer_request / session_timeout',
    workflow_status  VARCHAR(24)     NOT NULL DEFAULT 'queued' COMMENT '工单状态（指挥台）：queued / agent_running / awaiting_confirm / resolved / escalated',
    assignee         VARCHAR(16)     NULL                     COMMENT '处理者：agent（智能体）/ manual（管理员）',
    goods_no         VARCHAR(64)     NULL                     COMMENT '该订单对应的商品编号（「商品 × 售后联动」的桥；受理时从订单事实带入，取不到留空）',
    decision_result  VARCHAR(24)     NULL                     COMMENT '判定处置结果：refund / reship / reject / manual / pending_evidence',
    decision_amount  DECIMAL(12,2)   NULL                     COMMENT '判定金额（退款金额等，元）',
    confirmed_by     VARCHAR(64)     NULL                     COMMENT '确认人（管理员）',
    confirmed_at     DATETIME        NULL                     COMMENT '确认时间',
    timeout_at       DATETIME        NULL                     COMMENT '静默超时时刻（到期自动收尾或转人工）',
    created_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_case (case_id)                             COMMENT '一个会话号唯一 —— 重复创建会被拒绝（入口幂等）',
    KEY idx_order (order_id),
    KEY idx_goods_no (goods_no),
    KEY idx_status (status),
    KEY idx_timeout (timeout_at)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_general_ci
  COMMENT='对话会话主体（状态机 + 轮次计数；跨进程恢复依据）';

-- ------------------------------------------------------------
-- 2) conversation_message —— 会话原文（卷宗「对话历史」的数据源）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversation_message (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT  COMMENT '自增主键',
    case_id         VARCHAR(64)     NOT NULL                 COMMENT '会话 = 工单号（= 受理号 = 判定图 thread_id）',
    seq             INT UNSIGNED    NOT NULL                 COMMENT '会话内序号，从 1 递增；决定展示顺序，也用于幂等',
    role            VARCHAR(16)     NOT NULL                 COMMENT '角色：buyer（买家）/ agent（智能体）/ human（人工客服，管理员接管后发言）/ system（系统提示）',
    content         TEXT            NULL                     COMMENT '消息正文',
    attachments     JSON            NULL                     COMMENT '附件引用列表（对象存储 key / 本地路径）',
    tool_calls      JSON            NULL                     COMMENT '本轮工具调用摘要：工具名 / 入参摘要 / 结果摘要 / 耗时',
    guardrail_event VARCHAR(32)     NULL                     COMMENT '本条消息触发的护栏类型（如 msg_too_long）',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '写入时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_case_seq (case_id, seq)                    COMMENT '同一会话内序号唯一 —— 重复提交同 seq 会被拒绝（天然幂等）',
    KEY idx_case (case_id),
    KEY idx_created (created_at)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_general_ci
  COMMENT='对话消息（会话原文；保留 90 天后转冷存储或清理）';

-- ------------------------------------------------------------
-- 3) case_slot —— 模型抽取的结构化槽位（**推断**，不参与判定）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS case_slot (
    id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT  COMMENT '自增主键',
    case_id      VARCHAR(64)     NOT NULL                 COMMENT '会话 = 工单号',
    slot         VARCHAR(48)     NOT NULL                 COMMENT '槽位名：issue_reason（为什么）/ issue_object（问题在哪）/ claim_direction（想要什么）/ symptom / affected_qty / signed_days / evidence / note',
    value        VARCHAR(512)    NULL                     COMMENT '抽取到的值 —— 属**推断**，绝不进入规则事实字典',
    confidence   DECIMAL(4,3)    NULL                     COMMENT '模型自报置信度 0.000~1.000',
    source_seq   INT UNSIGNED    NULL                     COMMENT '来源轮次（卷宗需能点回买家原话的那一轮）',
    extracted_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '抽取时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_case_slot (case_id, slot)               COMMENT '同一会话同一槽位只保留最新值',
    KEY idx_case (case_id)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_general_ci
  COMMENT='模型抽取槽位（推断；只读展示，不提供人工编辑入口）';

-- ------------------------------------------------------------
-- 4) guardrail_event —— 护栏触发记录（也写入卷宗「审计与用量」区）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS guardrail_event (
    id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT  COMMENT '自增主键',
    case_id    VARCHAR(64)     NULL                     COMMENT '会话 = 工单号；非会话级事件（如全局成本暂停）可为空',
    type       VARCHAR(32)     NOT NULL                 COMMENT '类型：round_limit / msg_too_long / cost_warning / cost_cap / tool_limit / slot_missing / session_timeout / escalated',
    detail     VARCHAR(512)    NULL                     COMMENT '触发细节：第几次、当前值与阈值、原因码',
    created_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '触发时间',
    PRIMARY KEY (id),
    KEY idx_case (case_id),
    KEY idx_type (type),
    KEY idx_created (created_at)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_general_ci
  COMMENT='护栏触发记录（轮数 / 字数 / 成本 / 工具次数 / 追问次数 / 超时）';

-- ------------------------------------------------------------
-- 5) llm_usage —— 模型用量与**等效消耗**（成本闸门的记账依据）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS llm_usage (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT  COMMENT '自增主键',
    case_id       VARCHAR(64)     NULL                     COMMENT '会话 = 工单号；成本实测等无会话场景可为空',
    seq           INT UNSIGNED    NULL                     COMMENT '会话内轮次（第几次模型调用）',
    model         VARCHAR(64)     NOT NULL                 COMMENT '模型名（来自 .env，不硬编码）',
    input_tokens  INT UNSIGNED    NOT NULL DEFAULT 0       COMMENT '输入 token 数',
    output_tokens INT UNSIGNED    NOT NULL DEFAULT 0       COMMENT '输出 token 数',
    amount        DECIMAL(12,6)   NOT NULL DEFAULT 0       COMMENT '**等效消耗**金额（元）= token × 单价（免费额度期内实际扣费为 0）',
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '调用时间',
    PRIMARY KEY (id),
    KEY idx_case (case_id),
    KEY idx_created (created_at)                           COMMENT '月度累计 = 按当月 created_at 求和（不另建汇总表，避免两份真相）'
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_general_ci
  COMMENT='模型用量与等效消耗（成本闸门记账；单价来自 .env）';
