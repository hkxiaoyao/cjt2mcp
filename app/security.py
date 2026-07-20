"""安全相关：凭据加密包装、管理员口令、MCP Key、Session 校验。

- 租户敏感字段的加解密统一走 encrypt_secret/decrypt_secret（内部用平台主密钥做 AES-GCM）。
- 管理员口令用 bcrypt 存哈希。
- MCP Key 只存 SHA-256 哈希 + 前缀，完整 Key 仅创建时返回一次。
- 后台请求通过 require_admin 依赖校验 Session。
"""

from __future__ import annotations

import hashlib
import secrets

import bcrypt
from fastapi import Request
from starlette.exceptions import HTTPException

from app.config import get_settings
from app.crypto import decrypt_field, encrypt_field


# ─────────────────────────── 租户凭据字段加解密 ───────────────────────────

def encrypt_secret(plaintext: str) -> str:
    """用平台主密钥 AES-GCM 加密敏感字段（appSecret/消息秘钥/certificate/token）。"""
    return encrypt_field(plaintext, get_settings().master_key_bytes())


def decrypt_secret(token: str) -> str:
    """解密 encrypt_secret 产生的密文。"""
    return decrypt_field(token, get_settings().master_key_bytes())


# ─────────────────────────── 管理员口令 ───────────────────────────

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


# ─────────────────────────── MCP Key ───────────────────────────

def generate_mcp_key(client_code: str) -> tuple[str, str, str]:
    """生成一个 MCP Key。

    :return: (完整明文 Key, 前缀, SHA-256 哈希)
    完整 Key 形如 mcp_<code>_<随机串>；前缀取 mcp_<code>_ + 随机串前 4 位便于识别。
    """
    code = client_code.lower()
    rand = secrets.token_urlsafe(32).replace("-", "").replace("_", "")[:40]
    plain = f"mcp_{code}_{rand}"
    prefix = f"mcp_{code}_{rand[:4]}"
    return plain, prefix, hash_api_key(plain)


def hash_api_key(plain_key: str) -> str:
    """MCP Key 的 SHA-256 哈希（用于比对，不可逆）。"""
    return hashlib.sha256(plain_key.encode("utf-8")).hexdigest()


# ─────────────────────────── Session / 管理员校验 ───────────────────────────

def login_session(request: Request, admin_id: str, username: str) -> None:
    request.session["admin_id"] = admin_id
    request.session["admin_username"] = username


def logout_session(request: Request) -> None:
    request.session.clear()


def current_admin(request: Request) -> dict | None:
    """从 Session 读取当前登录管理员；未登录返回 None。"""
    admin_id = request.session.get("admin_id")
    if not admin_id:
        return None
    return {"id": admin_id, "username": request.session.get("admin_username")}


def require_admin(request: Request) -> dict:
    """FastAPI 依赖：要求已登录管理员。

    页面请求未登录时重定向到登录页（通过抛出带 Location 的 303）；
    这里统一抛 401，由页面路由层决定是否转成重定向。
    """
    admin = current_admin(request)
    if admin is None:
        raise HTTPException(status_code=401, detail="未登录")
    return admin
