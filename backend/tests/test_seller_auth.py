"""卖家登录测试：密码哈希 / 会话 / 401 / **每次登录都是新对话**。

覆盖：
  A 组 密码哈希（纯离线）：格式、正确/错误、盐随机、损坏哈希不抛异常
  B 组 会话 Token：不透明随机、线程号唯一
  C 组 数据库链路：自举幂等、登录成功/失败、禁用账号、退出立即失效、会话过期
  D 组 HTTP 契约：无 Token → 401；带 Token → 200；登录/退出/me 的返回结构
  E 组 限制：连续失败锁定（429）
  F 组 助手线程：同一账号两次登录 → 两个不同线程，且**互相看不到对方的对话**

前置：`backend/.env` 配了 `MYSQL_URI`。未配置时打印 SKIP 并以 0 退出。

⚠️ 会写库：只动**本测试自己创建**的账号（`t-auth-*`）与其会话，结束时删除；
   不动 `admin` 的密码（只会用它登录一次以验证演示账号可用）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from fastapi import HTTPException  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import assistant_repo  # noqa: E402
from app.db import seller_repo as repo  # noqa: E402
from app.db.connection import connect  # noqa: E402
from app.seller import auth  # noqa: E402
from app.seller.deps import require_session  # noqa: E402
from app.seller.passwords import (  # noqa: E402
    hash_password,
    new_session_token,
    new_thread_id,
    verify_password,
)

failures: list[str] = []

#: 本测试自建的账号前缀 —— 清理时只按它删，绝不横扫
PREFIX = "t-auth-"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(f"{name} {detail}".strip())


def _cleanup() -> None:
    """删掉测试账号及其会话（外键 ON DELETE CASCADE 会带走会话）。"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM seller_account WHERE username LIKE %s", (PREFIX + "%",))
        conn.commit()


def group_a() -> None:
    print("\n--- A 密码哈希 ---")
    stored = hash_password("s3cret-密码")
    check("A1 哈希串带算法与迭代次数（便于将来升级）",
          stored.startswith("pbkdf2_sha256$600000$"), stored[:32])
    check("A2 正确密码校验通过", verify_password("s3cret-密码", stored) is True)
    check("A3 错误密码校验失败", verify_password("s3cret", stored) is False)
    check("A4 空密码一律失败（不把空串当有效密码）", verify_password("", stored) is False)
    check("A5 同一密码两次哈希不同（盐随机）", hash_password("same") != hash_password("same"))
    check("A6 哈希串里不含明文密码", "s3cret" not in stored)
    # 损坏的哈希串不能抛异常 —— 否则一次坏数据就把登录接口变成 500（等于告知账号存在）
    for bad in ("", "garbage", "md5$1$a$b", "pbkdf2_sha256$notanint$a$b", "pbkdf2_sha256$1$!!!$!!!"):
        try:
            ok = verify_password("x", bad)
        except Exception as exc:  # noqa: BLE001
            ok = f"raised {type(exc).__name__}"
        check(f"A7 损坏哈希串不抛异常：{bad[:22] or '(空)'}", ok is False, str(ok))


def group_b() -> None:
    print("\n--- B Token / 线程号 ---")
    tokens = {new_session_token() for _ in range(50)}
    check("B1 Token 长度 43（32 字节 urlsafe base64 去填充）", all(len(t) == 43 for t in tokens),
          str(sorted({len(t) for t in tokens})))
    check("B2 Token 不重复", len(tokens) == 50, str(len(tokens)))
    check("B3 Token 不含 '=' 填充（可直接放进请求头）", all("=" not in t for t in tokens))
    threads = {new_thread_id() for _ in range(50)}
    check("B4 线程号不重复", len(threads) == 50, str(len(threads)))
    check("B5 线程号 ≤ 64 字符（assistant_message.thread_id 上限）",
          all(len(t) <= 64 for t in threads))
    check("B6 线程号带 t- 前缀（日志里能一眼认出是登录态线程）",
          all(t.startswith("t-") for t in threads))


