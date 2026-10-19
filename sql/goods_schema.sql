-- ============================================================
-- 卖家经营控制台 —— 商品域 DDL（阶段一）
-- 用途：承载「商品陈列 / 商品管理」的主数据、图片与操作日志。
-- 与售后域的关系：本文件是**商品域**的表，与对话层（chat_schema.sql）互不耦合。
-- 幂等：全部 IF NOT EXISTS，可重复执行。
-- 字符集：utf8mb4（中文安全）。
-- ⚠️ 所有表带 seller_id：为将来「多卖家登录」预留，本轮固定 'default'。
--    将来接登录时，只需把 seller_id 的来源从常量改为会话解析，表结构无需改动。
-- ============================================================

-- ------------------------------------------------------------
-- 1) goods —— 商品主表
--    关键：「商品陈列」与「商品管理」**共用这一张表** ——
--         陈列 = status='on_shelf' 的视图；管理 = 全量视图。
--         两者不是两套数据，因此"管理里删除 → 陈列同步消失"是天然成立的。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS goods (
    id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT  COMMENT '自增主键',
    seller_id   VARCHAR(64)   NOT NULL DEFAULT 'default' COMMENT '卖家标识（预留多卖家；本轮固定 default）',
    goods_no    VARCHAR(64)   NOT NULL                   COMMENT '商品编号（卖家可见，如 G-0012）',
    title       VARCHAR(200)  NOT NULL                   COMMENT '商品标题',
    description TEXT          NULL                       COMMENT '商品文案',
    price       DECIMAL(10,2) NOT NULL DEFAULT 0.00      COMMENT '售价（元）',
    cost_price  DECIMAL(10,2) NOT NULL DEFAULT 0.00      COMMENT '成本价（算毛利；不对外展示）',
    stock       INT           NOT NULL DEFAULT 0         COMMENT '库存',
    stock_alert INT           NOT NULL DEFAULT 0         COMMENT '低库存预警阈值；0 = 不预警',
    category    VARCHAR(64)   NULL                       COMMENT '类目',
    status      VARCHAR(16)   NOT NULL DEFAULT 'draft'   COMMENT '状态：draft 草稿 / on_shelf 已上架 / off_shelf 已下架',
    cover_url   VARCHAR(512)  NULL                       COMMENT '宣传图（主图）URL —— 由存储适配器给出，不在此拼路径',
    sort_no     INT           NOT NULL DEFAULT 0         COMMENT '陈列排序（越小越前）',
    pinned      TINYINT(1)    NOT NULL DEFAULT 0         COMMENT '陈列置顶：0 / 1',
    created_at  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_goods (seller_id, goods_no)            COMMENT '同卖家下编号唯一',
    KEY idx_shelf (seller_id, status, pinned, sort_no)   COMMENT '陈列视图查询',
    KEY idx_low_stock (seller_id, stock, stock_alert)    COMMENT '低库存预警查询'
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_general_ci
  COMMENT='商品主表（陈列与管理共用同一张；两模块只是不同视图）';

-- ------------------------------------------------------------
-- 2) goods_media —— 商品图片（宣传图可多张，首张为主图）
--    ⚠️ url 存的是**存储适配器给出的完整 URL**（本地 /api/seller/uploads/... 或 COS 域名），
--       不存相对路径 —— 换存储后端时不必回写历史数据。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS goods_media (
    id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT  COMMENT '自增主键',
    seller_id  VARCHAR(64)     NOT NULL DEFAULT 'default' COMMENT '卖家标识（冗余存一份，便于隔离与统计）',
    goods_id   BIGINT UNSIGNED NOT NULL                 COMMENT '所属商品',
    url        VARCHAR(512)    NOT NULL                 COMMENT '图片 URL（由存储适配器给出）',
    object_key VARCHAR(512)    NULL                     COMMENT '对象存储 key（用于删除时定位文件）',
    kind       VARCHAR(16)     NOT NULL DEFAULT 'cover' COMMENT '用途：cover 宣传图 / detail 详情图',
    sort_no    INT             NOT NULL DEFAULT 0       COMMENT '排序（0 为主图）',
    created_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '上传时间',
    PRIMARY KEY (id),
    KEY idx_goods (goods_id, sort_no),
    KEY idx_seller (seller_id)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_general_ci
  COMMENT='商品图片；商品删除时由 service 层级联清理（含对象存储文件）';

-- ------------------------------------------------------------
-- 3) operation_log —— 操作日志（人工与 AI 的动作都记）
--    为什么商品域一开始就要它：AI 代操作的前提是"每一步可查、可撤"。
--    before/after 存 JSON，撤销时据此回滚（只用于**可逆动作**）。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS operation_log (
    id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT  COMMENT '自增主键',
    seller_id   VARCHAR(64)   NOT NULL DEFAULT 'default' COMMENT '卖家标识',
    actor       VARCHAR(16)   NOT NULL                   COMMENT '操作者：human 卖家 / ai 助手 / system 系统（如预警）',
    action      VARCHAR(48)   NOT NULL                   COMMENT '动作码：create_goods / update_price / set_status / delete_goods / reorder / bulk_update_price …',
    target_type VARCHAR(24)   NOT NULL                   COMMENT '对象类型：goods / case',
    target_id   VARCHAR(64)   NOT NULL                   COMMENT '对象标识（goods_no 或 case_id）',
    summary     VARCHAR(255)  NOT NULL                   COMMENT '人可读的一句话（界面直接展示）',
    before_json JSON          NULL                       COMMENT '变更前快照（撤销依据）',
    after_json  JSON          NULL                       COMMENT '变更后快照',
    undoable    TINYINT(1)    NOT NULL DEFAULT 0         COMMENT '是否可撤销（删除类一律 0）',
    undone_at   DATETIME      NULL                       COMMENT '被撤销的时刻；NULL = 未撤销',
    created_at  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '发生时间',
    PRIMARY KEY (id),
    KEY idx_target (seller_id, target_type, target_id, created_at) COMMENT '按对象查历史',
    KEY idx_actor (seller_id, actor, created_at)                   COMMENT '按来源筛选（仅人工 / 仅 AI）'
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_general_ci
  COMMENT='操作日志：谁在什么时候改了什么；AI 的动作与人工同等留痕';
