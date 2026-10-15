-- ============================================================
-- 仅退款判定智能体 —— 业务审计表 DDL
-- 用途：decision_audit，记录每笔申请的判定结论与处置动作（方案 §9.1「业务留痕」）
-- 建议：与 LangGraph 检查点表**分库/分 schema**（方案 §9.2 约束 2：权限边界清晰、可独立清理）
-- 字符集：utf8mb4（中文与 emoji 安全）
-- 幂等：使用 IF NOT EXISTS，可重复执行
-- ============================================================

CREATE TABLE IF NOT EXISTS decision_audit (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT  COMMENT '自增主键',
    request_id      VARCHAR(64)     NOT NULL                 COMMENT '受理号（= LangGraph thread_id）',
    order_id        VARCHAR(64)     NOT NULL                 COMMENT '订单号',
    conclusion      VARCHAR(32)     NOT NULL                 COMMENT '判定结论：auto_pass / auto_reject / request_evidence / to_human',
    hit_rules       VARCHAR(255)    NOT NULL DEFAULT ''      COMMENT '命中规则 ID，逗号分隔（全量求值，可能多条，如 R1,R7）',
    rule_version    VARCHAR(32)     NOT NULL DEFAULT ''      COMMENT '规则表版本号，使结论可回溯到具体规则版本',
    field_values    JSON            NULL                     COMMENT '命中字段 / 运算符 / 阈值 / 实际值，逐项可回溯',
    risk_level      VARCHAR(16)     NULL                     COMMENT '风险等级：low / medium / high',
    action          VARCHAR(64)     NULL                     COMMENT '处置动作；影子模式下为「本应执行」的动作',
    executed        TINYINT(1)      NOT NULL DEFAULT 0       COMMENT '本次处置动作是否真正落地执行（影子模式恒为 0；无动作路径亦为 0）',
    shadow          TINYINT(1)      NOT NULL DEFAULT 1       COMMENT '该处置动作是否处于影子模式（无动作路径为 0；判断"钱有没有动"请看 executed）',
    review_decision VARCHAR(32)     NULL                     COMMENT '人工复核决定：maintain / override',
    reviewer        VARCHAR(64)     NULL                     COMMENT '复核人标识',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '写入时间',
    PRIMARY KEY (id),
    KEY idx_request (request_id),
    KEY idx_order (order_id),
    KEY idx_created (created_at)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_general_ci
  COMMENT='仅退款判定审计表（对外审计、客服口径、一致率统计）';
