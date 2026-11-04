"""商品域业务规则（状态机 / 校验 / 级联清理 / 操作留痕）。

分层：`api`（HTTP 形态）→ **`service`（本文件，业务规则）** → `repo`（SQL）。
所有业务判断都在本层，api 只做参数解析与错误转换。

三条约定：

1. **写操作一律留痕**（`operation_log`）——人工与 AI 同等留痕。
   AI 代操作的前提是"每一步可查、可撤"，所以日志不是配套设施，是前置条件。
2. **删除不可撤销**（`undoable=False`）：删除会连图片一起消失，撤销无从回滚。
   将来若加回收站（软删除），改的是本层，不动 repo。
3. **级联清理顺序：先删数据库行、再删对象存储文件**。
   反过来会产生"商品还在、图却没了"的错乱状态 —— 孤儿文件只是浪费空间，不破坏一致性。

状态机：`draft`（草稿）→ `on_shelf`（已上架）⇄ `off_shelf`（已下架）。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.db import goods_repo as repo
from app.ports import get_storage

logger = logging.getLogger(__name__)

#: 允许上传的图片扩展名（其余一律拒绝，不做"猜测纠正"）
ALLOWED_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")

VALID_STATUSES = (repo.STATUS_DRAFT, repo.STATUS_ON_SHELF, repo.STATUS_OFF_SHELF)

STATUS_LABELS = {
    repo.STATUS_DRAFT: "草稿",
    repo.STATUS_ON_SHELF: "已上架",
    repo.STATUS_OFF_SHELF: "已下架",
}

#: 可更新的字段白名单（与 repo 的 allowed 保持一致，此处再做一层业务校验）
_UPDATABLE = (
    "title", "description", "price", "cost_price", "stock", "stock_alert",
    "category", "status", "pinned",
)


class GoodsError(Exception):
    """商品域业务错误。API 层据 `status_code` 转 HTTP 状态码。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


# ============================================================
# 校验
# ============================================================


