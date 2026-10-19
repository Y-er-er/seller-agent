"""端口与适配器**契约测试**（全部离线，零外部调用、零计费）。

为什么需要这个文件：可插拔能否成立，取决于"同一套用例在每个适配器上表现一致"。
没有契约测试，两个实现会在细节上悄悄分叉 —— 等切到生产才发现字段对不上，那时成本最大。

新增适配器时（例如未来的 `CosObjectStorage`、或另一种数据源），**照着本文件跑一遍**，
通过即视为满足契约。

覆盖：
  A 映射契约    to_order / to_logistics / to_profile（正常 / 缺字段 / 脏值 / 空）
  B 数据源契约  MockDataSource 与 HttpDataSource 在"取不到即 None"上的一致性，
                以及 **remote 绝不回落到本地夹具**
  C 存储契约    LocalObjectStorage 的往返 / 边界 / **路径穿越拒绝**

运行（无需 MySQL、无网络、不调用模型）：
    D:\\Anaconda1\\envs\\pythonproject\\python.exe backend/tests/test_ports_contract.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.coerce import coerce_datetime, coerce_enum, coerce_int  # noqa: E402
from app.adapters.datasource_http import HttpDataSource, reset_warnings  # noqa: E402
from app.adapters.datasource_mock import MockDataSource, get_fixtures  # noqa: E402
from app.adapters.mapping import to_logistics, to_order, to_profile  # noqa: E402
from app.adapters.storage_local import LocalObjectStorage  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import LogisticsStatus  # noqa: E402
from app.ports import (  # noqa: E402
    DataSource,
    ObjectStorage,
    get_datasource,
    reset_datasource,
)

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


# ============================================================
# A 组：映射契约
# ============================================================


def run_mapping_contract() -> None:
    print("【A 组】映射契约（外部原始数据 → 领域模型）")

    # A1 空数据 → None（两种"空"都要覆盖：None 与 不传）
    check("A1a to_order(None) → None", to_order("O1", None) is None)
    check("A1b to_profile(None) → None", to_profile("O1", None) is None)

    # A2 正常字段映射
    order = to_order("O1", {"item_category": "日用百货", "pay_amount": 39.0,
                            "logistics_status": "signed", "signed_at": "2026-10-10T14:22:00"})
    check("A2a 订单字段映射正确",
          order is not None and order.item_category == "日用百货" and order.pay_amount == 39.0
          and order.logistics_status is LogisticsStatus.SIGNED and order.signed_at is not None,
          str(order))

    # A3 缺字段 → 默认值（不因单字段缺失整条丢弃）
    sparse = to_order("O2", {})
    check("A3a 空对象仍产出对象（不整条丢弃）", sparse is not None, str(sparse))
    check("A3b 金额缺失 → 0.0", sparse is not None and sparse.pay_amount == 0.0)
    check("A3c 物流状态缺失 → in_transit",
          sparse is not None and sparse.logistics_status is LogisticsStatus.IN_TRANSIT)

    # A4 脏值 → 回落默认值，不抛异常
    dirty = to_order("O3", {"pay_amount": "abc", "logistics_status": "NOT_A_STATUS",
                            "signed_at": "不是时间", "item_category": None})
    check("A4a 脏值不抛异常且回落默认",
          dirty is not None and dirty.pay_amount == 0.0
          and dirty.logistics_status is LogisticsStatus.IN_TRANSIT
          and dirty.signed_at is None and dirty.item_category == "",
          str(dirty))

    # A5 画像默认值（信用分兜底 600，与契约一致）
    profile = to_profile("O1", {"buyer_refund_rate_90d": 0.05})
    check("A5a 信用分缺失 → 600", profile is not None and profile.buyer_credit_score == 600, str(profile))
    check("A5b 退款率已映射", profile is not None and profile.buyer_refund_rate_90d == 0.05)

    # A6 物流轨迹强转字符串
    lg = to_logistics("O1", {"traces": ["已揽收", 123, None]})
    check("A6 轨迹元素强转字符串", lg is not None and lg.traces == ["已揽收", "123", "None"], str(lg))

    # A7 coerce 层独立可用（它是适配层基础设施）
    check("A7a coerce_int 异常输入回落", coerce_int("x", 7) == 7 and coerce_int(None, 7) == 7)
    check("A7b coerce_datetime 空串 → None", coerce_datetime("") is None)
    check("A7c coerce_enum 非法值回落", coerce_enum(LogisticsStatus, "x", LogisticsStatus.SIGNED)
          is LogisticsStatus.SIGNED)


# ============================================================
# B 组：数据源契约
# ============================================================


async def run_datasource_contract() -> None:
    print("\n【B 组】数据源契约（两个适配器行为一致 + remote 不回落）")

    original = {
        "order": settings.order_api_base,
        "logistics": settings.logistics_api_base,
        "aftersale": settings.aftersale_api_base,
        "data_source": settings.data_source,
    }
    try:
        # ---- B1 两个适配器都满足 DataSource 协议 ----
        check("B1a MockDataSource 符合 DataSource",
              isinstance(MockDataSource(), DataSource))
        check("B1b HttpDataSource 符合 DataSource",
              isinstance(HttpDataSource(), DataSource))

        # ---- B2 mock 适配器：注入即可取，未注入即 None ----
        settings.data_source, settings.order_api_base = "local", ""
        settings.logistics_api_base = settings.aftersale_api_base = ""
        reset_datasource()
        fixtures = get_fixtures()
        fixtures.clear()
        mock = get_datasource()
        check("B2a local 模式解析为 MockDataSource", isinstance(mock, MockDataSource))
        check("B2b 未注入 → None", await mock.fetch_order("C-NONE") is None)

        fixtures.seed_order("C-1", pay_amount=39.0, item_category="日用百货")
        fixtures.seed_logistics("C-1", logistics_status="signed")
        fixtures.seed_profile("C-1", buyer_credit_score=700)
        got = await mock.fetch_order("C-1")
        check("B2c 注入后可取（走同一套映射）",
              got is not None and got.pay_amount == 39.0 and got.item_category == "日用百货", str(got))
        check("B2d 物流/画像同样可取",
              (await mock.fetch_logistics("C-1")) is not None
              and (await mock.fetch_profile("C-1")) is not None)

        # ---- B3 remote 适配器：**绝不回落到夹具**（本设计的核心不变量）----
        settings.data_source, settings.order_api_base = "remote", ""
        reset_datasource()
        reset_warnings()
        http = get_datasource()
        check("B3a remote 模式解析为 HttpDataSource", isinstance(http, HttpDataSource))
        check("B3b remote 且未配 base → None（而不是读夹具）",
              await http.fetch_order("C-1") is None,
              "夹具里明明有 C-1，仍必须返回 None")
        check("B3c 同上：物流 / 画像",
              await http.fetch_logistics("C-1") is None
              and await http.fetch_profile("C-1") is None)

        # ---- B4 网络失败 → None，且不抛异常（离线：指向不可达端口）----
        settings.order_api_base = "http://127.0.0.1:1"
        reset_datasource()
        try:
            dead = await get_datasource().fetch_order("C-1")
            check("B4 不可达地址 → None 且不抛异常", dead is None)
        except Exception as exc:  # noqa: BLE001
            check("B4 不可达地址 → None 且不抛异常", False, f"抛了 {type(exc).__name__}: {exc}")

        # ---- B5 自动推断：不显式配置时，有 base 即 remote ----
        settings.data_source, settings.order_api_base = "", "http://127.0.0.1:1"
        reset_datasource()
        check("B5a 未显式配置 + 有 base → 推断为 remote",
              isinstance(get_datasource(), HttpDataSource), settings.resolved_data_source)
        settings.order_api_base = ""
        reset_datasource()
        check("B5b 未显式配置 + 无 base → 推断为 local",
              isinstance(get_datasource(), MockDataSource), settings.resolved_data_source)
    finally:
        settings.order_api_base = original["order"]
        settings.logistics_api_base = original["logistics"]
        settings.aftersale_api_base = original["aftersale"]
        settings.data_source = original["data_source"]
        reset_datasource()


# ============================================================
# C 组：存储契约
# ============================================================


async def run_storage_contract() -> None:
    print("\n【C 组】存储契约（LocalObjectStorage）")

    with tempfile.TemporaryDirectory() as tmp:
        store = LocalObjectStorage(root=tmp)
        check("C1 符合 ObjectStorage 协议", isinstance(store, ObjectStorage))

        # C2 往返
        key = "202610/demo.jpg"
        returned = await store.put(key, b"hello-bytes", "image/jpeg")
        check("C2a put 返回 key 本身", returned == key, returned)
        check("C2b get 取回同样内容", await store.get(key) == b"hello-bytes")

        # C3 不存在 → None / False（读通道可降级）
        check("C3a get 不存在 → None", await store.get("202610/nope.jpg") is None)
        check("C3b delete 不存在 → False", await store.delete("202610/nope.jpg") is False)

        # C4 删除
        check("C4a delete 已存在 → True", await store.delete(key) is True)
        check("C4b 删除后取不到", await store.get(key) is None)

        # C5 **路径穿越拒绝**（安全边界，必须挡住）
        for bad in ("../evil.txt", "a/../../evil.txt", "/etc/passwd", "..\\evil.txt", ""):
            blocked_get = await store.get(bad)
            blocked_del = await store.delete(bad)
            put_raised = False
            try:
                await store.put(bad, b"x")
            except ValueError:
                put_raised = True
            except Exception:  # noqa: BLE001
                put_raised = False
            check(f"C5 拒绝非法 key {bad!r}",
                  blocked_get is None and blocked_del is False and put_raised,
                  f"get={blocked_get} delete={blocked_del} put_raised={put_raised}")
        check("C5z 根目录之外未产生文件",
              not (Path(tmp).parent / "evil.txt").exists())

        # C6 URL 由实现给出（调用方禁止自己拼）
        check("C6a url_for 形态正确",
              store.url_for("202610/demo.jpg") == "/api/seller/uploads/202610/demo.jpg",
              store.url_for("202610/demo.jpg"))
        check("C6b url_for 容忍前导斜杠",
              store.url_for("/202610/demo.jpg") == "/api/seller/uploads/202610/demo.jpg")


async def main() -> int:
    run_mapping_contract()
    await run_datasource_contract()
    await run_storage_contract()

    total = len(_failures)
    print(f"\n结果：{'全部通过' if not total else f'{total} 项失败'}")
    for f in _failures:
        print(f"  - {f}")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
