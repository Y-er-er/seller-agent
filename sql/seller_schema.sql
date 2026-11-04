-- ============================================================
-- 卖家登录（身份来源）
-- ============================================================
-- 为什么单独两张表、而不是直接给 goods 加账号列：
--   1. **身份与业务数据分离**：业务表只需要 `seller_id`（已有），
--      密码哈希、登录时间这些只跟"人"有关，混进业务表会扩散敏感字段。
--   2. **一个账号对应一个卖家的业务数据**（`seller_id`）：数据隔离早就按
--      `seller_id` 做好了（见 goods_schema.sql），登录只需把"谁"解析成 id。
--   3. **会话单独落库**：退出登录要能**立即失效**（revoked_at），
--      纯签名 Token 做不到"现在就踢下线"。
--
-- ⚠️ 密码**绝不存明文**：`password_hash` 是 `pbkdf2_sha256$迭代次数$盐$摘要`，
--    由 `app/seller/accounts.py` 生成与校验（标准库实现，不引入新依赖）。
-- ============================================================

CREATE TABLE IF NOT EXISTS seller_account (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    seller_id     VARCHAR(64)   NOT NULL                COMMENT '业务数据归属（goods/任务/知识库均按它隔离）',
    username      VARCHAR(64)   NOT NULL                COMMENT '登录账号（全局唯一，大小写不敏感）',
    password_hash VARCHAR(255)  NOT NULL                COMMENT 'PBKDF2-HMAC-SHA256，格式见文件头注释',
    display_name  VARCHAR(64)   NOT NULL DEFAULT ''     COMMENT '界面上显示的名字（左栏「当前卖家」）',
    status        VARCHAR(16)   NOT NULL DEFAULT 'active' COMMENT 'active / disabled',
    created_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at DATETIME      NULL                    COMMENT '最近一次成功登录（失败不更新）',
    UNIQUE KEY uk_username (username),
    KEY idx_seller (seller_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='卖家登录账号';

CREATE TABLE IF NOT EXISTS seller_session (
    token       CHAR(43)    PRIMARY KEY                  COMMENT '不透明随机串（32 字节 urlsafe 去填充）；不承载任何用户信息',
    account_id  INT         NOT NULL                     COMMENT '所属账号',
    seller_id   VARCHAR(64) NOT NULL                     COMMENT '冗余存一份，免每次 join',
    -- **本次登录专属的助手会话线程**：会话一换，线程就换 → 每次登录天然是"新对话"。
    -- 这也是"每次登录默认开启新对话"的实现方式（不依赖前端记状态，刷新也不会串回旧对话）。
    thread_id   VARCHAR(64) NOT NULL                     COMMENT '助手对话线程（每次登录新分配）',
    created_at  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at  DATETIME    NOT NULL                     COMMENT '到期即失效；前端收到 401 会弹登录窗',
    revoked_at  DATETIME    NULL                         COMMENT '退出登录的时间（非空即失效）',
    last_seen_at DATETIME   NULL                         COMMENT '最近一次带此 Token 的请求',
    KEY idx_account (account_id),
    KEY idx_seller (seller_id),
    CONSTRAINT fk_session_account FOREIGN KEY (account_id) REFERENCES seller_account (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='卖家登录会话';