def _require_text(value: Any, name: str, *, max_len: int = 200, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise GoodsError("invalid_field", f"{name}不能为空")
    if len(text) > max_len:
        raise GoodsError("invalid_field", f"{name}过长（上限 {max_len} 字）")
    return text


def _require_number(value: Any, name: str, *, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise GoodsError("invalid_field", f"{name}必须是数字") from None
    if number < minimum:
        raise GoodsError("invalid_field", f"{name}不能小于 {minimum}")
    return round(number, 2)


def _require_int(value: Any, name: str, *, minimum: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise GoodsError("invalid_field", f"{name}必须是整数") from None
    if number < minimum:
        raise GoodsError("invalid_field", f"{name}不能小于 {minimum}")
    return number


def _check_status(status: str) -> str:
    if status not in VALID_STATUSES:
        raise GoodsError("invalid_field", f"状态非法：{status}（可选 {', '.join(VALID_STATUSES)}）")
    return status


def _check_can_shelve(title: str, price: float) -> None:
    """上架前的必要条件。

    为什么要求价格 > 0：陈列是对外展示，标价 0 的商品上线属于明显事故。
    图片不强制，但会在概览里提示"缺主图"（避免把上架门槛设得过高而卡住流程）。
    """
    if not title:
        raise GoodsError("invalid_field", "上架前必须填写商品标题")
    if price <= 0:
        raise GoodsError("invalid_field", "上架前必须设置大于 0 的价格")


# ============================================================
# 创建 / 更新 / 删除
# ============================================================


async def create_goods(
    seller_id: str,
    *,
    title: Any,
    price: Any = 0,
    description: Any = "",
    cost_price: Any = 0,
    stock: Any = 0,
    stock_alert: Any = 0,
    category: Any = "",
    status: str = repo.STATUS_DRAFT,
    actor: str = repo.ACTOR_HUMAN,
) -> dict[str, Any]:
    """新建商品（默认草稿）。编号与陈列顺序自动分配。"""
    clean_title = _require_text(title, "商品标题", required=True)
    clean_price = _require_number(price, "售价")
    clean_cost = _require_number(cost_price, "成本价")
    clean_stock = _require_int(stock, "库存")
    clean_alert = _require_int(stock_alert, "预警阈值")
    clean_status = _check_status(status)
    if clean_status == repo.STATUS_ON_SHELF:
        _check_can_shelve(clean_title, clean_price)

    goods_no = await repo.next_goods_no(seller_id)
    sort_no = await repo.max_sort_no(seller_id) + 1
    try:
        goods_id = await repo.create_goods(
            seller_id=seller_id,
            goods_no=goods_no,
            title=clean_title,
            description=_require_text(description, "商品文案", max_len=5000),
            price=clean_price,
            cost_price=clean_cost,
            stock=clean_stock,
            stock_alert=clean_alert,
            category=_require_text(category, "类目", max_len=64),
            status=clean_status,
            sort_no=sort_no,
        )
    except Exception as exc:  # noqa: BLE001 — 唯一键冲突等，转成明确的业务错误
        if "Duplicate" in str(exc) or "duplicate" in str(exc):
            raise GoodsError("duplicate_no", f"商品编号 {goods_no} 已存在，请重试", 409) from exc
        raise

    await repo.insert_operation(
        actor=actor,
        action="create_goods",
        target_id=goods_no,
        summary=f"新建商品「{clean_title}」（{STATUS_LABELS[clean_status]}）",
        after={"goods_no": goods_no, "title": clean_title, "price": clean_price},
        undoable=False,
        seller_id=seller_id,
    )
    goods = await repo.get_goods(goods_id, seller_id)
    return goods or {}


async def update_goods(
    seller_id: str, goods_id: int, *, actor: str = repo.ACTOR_HUMAN, **fields: Any
) -> dict[str, Any]:
    """更新商品字段。自动记录变更前后快照（供撤销）。"""
    before = await repo.get_goods(goods_id, seller_id)
    if before is None:
        raise GoodsError("goods_not_found", f"商品不存在：{goods_id}", 404)

    clean: dict[str, Any] = {}
    if "title" in fields:
        clean["title"] = _require_text(fields["title"], "商品标题", required=True)
    if "description" in fields:
        clean["description"] = _require_text(fields["description"], "商品文案", max_len=5000)
    if "category" in fields:
        clean["category"] = _require_text(fields["category"], "类目", max_len=64)
    if "price" in fields:
        clean["price"] = _require_number(fields["price"], "售价")
    if "cost_price" in fields:
        clean["cost_price"] = _require_number(fields["cost_price"], "成本价")
    if "stock" in fields:
        clean["stock"] = _require_int(fields["stock"], "库存")
    if "stock_alert" in fields:
        clean["stock_alert"] = _require_int(fields["stock_alert"], "预警阈值")
    if "pinned" in fields:
        clean["pinned"] = 1 if fields["pinned"] else 0
    if "status" in fields:
        clean["status"] = _check_status(str(fields["status"]))

    unknown = set(fields) - set(_UPDATABLE)
    if unknown:
        raise GoodsError("invalid_field", f"不支持修改的字段：{', '.join(sorted(unknown))}")

    if not clean:
        return before

    # 目标状态为"上架"时，用**更新后**的值做校验
    if clean.get("status") == repo.STATUS_ON_SHELF:
        _check_can_shelve(
            clean.get("title", before.get("title") or ""),
            float(clean.get("price", before.get("price") or 0)),
        )

    await repo.update_goods(goods_id, seller_id, **clean)

    changes = {k: v for k, v in clean.items() if before.get(k) != v}
    if changes:
        await _log_changes(seller_id, actor, before, changes)
    return await repo.get_goods(goods_id, seller_id) or {}


async def _log_changes(
    seller_id: str, actor: str, before: dict[str, Any], changes: dict[str, Any]
) -> None:
    """按变更内容写操作日志（含可读摘要与撤销快照）。"""
    labels = {
        "title": "标题", "description": "文案", "price": "价格", "cost_price": "成本价",
        "stock": "库存", "stock_alert": "预警阈值", "category": "类目",
        "status": "状态", "pinned": "置顶",
    }
    parts = []
    for key, new_value in changes.items():
        old_value = before.get(key)
        if key == "status":
            old_value, new_value = STATUS_LABELS.get(old_value, old_value), STATUS_LABELS.get(new_value, new_value)
            action = "set_status"
        elif key == "pinned":
            old_value, new_value = ("是" if old_value else "否"), ("是" if new_value else "否")
            action = "set_pinned"
        elif key == "price":
            action = "update_price"
        else:
            action = f"update_{key}"
        parts.append(f"{labels.get(key, key)} {old_value} → {new_value}")

    await repo.insert_operation(
        actor=actor,
        action=("set_status" if "status" in changes else ("update_price" if "price" in changes else "update_goods")),
        target_id=str(before.get("goods_no") or before.get("id")),
        summary="；".join(parts)[:255],
        before={k: before.get(k) for k in changes},
        after=dict(changes),
        undoable=True,   # 字段级修改都可回滚（删除除外）
        seller_id=seller_id,
    )


async def delete_goods(
    seller_id: str, goods_id: int, *, actor: str = repo.ACTOR_HUMAN
) -> dict[str, Any]:
    """删除商品 → **移入回收站**（软删除）。

    为什么改成软删：AI 可以代卖家执行删除，而删除是**不可逆**的。
    移入回收站后，"AI 误删"从"数据没了"变成"还能捞回来"（默认保留 7 天）。

    ⚠️ **只打 `deleted_at`、不动 `status`** —— 恢复时原状态（已上架/草稿）自动回来，
       不需要另存一份快照。
    ⚠️ 图片**保留**（恢复要用）；只有彻底删除才清理文件。
    """
    goods = await repo.get_goods(goods_id, seller_id)
    if goods is None:
        raise GoodsError("goods_not_found", f"商品不存在：{goods_id}", 404)

    media_count = len(await repo.list_media_keys(goods_id))
    if not await repo.soft_delete(goods_id, seller_id):
        raise GoodsError("goods_not_found", f"商品不存在或已在回收站：{goods_id}", 404)

    days = settings.goods_trash_days
    await repo.insert_operation(
        actor=actor,
        action="delete_goods",
        target_id=str(goods.get("goods_no") or goods_id),
        summary=(f"删除商品「{goods.get('title')}」（¥{goods.get('price')}）→ 移入回收站"
                 + (f"，{days} 天内可恢复" if days > 0 else "，可恢复")),
        before={"title": goods.get("title"), "price": goods.get("price"), "media": media_count},
        undoable=False,   # 撤销已由"从回收站恢复"取代
        seller_id=seller_id,
    )
    return {
        "goods_id": goods_id,
        "goods_no": goods.get("goods_no"),
        "trashed": True,
        "media_removed": 0,        # 兼容旧字段：软删不再删图片
        "media_kept": media_count,
        "restore_days": days,
    }


# ============================================================
# 回收站
# ============================================================


async def list_trash(seller_id: str) -> list[dict[str, Any]]:
    """回收站列表（附「剩余可恢复天数」）。"""
    days = settings.goods_trash_days
    rows = await repo.list_trash(seller_id)
    out: list[dict[str, Any]] = []
    for goods in rows:
        since = goods.pop("days_since", None)
        left = max(0, days - int(since)) if (days > 0 and since is not None) else None
        goods["days_left"] = left
        out.append(goods)
    return out


async def restore_goods(
    seller_id: str, goods_id: int, *, actor: str = repo.ACTOR_HUMAN
) -> dict[str, Any]:
    """从回收站恢复（原状态一并回来）。"""
    goods = await repo.get_goods(goods_id, seller_id, include_deleted=True)
    if goods is None or not goods.get("deleted_at"):
        raise GoodsError("goods_not_found", "回收站里没有该商品", 404)
    if not await repo.restore(goods_id, seller_id):
        raise GoodsError("restore_failed", "恢复失败：该商品可能已被彻底删除", 409)

    await repo.insert_operation(
        actor=actor,
        action="restore_goods",
        target_id=str(goods.get("goods_no") or goods_id),
        summary=f"从回收站恢复商品「{goods.get('title')}」",
        undoable=False,
        seller_id=seller_id,
    )
    return {"goods_id": goods_id, "goods_no": goods.get("goods_no"), "status": goods.get("status")}


async def purge_goods(
    seller_id: str, goods_id: int, *, actor: str = repo.ACTOR_HUMAN
) -> dict[str, Any]:
    """**彻底删除**（不可恢复）：商品行 + media 行 + 对象存储文件。"""
    goods = await repo.get_goods(goods_id, seller_id, include_deleted=True)
    if goods is None or not goods.get("deleted_at"):
        raise GoodsError("goods_not_found", "回收站里没有该商品（彻底删除只对回收站内的商品生效）", 404)

    keys = await repo.list_media_keys(goods_id)
    await repo.delete_media_by_goods(goods_id)
    if not await repo.purge_hard(goods_id, seller_id):
        raise GoodsError("purge_failed", "彻底删除失败，请重试", 409)
    removed = await _purge_files(keys)

    await repo.insert_operation(
        actor=actor,
        action="purge_goods",
        target_id=str(goods.get("goods_no") or goods_id),
        summary=f"彻底删除商品「{goods.get('title')}」及 {len(keys)} 张图片（不可恢复）",
        undoable=False,
        seller_id=seller_id,
    )
    return {"goods_id": goods_id, "files_removed": removed, "media_removed": len(keys)}


async def cleanup_expired_trash() -> int:
    """清理回收站里**超过保留期**的商品（启动时 + 打开回收站时调用）。

    到期即**彻底删除**：商品行 + media 行 + 对象存储文件，并记一条 `system` 日志。
    这是"限时保留"的兜底 —— 没有它，回收站会无限增长。
    """
    days = settings.goods_trash_days
    if days <= 0:
        return 0
    try:
        expired = await repo.list_expired(days)
    except Exception as exc:  # noqa: BLE001 —— 清理是旁路，失败不阻断
        logger.warning("查询过期回收站商品失败（忽略）：%s", exc)
        return 0

    cleaned = 0
    for goods in expired:
        seller_id = str(goods.get("seller_id") or "")
        goods_id = int(goods["id"])
        keys = await repo.list_media_keys(goods_id)
        try:
            await repo.delete_media_by_goods(goods_id)
            if not await repo.purge_hard(goods_id, seller_id):
                continue
            await _purge_files(keys)
            await repo.insert_operation(
                actor=repo.ACTOR_SYSTEM,
                action="purge_goods",
                target_id=str(goods.get("goods_no") or goods_id),
                summary=f"回收站到期，彻底删除商品「{goods.get('title')}」及 {len(keys)} 张图片",
                undoable=False,
                seller_id=seller_id,
            )
            cleaned += 1
        except Exception as exc:  # noqa: BLE001 —— 单个失败不影响其余
            logger.warning("清理过期商品 %s 失败：%s", goods_id, exc)

    if cleaned:
        logger.warning("回收站到期清理：彻底删除 %d 个商品", cleaned)
    return cleaned


async def _purge_files(keys: list[str]) -> int:
    """尽力删除对象存储文件。失败只记日志 —— 库里的行已删，孤儿文件不破坏一致性。"""
    if not keys:
        return 0
    storage = get_storage()
    removed = 0
    for key in keys:
        try:
            if await storage.delete(key):
                removed += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("删除对象存储文件失败（key=%s）：%s: %s", key, type(exc).__name__, exc)
    return removed


# ============================================================
# 状态流转 / 编排 / 批量
# ============================================================


async def set_status(
    seller_id: str, goods_id: int, status: str, *, actor: str = repo.ACTOR_HUMAN
) -> dict[str, Any]:
    """上架 / 下架 / 回草稿。"""
    return await update_goods(seller_id, goods_id, actor=actor, status=_check_status(status))


async def reorder(
    seller_id: str, ordered_ids: list[int], *, actor: str = repo.ACTOR_HUMAN
) -> dict[str, Any]:
    """陈列编排：按给定顺序落 `sort_no`。"""
    ids: list[int] = []
    for raw in ordered_ids or []:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            raise GoodsError("invalid_field", f"非法商品 id：{raw!r}") from None
    if not ids:
        raise GoodsError("invalid_field", "排序列表不能为空")

    updated = await repo.reorder_goods(seller_id, ids)
    await repo.insert_operation(
        actor=actor,
        action="reorder",
        target_id="gallery",
        summary=f"调整陈列顺序（{updated} 件商品）",
        undoable=False,   # 排序可再排一次，值不高，暂不提供撤销
        seller_id=seller_id,
    )
    return {"updated": updated}


BULK_ACTIONS = ("on_shelf", "off_shelf", "delete", "price_delta")


async def bulk_action(
    seller_id: str,
    goods_ids: list[int],
    action: str,
    *,
    price_delta: float = 0.0,
    actor: str = repo.ACTOR_HUMAN,
) -> dict[str, Any]:
    """批量动作：上架 / 下架 / 删除 / 调价。

    ⚠️ 批量动作由 **API 层**要求调用方先确认（界面二次确认 / AI 的待确认卡），
    本层只管执行与留痕。删除类会逐件走 `delete_goods`（含文件清理）。
    """
    ids = [int(i) for i in (goods_ids or [])]
    if not ids:
        raise GoodsError("invalid_field", "未选择任何商品")
    if action not in BULK_ACTIONS:
        raise GoodsError("invalid_field", f"不支持的批量动作：{action}")

    done, failed = [], []
    for goods_id in ids:
        try:
            if action == "delete":
                await delete_goods(seller_id, goods_id, actor=actor)
            elif action == "price_delta":
                goods = await repo.get_goods(goods_id, seller_id)
                if goods is None:
                    raise GoodsError("goods_not_found", f"商品不存在：{goods_id}", 404)
                new_price = max(0.0, round(float(goods["price"]) + float(price_delta), 2))
                await update_goods(seller_id, goods_id, actor=actor, price=new_price)
            else:
                await set_status(seller_id, goods_id, action, actor=actor)
            done.append(goods_id)
        except GoodsError as exc:
            failed.append({"goods_id": goods_id, "error": exc.message})
        except Exception as exc:  # noqa: BLE001
            failed.append({"goods_id": goods_id, "error": f"{type(exc).__name__}: {exc}"})

    verb = {"on_shelf": "批量上架", "off_shelf": "批量下架", "delete": "批量删除",
            "price_delta": f"批量调价（{price_delta:+.2f} 元）"}[action]
    await repo.insert_operation(
        actor=actor,
        action=f"bulk_{action}",
        target_id="bulk",
        summary=f"{verb}：成功 {len(done)} 件" + (f"，失败 {len(failed)} 件" if failed else ""),
        undoable=False,
        seller_id=seller_id,
    )
    return {"done": done, "failed": failed}


# ============================================================
# 撤销
# ============================================================


async def undo_operation(
    seller_id: str, log_id: int, *, actor: str = repo.ACTOR_HUMAN
) -> dict[str, Any]:
    """按操作日志的 `before` 快照回滚（仅字段级修改可撤销）。"""
    log = await repo.get_operation(log_id, seller_id)
    if log is None:
        raise GoodsError("log_not_found", f"操作记录不存在：{log_id}", 404)
    if log.get("undone_at"):
        raise GoodsError("already_undone", "该操作已被撤销过", 409)
    if not log.get("undoable"):
        raise GoodsError("not_undoable", "该操作不可撤销（删除类操作不提供撤销）", 409)

    before = log.get("before_json") or {}
    target_no = str(log.get("target_id") or "")
    if not before or not target_no:
        raise GoodsError("not_undoable", "缺少回滚所需的前置快照", 409)

    goods = await _find_by_no(seller_id, target_no)
    if goods is None:
        raise GoodsError("goods_not_found", f"商品已不存在（{target_no}），无法回滚", 409)

    # 回滚时把 status 一并还原为可展示状态（避免回滚后处于非法中间态）
    payload = dict(before)
    await repo.update_goods(goods["id"], seller_id, **payload)

    if not await repo.mark_operation_undone(log_id, seller_id):
        raise GoodsError("already_undone", "该操作已被撤销过", 409)

    await repo.insert_operation(
        actor=actor,
        action="undo",
        target_id=target_no,
        summary=f"撤销「{log.get('summary')}」",
        undoable=False,
        seller_id=seller_id,
    )
    return {"log_id": log_id, "target_id": target_no, "restored": payload}


async def _find_by_no(seller_id: str, goods_no: str) -> dict[str, Any] | None:
    """按编号找商品（操作日志记的是编号，不是自增 id）。"""
    return await find_by_no(seller_id, goods_no)


async def find_by_no(
    seller_id: str, goods_no: str, *, include_deleted: bool = False
) -> dict[str, Any] | None:
    """按商品编号查商品**详情（含图片列表）**；不存在返回 `None`。

    公开此函数是因为**助手工具层**需要按编号定位商品（模型只认编号，不认自增 id）。

    统一返回详情：无论走 id 还是编号命中，最终都取一次 `get_goods` ——
    否则"返回的行不含 media"会让调用方（如确认卡的影响预览）拿到不完整数据。

    `include_deleted=True` 时**只在回收站里找**（用于"恢复误删"）。
    """
    key = str(goods_no or "").strip()
    if not key:
        return None

    if include_deleted:
        for row in await repo.list_trash(seller_id, limit=200):
            if str(row.get("goods_no")) == key:
                return await repo.get_goods(int(row["id"]), seller_id, include_deleted=True)
        return None

    if key.isdigit():
        found = await repo.get_goods(int(key), seller_id)
        if found is not None:
            return found
    rows = await repo.list_goods(seller_id, scope="all", q=key, limit=200)
    for row in rows:
        if str(row.get("goods_no")) == key:
            return await repo.get_goods(row["id"], seller_id)
    return None


# ============================================================
# 概览
# ============================================================


async def overview(seller_id: str) -> dict[str, Any]:
    """概览看板数据：商品计数 / 低库存 / 待处理售后 / 最近操作。

    「待处理售后」由调用方（api 层）从售后域补齐 —— 本层不跨域查 chat 表，
    避免商品域反向依赖售后域。
    """
    counts = await repo.count_by_status(seller_id)
    low_stock = await repo.list_low_stock(seller_id, limit=10)
    operations = await repo.list_operations(seller_id, limit=8)
    total = sum(counts.values())
    return {
        "goods": {
            "total": total,
            "on_shelf": counts.get(repo.STATUS_ON_SHELF, 0),
            "draft": counts.get(repo.STATUS_DRAFT, 0),
            "off_shelf": counts.get(repo.STATUS_OFF_SHELF, 0),
        },
        "low_stock": low_stock,
        "low_stock_count": len(low_stock),
        "recent_operations": operations,
    }


# ============================================================
# 图片
# ============================================================


def _build_object_key(seller_id: str, goods_id: int, suffix: str) -> str:
    """服务端生成对象 key：`<日期>/<卖家>/goods_<id>/<随机名><扩展名>`。

    ⚠️ **绝不使用用户提供的文件名** —— 它可能含路径分隔符、控制字符或超长内容；
    统一由服务端生成，从根上杜绝路径穿越与相互覆盖。
    """
    today = datetime.now().strftime("%Y%m%d")
    return f"{today}/{seller_id}/goods_{goods_id}/{uuid.uuid4().hex}{suffix}"


async def attach_media(
    seller_id: str,
    goods_id: int,
    *,
    filename: str,
    data: bytes,
    content_type: str = "",
    actor: str = repo.ACTOR_HUMAN,
) -> dict[str, Any]:
    """上传并登记一张商品图片；**首张自动设为主图**。

    写失败一律抛错 —— 与取数读通道相反，图片是用户提交的业务数据，不能静默丢弃。
    """
    goods = await repo.get_goods(goods_id, seller_id)
    if goods is None:
        raise GoodsError("goods_not_found", f"商品不存在：{goods_id}", 404)

    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_IMAGE_SUFFIXES:
        raise GoodsError(
            "invalid_file",
            f"不支持的图片类型：{suffix or '(无扩展名)'}；仅支持 {'/'.join(ALLOWED_IMAGE_SUFFIXES)}",
        )
    if not data:
        raise GoodsError("invalid_file", "文件内容为空")
    max_bytes = int(settings.upload_max_bytes)
    if len(data) > max_bytes:
        raise GoodsError(
            "file_too_large", f"图片超过上限 {max_bytes // 1024 // 1024}MB", 413
        )

    key = _build_object_key(seller_id, goods_id, suffix)
    storage = get_storage()
    await storage.put(key, data, content_type or "")
    url = storage.url_for(key)

    existing = goods.get("media") or []
    media_id = await repo.add_media(
        goods_id, url, key, "cover", len(existing), seller_id=seller_id
    )
    if not existing:
        await repo.update_goods(goods_id, seller_id, cover_url=url)

    await repo.insert_operation(
        actor=actor,
        action="upload_media",
        target_id=str(goods.get("goods_no") or goods_id),
        summary=f"上传商品图片（第 {len(existing) + 1} 张）",
        after={"url": url, "kind": "cover"},
        undoable=False,
        seller_id=seller_id,
    )
    return {"media_id": media_id, "url": url, "object_key": key}


async def detach_media(
    seller_id: str, goods_id: int, media_id: int, *, actor: str = repo.ACTOR_HUMAN
) -> dict[str, Any]:
    """删除一张图片（先删行、再删文件），主图被删时顺延到下一张。"""
    goods = await repo.get_goods(goods_id, seller_id)
    if goods is None:
        raise GoodsError("goods_not_found", f"商品不存在：{goods_id}", 404)

    row = await repo.delete_media(media_id, seller_id)
    if row is None:
        raise GoodsError("media_not_found", f"图片不存在：{media_id}", 404)
    if row.get("object_key"):
        await _purge_files([str(row["object_key"])])

    remaining = (await repo.get_goods(goods_id, seller_id) or {}).get("media") or []
    await repo.update_goods(
        goods_id, seller_id, cover_url=(remaining[0]["url"] if remaining else None)
    )

    await repo.insert_operation(
        actor=actor,
        action="delete_media",
        target_id=str(goods.get("goods_no") or goods_id),
        summary=f"删除商品图片（剩 {len(remaining)} 张）",
        undoable=False,
        seller_id=seller_id,
    )
    return {"goods_id": goods_id, "media_id": media_id, "remaining": len(remaining)}
