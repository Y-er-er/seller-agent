"""新建卖家账号（运维脚本，**不开放注册接口**）。

为什么是脚本而不是注册页：业务数据按 `seller_id` 隔离，自助注册出来的账号
看到的是一间**空店**（没有商品、没有知识库），对卖家是误导。要让某个账号
对接某间店，必须由人明确指定 `seller_id` —— 这种事适合脚本，不适合界面。

用法（在 `backend/` 下执行）：

    python scripts/create_seller_account.py --username alice --password '换成强口令' \
        --display-name 爱丽丝 --seller-id shop_alice

    # 只改密码（账号已存在时）
    python scripts/create_seller_account.py --username admin --password '新口令' --reset-password

安全约束：
- 密码只从命令行读取，**不写进任何日志文件**；口令会短暂出现在 shell history 里，
  生产环境请改用交互输入（本脚本不实现，避免把"看起来安全"当成安全）。
- `--reset-password` 只重置口令，不动 `seller_id` / 已有业务数据。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.db import seller_repo as repo  # noqa: E402
from app.seller.auth import AuthError, create_account  # noqa: E402
from app.seller.passwords import hash_password  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description="新建卖家账号 / 重置密码")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--display-name", default="")
    parser.add_argument("--seller-id", default="", help="业务数据归属；新建账号必填")
    parser.add_argument("--reset-password", action="store_true", help="账号已存在时只改密码")
    args = parser.parse_args()

    await repo.ensure_tables()

    if args.reset_password:
        account = await repo.get_account(args.username.strip())
        if not account:
            print(f"账号不存在：{args.username}")
            return 2
        await repo.set_password(account["id"], hash_password(args.password))
        print(f"已重置密码：{account['username']}（seller_id={account['seller_id']}）")
        return 0

    if not args.seller_id:
        print("新建账号必须指定 --seller-id（业务数据按它隔离）")
        return 2
    try:
        created = await create_account(
            username=args.username,
            password=args.password,
            seller_id=args.seller_id,
            display_name=args.display_name,
        )
    except AuthError as exc:
        print(f"创建失败：{exc.message}")
        return 1
    print(
        f"已创建：{created['username']}（seller_id={created['seller_id']}）\n"
        "提示：新 seller_id 是一间空店；要看到演示商品请用绑定 default 的账号。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
