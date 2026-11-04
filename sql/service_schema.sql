-- 普通咨询与售后工单分开存储；仅本地演示进线，暂不接平台消息。
CREATE TABLE IF NOT EXISTS service_conversation (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    public_id CHAR(32) NOT NULL,
    buyer_token_hash CHAR(64) NOT NULL,
    seller_id VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'open',
    assignee VARCHAR(16) NOT NULL DEFAULT 'manual',
    last_seq INT UNSIGNED NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_public_id (public_id),
    KEY idx_seller_status (seller_id, status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS service_message (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    conversation_id BIGINT UNSIGNED NOT NULL,
    seq INT UNSIGNED NOT NULL,
    role VARCHAR(16) NOT NULL,
    author VARCHAR(64) NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    quick_reply_id BIGINT UNSIGNED NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_conversation_seq (conversation_id, seq),
    CONSTRAINT fk_service_message_conversation FOREIGN KEY (conversation_id) REFERENCES service_conversation(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS quick_reply (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    seller_id VARCHAR(64) NOT NULL,
    title VARCHAR(80) NOT NULL,
    content TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_seller_reply (seller_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