async def group_c() -> None:
    print("\n--- C 数据库链路 ---")
    await repo.ensure_tables()
    check("C1 建表幂等（第二次不报错）", True)

    first = await auth.bootstrap()
    second = await auth.bootstrap()
    check("C2 自举幂等：账号表非空时不再创建", second is None or first is not None, str(second))

    account = await auth.create_account(
        username=PREFIX + "alice", password="pass-alice", seller_id="shop_alice",
        display_name="爱丽丝",
    )
    check("C3 新建账号绑定自己的 seller_id", account["seller_id"] == "shop_alice", str(account))

    try:
        await auth.create_account(username=PREFIX + "alice", password="x", seller_id="s2")
        check("C4 同名账号应被拒绝", False, "竟然创建成功")
    except auth.AuthError as exc:
        check("C4 同名账号被拒绝（409）", exc.status_code == 409 and exc.code == "username_taken",
              f"{exc.status_code} {exc.code}")

    try:
        await auth.login(PREFIX + "alice", "wrong-pass")
        check("C5 错误密码应被拒绝", False, "竟然登录成功")
    except auth.AuthError as exc:
        check("C5 错误密码 → 401 且**不透露账号是否存在**",
              exc.status_code == 401 and "账号或密码" in exc.message, exc.message)

    try:
        await auth.login(PREFIX + "nobody-here", "whatever")
        check("C6 不存在的账号 → 同一句提示", False, "竟然登录成功")
    except auth.AuthError as exc:
        check("C6 不存在的账号 → 与密码错**同一句提示**（防账号枚举）",
              exc.status_code == 401 and "账号或密码" in exc.message, exc.message)

    result = await auth.login(PREFIX + "alice", "pass-alice")
    check("C7 登录成功返回 Token 与自己身份",
          bool(result["token"]) and result["seller"]["seller_id"] == "shop_alice",
          str(result["seller"]))
    check("C8 登录返回本次会话线程号", bool(result["thread_id"]), result["thread_id"])

    session = await auth.resolve(result["token"])
    check("C9 Token 能解析成会话（含 seller_id / thread_id）",
          session is not None and session["seller_id"] == "shop_alice"
          and session["thread_id"] == result["thread_id"], str(session and session["thread_id"]))

    second_login = await auth.login(PREFIX + "alice", "pass-alice")
    check("C10 **同一账号再登录 → 换一条新线程**（每次登录都是新对话）",
          second_login["thread_id"] != result["thread_id"],
          f"{result['thread_id']} vs {second_login['thread_id']}")
    check("C11 两次登录的 Token 也不同（旧会话不因此失效，但互不覆盖）",
          second_login["token"] != result["token"])

    check("C12 退出登录返回 True", await auth.logout(result["token"]) is True)
    check("C13 退出后 Token 立即失效（不等过期）", await auth.resolve(result["token"]) is None)
    check("C14 另一个会话不受影响（退出只影响当前这一次登录）",
          await auth.resolve(second_login["token"]) is not None)
    await auth.logout(second_login["token"])

    # 过期：直接把 expires_at 拨到过去，模拟"放了 12 小时"
    expired = await auth.login(PREFIX + "alice", "pass-alice")
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE seller_session SET expires_at = DATE_SUB(NOW(), INTERVAL 1 MINUTE) WHERE token=%s",
            (expired["token"],),
        )
        conn.commit()
    check("C15 过期会话解析为 None（前端会收到 401 去重新登录）",
          await auth.resolve(expired["token"]) is None)

    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE seller_account SET status='disabled' WHERE username=%s", (PREFIX + "alice",))
        conn.commit()
    try:
        await auth.login(PREFIX + "alice", "pass-alice")
        check("C16 停用账号应被拒绝", False, "竟然登录成功")
    except auth.AuthError as exc:
        check("C16 停用账号 → 403 account_disabled",
              exc.status_code == 403 and exc.code == "account_disabled", exc.code)
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE seller_account SET status='active' WHERE username=%s", (PREFIX + "alice",))
        conn.commit()

    # 停用账号**已发的 Token** 也要立即失效（否则"停用"只是不能新登录）
    alive = await auth.login(PREFIX + "alice", "pass-alice")
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE seller_account SET status='disabled' WHERE username=%s", (PREFIX + "alice",))
        conn.commit()
    check("C17 账号被停用后，已发的 Token 立即不可用",
          await auth.resolve(alive["token"]) is None)
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE seller_account SET status='active' WHERE username=%s", (PREFIX + "alice",))
        conn.commit()

    # 每次登录新线程 → 对话不串
    s1 = await auth.login(PREFIX + "alice", "pass-alice")
    await assistant_repo.append_message(s1["thread_id"], "seller", "第一段对话的问题",
                                        seller_id="shop_alice")
    s2 = await auth.login(PREFIX + "alice", "pass-alice")
    old_msgs = await assistant_repo.list_messages(s1["thread_id"], limit=50)
    new_msgs = await assistant_repo.list_messages(s2["thread_id"], limit=50)
    check("C18 上一段对话留在旧线程里（不是被删掉，只是不再展示）",
          len(old_msgs) == 1, str(len(old_msgs)))
    check("C19 新登录的线程里**看不到**上一段对话",
          len(new_msgs) == 0, str([m["content"] for m in new_msgs]))
    # 清理本次写入的对话，避免污染演示数据
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM assistant_message WHERE thread_id IN (%s, %s)",
                    (s1["thread_id"], s2["thread_id"]))
        conn.commit()
    await auth.logout(s1["token"])
    await auth.logout(s2["token"])


