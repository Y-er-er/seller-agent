-- ============================================================
-- 2026-10-20  pending_action 增加「批次号」
--
-- 为什么需要它：
--   卖家一句话里常有多件事（例如「生成宣传图、文案」），助手会提出**多个**待确认动作。
--   没有批次号时，界面只能一张一张列出来，卖家点掉其中一张就以为全做完了 ——
--   剩下的卡还挂着，看起来像"又弹出来了"（实测踩到：任务中心没有生图任务，商品也没图）。
--
--   加上 batch_id 之后：同一次请求里的多个动作共享一个值，界面**收成一张卡**，
--   点一次全部执行（见 app/assistant/actions.py::confirm_batch）。
--
-- 幂等说明：
--   MySQL 8.0 不支持 `ADD COLUMN IF NOT EXISTS`，本文件请配合
--   `sql/apply_schema.py` 或后端启动时的自动补列（assistant_repo._ensure_batch_column）使用；
--   重复执行会报 "Duplicate column name"，属预期，不是失败。
-- ============================================================

ALTER TABLE pending_action
    ADD COLUMN batch_id VARCHAR(40) NULL
    COMMENT '批次：同一次请求里的多个待确认动作共享一个值' AFTER status;

ALTER TABLE pending_action
    ADD KEY idx_batch (batch_id);
