"""商品域 service 测试 —— **假仓储 + 假存储，零外部依赖**。

为什么用假仓储而不是真 MySQL：业务规则的验证不该依赖数据库可用性，
也不该在开发者的库里留下测试数据。仓储层由真机冒烟（HTTP 全链路）覆盖。

覆盖：
  G1  创建：默认草稿 / 编号与排序自动分配
  G2  校验：空标题 / 负价格 / 负库存 / 未知字段 / 非法状态
  G3  上架闸门：未定价不可上架；已定价可通过
  G4  留痕：改价产生带前后快照的日志
  G5  撤销：按快照回滚 / 不可重复撤销 / 删除类不可撤销
  G6  批量：调价 / 删除 / 非法动作
  G7  删除：级联清理图片行与对象存储文件；删除记为不可撤销
  G8  图片：扩展名白名单 / 超限拒绝 / 首张设为主图 / 删除后主图顺延
  G9  概览：结构完整
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.goods import service  # noqa: E402
from app.goods.service import GoodsError  # noqa: E402

S = "test-seller"
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


# ============================================================
# 假仓储
# ============================================================


class FakeRepo:
    ACTOR_HUMAN = "human"
    ACTOR_AI = "ai"
    STATUS_DRAFT = "draft"
    STATUS_ON_SHELF = "on_shelf"
    STATUS_OFF_SHELF = "off_shelf"

    def __init__(self) -> None:
        self.goods: dict[int, dict] = {}
        self.media: dict[int, list[dict]] = {}
        self.logs: list[dict] = []
        self._seq = 0
        self._log_seq = 0

    # ---- 商品 ----
    async def next_goods_no(self, seller_id):
        return f"G-{len(self.goods) + 1:04d}"

    async def max_sort_no(self, seller_id):
        return max((g["sort_no"] for g in self.goods.values()), default=0)

    async def create_goods(self, **f):
        self._seq += 1
        gid = self._seq
        self.goods[gid] = {
            "id": gid, "seller_id": f["seller_id"], "goods_no": f["goods_no"],
            "title": f["title"], "description": f["description"], "price": f["price"],
            "cost_price": f["cost_price"], "stock": f["stock"], "stock_alert": f["stock_alert"],
            "category": f["category"], "status": f["status"], "cover_url": None,
            "sort_no": f["sort_no"], "pinned": False,
        }
        self.media[gid] = []
        return gid

    async def get_goods(self, goods_id, seller_id):
        g = self.goods.get(goods_id)
        if g is None or g["seller_id"] != seller_id:
            return None
        out = dict(g)
        out["media"] = [dict(m) for m in self.media.get(goods_id, [])]
        return out

    async def update_goods(self, goods_id, seller_id, **fields):
        g = self.goods.get(goods_id)
        if g is None:
            return False
        g.update(fields)
        return True

    async def delete_goods(self, goods_id, seller_id):
        return self.goods.pop(goods_id, None) is not None

    async def list_goods(self, seller_id, *, scope="all", q="", status="", low_stock_only=False, limit=200):
        rows = [g for g in self.goods.values() if g["seller_id"] == seller_id]
        if scope == "shelf":
            rows = [g for g in rows if g["status"] == self.STATUS_ON_SHELF]
        if status:
            rows = [g for g in rows if g["status"] == status]
        if low_stock_only:
            rows = [g for g in rows if g["stock_alert"] > 0 and g["stock"] <= g["stock_alert"]]
        return [dict(g) for g in rows][:limit]

    async def reorder_goods(self, seller_id, ordered_ids):
        for i, gid in enumerate(ordered_ids):
            if gid in self.goods:
                self.goods[gid]["sort_no"] = i
        return len(ordered_ids)

    async def count_by_status(self, seller_id):
        out: dict[str, int] = {}
        for g in self.goods.values():
            if g["seller_id"] == seller_id:
                out[g["status"]] = out.get(g["status"], 0) + 1
        return out

    async def list_low_stock(self, seller_id, limit=20):
        return [dict(g) for g in self.goods.values()
                if g["seller_id"] == seller_id and g["stock_alert"] > 0
                and g["stock"] <= g["stock_alert"]][:limit]

    # ---- 图片 ----
    async def add_media(self, goods_id, url, object_key="", kind="cover", sort_no=0, seller_id=S):
        mid = len(self.media.setdefault(goods_id, [])) + 1
        self.media[goods_id].append(
            {"id": mid, "goods_id": goods_id, "url": url, "object_key": object_key,
             "kind": kind, "sort_no": sort_no}
        )
        return mid

    async def delete_media(self, media_id, seller_id):
        for gid, items in self.media.items():
            for i, m in enumerate(items):
                if m["id"] == media_id:
                    items.pop(i)
                    return {"id": media_id, "goods_id": gid, "object_key": m["object_key"]}
        return None

    async def delete_media_by_goods(self, goods_id):
        n = len(self.media.get(goods_id, []))
        self.media[goods_id] = []
        return n

    async def list_media_keys(self, goods_id):
        return [m["object_key"] for m in self.media.get(goods_id, []) if m["object_key"]]

    # ---- 日志 ----
    async def insert_operation(self, *, actor, action, target_id, summary, target_type="goods",
                               before=None, after=None, undoable=False, seller_id=S):
        self._log_seq += 1
        self.logs.append({
            "id": self._log_seq, "seller_id": seller_id, "actor": actor, "action": action,
            "target_type": target_type, "target_id": target_id, "summary": summary,
            "before_json": before, "after_json": after, "undoable": bool(undoable),
            "undone_at": None,
        })
        return self._log_seq

    async def list_operations(self, seller_id, *, actor="", target_id="", limit=100):
        rows = [l for l in self.logs if l["seller_id"] == seller_id]
        if actor:
            rows = [l for l in rows if l["actor"] == actor]
        if target_id:
            rows = [l for l in rows if l["target_id"] == target_id]
        return list(reversed(rows))[:limit]

    async def get_operation(self, log_id, seller_id):
        for l in self.logs:
            if l["id"] == log_id and l["seller_id"] == seller_id:
                return dict(l)
        return None

    async def mark_operation_undone(self, log_id, seller_id):
        for l in self.logs:
            if l["id"] == log_id and l["seller_id"] == seller_id and not l["undone_at"] and l["undoable"]:
                l["undone_at"] = "now"
                return True
        return False


class FakeStorage:
    """假对象存储：只记录调用，不落盘。"""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    async def put(self, key, data, content_type=""):
        self.objects[key] = data
        return key

    async def get(self, key):
        return self.objects.get(key)

    async def delete(self, key):
        self.deleted.append(key)
        return self.objects.pop(key, None) is not None

    def url_for(self, key):
        return f"/api/seller/uploads/{key}"


# ============================================================
# 用例
# ============================================================


async def main() -> int:
    fake = FakeRepo()
    storage = FakeStorage()
    service.repo = fake                      # 注入假仓储
    service.get_storage = lambda: storage    # 注入假存储

    # ---- G1 创建 ----
    g = await service.create_goods(S, title="榨汁杯", price=99.0, cost_price=38.0,
                                   stock=3, stock_alert=10, category="厨房小电")
    check("G1a 默认草稿", g["status"] == "draft", str(g.get("status")))
    check("G1b 编号自动分配", g["goods_no"] == "G-0001", str(g.get("goods_no")))
    check("G1c 排序追加到末尾", g["sort_no"] == 1, str(g.get("sort_no")))

    # ---- G2 校验 ----
    for kw, label in (
        ({"title": "  "}, "空标题"),
        ({"title": "x", "price": -1}, "负价格"),
        ({"title": "x", "stock": -3}, "负库存"),
        ({"title": "x", "status": "weird"}, "非法状态"),
    ):
        try:
            await service.create_goods(S, **kw)
            check(f"G2 拒绝{label}", False, "竟然通过了")
        except GoodsError:
            check(f"G2 拒绝{label}", True)
    try:
        await service.update_goods(S, g["id"], nope=1)
        check("G2b 拒绝未知字段", False, "竟然通过了")
    except GoodsError:
        check("G2b 拒绝未知字段", True)

    # ---- G3 上架闸门 ----
    g2 = await service.create_goods(S, title="无价商品")
    try:
        await service.set_status(S, g2["id"], "on_shelf")
        check("G3a 未定价不可上架", False, "竟然通过了")
    except GoodsError as exc:
        check("G3a 未定价不可上架", exc.code == "invalid_field", exc.message)
    up = await service.set_status(S, g["id"], "on_shelf")
    check("G3b 已定价可上架", up["status"] == "on_shelf", str(up.get("status")))

    # ---- G4 留痕 ----
    await service.update_goods(S, g["id"], price=88.0)
    plog = [l for l in fake.logs if l["action"] == "update_price" and l["target_id"] == g["goods_no"]]
    check("G4 改价留痕含前后快照",
          bool(plog) and plog[-1]["before_json"] == {"price": 99.0}
          and plog[-1]["after_json"] == {"price": 88.0}, str(plog[-1:] if plog else []))

    # ---- G5 撤销 ----
    await service.undo_operation(S, plog[-1]["id"])
    check("G5a 撤销后回滚到 99.0", fake.goods[g["id"]]["price"] == 99.0,
          str(fake.goods[g["id"]]["price"]))
    try:
        await service.undo_operation(S, plog[-1]["id"])
        check("G5b 不可重复撤销", False, "竟然通过了")
    except GoodsError as exc:
        check("G5b 不可重复撤销", exc.code == "already_undone", exc.code)

    # ---- G8 图片（先测图片，供 G7 级联清理用）----
    up1 = await service.attach_media(S, g["id"], filename="ORIGINAL-NAME.png", data=b"x" * 10,
                                     content_type="image/png")
    check("G8a 首张图自动设为主图",
          fake.goods[g["id"]]["cover_url"] == up1["url"], str(fake.goods[g["id"]]["cover_url"]))
    # ⚠️ 断言必须用**独特**的原文件名：早先用 "a.png"，而随机 hex 恰好以 "a" 结尾时（…a.png）
    #    会命中子串导致假失败 —— 用随机值做子串匹配的断言天生不可靠。
    check("G8b key 由服务端生成（不含原文件名）",
          "ORIGINAL" not in up1["object_key"] and up1["object_key"].endswith(".png"),
          up1["object_key"])
    try:
        await service.attach_media(S, g["id"], filename="evil.exe", data=b"MZ")
        check("G8c 非图片扩展名被拒", False, "竟然通过了")
    except GoodsError:
        check("G8c 非图片扩展名被拒", True)
    try:
        await service.attach_media(S, g["id"], filename="big.png", data=b"x" * (6 * 1024 * 1024))
        check("G8d 超限被拒（413）", False, "竟然通过了")
    except GoodsError as exc:
        check("G8d 超限被拒（413）", exc.status_code == 413, str(exc.status_code))

    # 上传第二张 → 删除它不应影响主图；删除主图后顺延
    up2 = await service.attach_media(S, g["id"], filename="b.png", data=b"y" * 10)
    await service.detach_media(S, g["id"], up2["media_id"])
    check("G8e 删非主图不影响主图",
          fake.goods[g["id"]]["cover_url"] == up1["url"], str(fake.goods[g["id"]]["cover_url"]))
    await service.detach_media(S, g["id"], up1["media_id"])
    check("G8f 主图删空后 cover_url 置空",
          fake.goods[g["id"]]["cover_url"] is None, str(fake.goods[g["id"]]["cover_url"]))
    check("G8g 文件已从存储删除", len(storage.deleted) >= 2, str(storage.deleted))

    # ---- G6 批量 ----
    g3 = await service.create_goods(S, title="批量A", price=50.0)
    bulk = await service.bulk_action(S, [g["id"], g3["id"]], "price_delta", price_delta=-10.0)
    check("G6a 批量调价", bulk["done"] == [g["id"], g3["id"]] and fake.goods[g3["id"]]["price"] == 40.0,
          str(bulk))
    bad_bulk = await service.bulk_action(S, [g3["id"]], "delete")
    check("G6b 批量删除", bad_bulk["done"] == [g3["id"]] and g3["id"] not in fake.goods, str(bad_bulk))
    try:
        await service.bulk_action(S, [g["id"]], "nuke")
        check("G6c 拒绝非法批量动作", False, "竟然通过了")
    except GoodsError:
        check("G6c 拒绝非法批量动作", True)
    try:
        await service.bulk_action(S, [], "on_shelf")
        check("G6d 空选择被拒", False, "竟然通过了")
    except GoodsError:
        check("G6d 空选择被拒", True)

    # ---- G7 删除 + 级联 ----
    await service.attach_media(S, g["id"], filename="c.png", data=b"z" * 10)
    keys_before = list(storage.objects.keys())
    res = await service.delete_goods(S, g["id"])
    check("G7a 商品行已删", g["id"] not in fake.goods, str(res))
    check("G7b 图片行已清", fake.media.get(g["id"]) == [], str(fake.media.get(g["id"])))
    check("G7c 对象文件已清", all(k not in storage.objects for k in keys_before),
          f"{keys_before} vs {list(storage.objects.keys())}")
    dlog = [l for l in fake.logs if l["action"] == "delete_goods"]
    check("G7d 删除记为不可撤销", bool(dlog) and not dlog[-1]["undoable"])

    # ---- G9 概览 ----
    ov = await service.overview(S)
    check("G9 概览结构完整",
          {"goods", "low_stock", "low_stock_count", "recent_operations"} <= set(ov), str(ov)[:120])

    print(f"\n结果：{'全部通过' if not _failures else f'{len(_failures)} 项失败'}")
    for f in _failures:
        print(f"  - {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
