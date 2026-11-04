"""密码哈希与校验（标准库实现，不引入新依赖）。

格式：`pbkdf2_sha256$<迭代次数>$<盐 base64>$<摘要 base64>`

三条约束：

1. **绝不存明文、绝不存裸 SHA**：PBKDF2-HMAC-SHA256 + 每账号独立随机盐，
   迭代次数写进哈希串本身 —— 将来提高迭代次数时，旧哈希仍能校验通过。
2. **校验用常数时间比较**（`hmac.compare_digest`），避免按字节比较泄漏时序信息。
3. 迭代次数属于**安全参数**，写在代码里而不是 `.env`：它必须与已存哈希串一致才能校验，
   做成可配置只会制造"改配置 = 所有账号登不进去"的坑。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

#: PBKDF2 迭代次数。OWASP 对 PBKDF2-HMAC-SHA256 的建议量级（60 万次）；
#: `hashlib.pbkdf2_hmac` 是 C 实现，实测单次约 0.15s —— 只在登录时发生，可接受。
_ITERATIONS = 600_000
_ALGORITHM = "pbkdf2_sha256"
_SALT_BYTES = 16


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str, *, iterations: int = _ITERATIONS) -> str:
    """生成可入库的哈希串。盐每次随机 —— 同样的密码，两次哈希结果不同。"""
    if not password:
        raise ValueError("密码不能为空")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGORITHM}${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码。

    任何异常（格式损坏 / 算法不认识）都返回 False —— **校验失败不能抛异常**，
    否则调用方一不小心就把"哈希损坏"变成 500，等于告诉攻击者账号存在且数据有异。
    """
    if not password or not stored:
        return False
    try:
        algorithm, raw_iterations, raw_salt, raw_digest = stored.split("$")
        if algorithm != _ALGORITHM:
            return False
        iterations = int(raw_iterations)
        salt = base64.b64decode(raw_salt, validate=True)
        expected = base64.b64decode(raw_digest, validate=True)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def new_session_token() -> str:
    """会话 Token：32 字节随机数（43 字符 urlsafe base64）。

    **不透明**、不签名、不承载任何用户信息 —— 权限只来自库里的会话行，
    这样"退出登录立即失效"才成立（纯签名 Token 只能等它自然过期）。
    """
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def new_thread_id() -> str:
    """本次登录专属的助手会话线程号。

    `assistant_message.thread_id` 是 `VARCHAR(64)`：这里固定 14 个字符，
    留足余量，也便于在日志里一眼认出"这是登录态线程"（`t-` 前缀）。
    """
    return "t-" + secrets.token_hex(6)