async def group_d() -> None:
    print("\n--- D HTTP 契约 ---")
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r = client.get("/api/seller/overview")
    check("D1 没带 Token 访问业务接口 → 401", r.status_code == 401, str(r.status_code))
    check("D2 401 的提示要求重新登录", "登录" in str(r.json().get("detail")), str(r.json()))

    r = client.post("/api/seller/auth/login",
                    json={"username": settings.seller_bootstrap_username,
                          "password": settings.seller_bootstrap_password})
    check("D3 用演示账号登录成功", r.status_code == 200, str(r.status_code))
    data = r.json()
    check("D4 返回 token / thread_id / seller",
          {"token", "thread_id", "seller"} <= set(data), str(sorted(data)))
    token = data["token"]
    headers = {"X-Seller-Token": token}

    r = client.get("/api/seller/auth/me", headers=headers)
    check("D5 /me 返回当前账号", r.status_code == 200
          and r.json()["seller"]["username"] == settings.seller_bootstrap_username, str(r.json()))
    check("D6 /me 的 thread_id 与登录返回一致", r.json()["thread_id"] == data["thread_id"])

    r = client.get("/api/seller/overview", headers=headers)
    check("D7 带 Token 访问业务接口 → 200", r.status_code == 200, str(r.status_code))

    r = client.get("/api/seller/assistant/messages", headers=headers)
    check("D8 新登录的助手对话为空（新对话）",
          r.status_code == 200 and r.json()["items"] == [], str(r.json())[:120])

    r = client.get("/api/seller/overview", headers={"X-Seller-Token": "forged-token-xxx"})
    check("D9 伪造 Token → 401（不靠前端自觉）", r.status_code == 401, str(r.status_code))

    r = client.post("/api/seller/auth/login", json={"username": "admin", "password": "bad"})
    check("D10 密码错 → 401", r.status_code == 401, str(r.status_code))

    r = client.post("/api/seller/auth/logout", headers=headers)
    check("D11 退出成功", r.status_code == 200 and r.json().get("ok") is True, str(r.json()))
    r = client.get("/api/seller/auth/me", headers=headers)
    check("D12 退出后同一 Token → 401", r.status_code == 401, str(r.status_code))


async def group_e() -> None:
    print("\n--- E 连续失败锁定 ---")
    username = PREFIX + "bruteforce"
    await auth.create_account(username=username, password="right-pass", seller_id="shop_bf")
    limit = settings.seller_login_max_fails
    last_code = ""
    for _ in range(limit + 1):
        try:
            await auth.login(username, "bad-pass")
        except auth.AuthError as exc:
            last_code = exc.code
    check(f"E1 连续失败 {limit} 次后锁定（429 too_many_attempts）",
          last_code == "too_many_attempts", last_code)

    # 锁定期内**正确密码也不放行**：否则限流形同虚设
    try:
        await auth.login(username, "right-pass")
        check("E2 锁定期内正确密码也应被拒", False, "竟然登录成功")
    except auth.AuthError as exc:
        check("E2 锁定期内正确密码也被拒（限流不是摆设）",
              exc.code == "too_many_attempts", exc.code)

    # 计数是进程内的：清掉后能正常登录（这条是**已知边界**，写出来比藏着好）
    auth._clear_fails(username.lower())
    result = await auth.login(username, "right-pass")
    check("E3 计数清除后可正常登录（单进程内存计数，重启即清零）",
          bool(result["token"]))
    await auth.logout(result["token"])


async def group_f() -> None:
    print("\n--- F 依赖注入 ---")
    try:
        await require_session(None)
        check("F1 无 Token 调用 require_session 应 401", False, "竟然通过")
    except HTTPException as exc:
        check("F1 无 Token → 401", exc.status_code == 401, str(exc.status_code))
    try:
        await require_session("nope")
        check("F2 无效 Token 应 401", False, "竟然通过")
    except HTTPException as exc:
        check("F2 无效 Token → 401", exc.status_code == 401, str(exc.status_code))

    result = await auth.login(PREFIX + "alice", "pass-alice")
    sess = await require_session(result["token"])
    check("F3 有效 Token 解析出 seller_id 与 thread_id",
          sess.seller_id == "shop_alice" and sess.thread_id == result["thread_id"],
          f"{sess.seller_id} {sess.thread_id}")
    check("F4 会话里不携带密码哈希（避免随响应泄漏）",
          not hasattr(sess, "password_hash"))
    await auth.logout(result["token"])


async def main() -> int:
    if not settings.mysql_uri:
        print("SKIP：未配置 MYSQL_URI，登录链路需要数据库")
        return 0

    await repo.ensure_tables()
    _cleanup()                       # 上次异常中断留下的残留先清掉
    group_a()
    group_b()
    await group_c()
    await group_d()
    await group_e()
    await group_f()
    _cleanup()

    print(f"\n结果：{'全部通过' if not failures else f'{len(failures)} 项失败'}")
    for item in failures:
        print(f"  - {item}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
