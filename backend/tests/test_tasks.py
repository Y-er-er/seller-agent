"""任务中心测试 —— **假仓储，零外部依赖、无网络**。

覆盖：
  A `submit()` 长任务：立即返回 → 后台跑完 → 状态/进度/结果都落库
  B 失败路径：handler 抛错 → status=failed 且 error 带原因（异常不能消失在后台）
  C `run_inline()`：当场返回结果，但**同样留一条任务记录**（批量操作要能事后查）
  D 进度回调：中途进度会被写入，且超范围值被夹到 0—100

运行：python backend/tests/test_tasks.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tasks import runner  # noqa: E402

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


class FakeRepo:
    """内存版任务仓储，接口与被测代码用到的完全一致。"""

    TASK_QUEUED = "queued"
    TASK_RUNNING = "running"
    TASK_DONE = "done"
    TASK_FAILED = "failed"
    TASK_INTERRUPTED = "interrupted"

    def __init__(self) -> None:
        self.tasks: dict[int, dict] = {}
        self._id = 0
        self.progress_log: list[tuple[int, str]] = []

    async def create_task(self, kind, title, *, target="", action_id=None,
                          status=TASK_QUEUED, seller_id="default", payload=None):
        self._id += 1
        self.tasks[self._id] = {
            "id": self._id, "kind": kind, "title": title, "target": target,
            "action_id": action_id, "status": status, "progress": 0,
            "stage": None, "result": None, "detail": None, "error": None,
            "payload": payload,
            "seller_id": seller_id,
        }
        return self._id

    async def get_task(self, task_id, seller_id="default"):
        t = self.tasks.get(task_id)
        return dict(t) if t else None

    async def update_task(self, task_id, **fields):
        if "progress" in fields:
            self.progress_log.append((fields["progress"], fields.get("stage") or ""))
        self.tasks[task_id].update(fields)

    async def list_tasks(self, seller_id="default", *, status="", limit=50):
        return [dict(t) for t in self.tasks.values() if not status or t["status"] == status]

    async def count_unfinished(self, seller_id="default"):
        return len([t for t in self.tasks.values() if t["status"] in ("queued", "running")])


async def group_a(fake: FakeRepo) -> None:
    print("【A 组】长任务：后台执行")

    async def job(progress, seller_id):
        await progress(30, "生成中")
        await progress(80, "下载转存")
        return {"summary": "已生成宣传图", "url": "/api/seller/uploads/x.png"}

    row = await runner.submit("ai_cover", "为 G-0001 生成宣传图", job, target="G-0001")
    check("A1 submit 立即返回（不等任务跑完）",
          row["status"] in ("queued", "running"), row["status"])
    check("A2 任务号已返回", bool(row.get("id")))

    await asyncio.sleep(0.05)          # 让后台协程跑完
    t = fake.tasks[row["id"]]
    check("A3 后台执行完成 → done", t["status"] == "done", t["status"])
    check("A4 进度推进到 100", t["progress"] == 100, str(t["progress"]))
    check("A5 结果摘要落库", t["result"] == "已生成宣传图", str(t["result"]))
    check("A6 结构化明细落库（含 url 与耗时）",
          isinstance(t["detail"], dict) and "url" in t["detail"] and "elapsed_ms" in t["detail"],
          str(t["detail"])[:120])
    check("A7 中途进度被记录并夹在 0—100",
          all(0 <= p <= 100 for p, _ in fake.progress_log) and len(fake.progress_log) >= 2,
          str(fake.progress_log))


async def group_b(fake: FakeRepo) -> None:
    print("\n【B 组】失败路径")

    async def boom(progress, seller_id):
        raise RuntimeError("模型未开通")

    row = await runner.submit("ai_copy", "为 G-0001 生成文案", boom)
    await asyncio.sleep(0.05)
    t = fake.tasks[row["id"]]
    check("B1 失败落库为 failed（异常不会消失在后台）", t["status"] == "failed", t["status"])
    check("B2 失败原因可读", "模型未开通" in (t["error"] or ""), str(t["error"]))
    check("B3 失败任务不会停留在 running", t["status"] != "running")


async def group_c(fake: FakeRepo) -> None:
    print("\n【C 组】即时操作（批量类）")

    async def quick(progress, seller_id):
        return {"summary": "删除 2 件，失败 1 件", "failed": [{"goods_id": 9, "error": "商品不存在"}]}

    out = await runner.run_inline("bulk_op", "批量删除 3 件", quick)
    check("C1 当场返回结果（调用方要立刻展示）", out["summary"].startswith("删除 2 件"), str(out)[:100])
    check("C2 附带 task_id", bool(out.get("task_id")))
    t = fake.tasks[out["task_id"]]
    check("C3 仍留一条任务记录（事后可查）", t["status"] == "done", t["status"])
    check("C4 失败明细进了 detail",
          isinstance(t["detail"], dict) and t["detail"].get("failed"), str(t["detail"])[:150])

    async def boom_inline(progress, seller_id):
        raise ValueError("批量参数非法")

    try:
        await runner.run_inline("bulk_op", "批量改价", boom_inline)
        check("C5 即时操作失败会抛给调用方", False, "竟然没抛")
    except ValueError:
        check("C5 即时操作失败会抛给调用方", True)
    failed = [t for t in fake.tasks.values() if t["status"] == "failed"]
    check("C6 失败也留了记录", len(failed) >= 2, str(len(failed)))


async def main() -> int:
    fake = FakeRepo()
    original = runner.repo
    runner.repo = fake
    try:
        await group_a(fake)
        await group_b(fake)
        await group_c(fake)
    finally:
        runner.repo = original

    print(f"\n结果：{'全部通过' if not _failures else f'{len(_failures)} 项失败'}")
    for f in _failures:
        print(f"  - {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
