"""数据访问层：租户、管理员的增删改查。

敏感畅捷通凭据（appSecret/消息秘钥/certificate）在写入前经 security.encrypt_secret
加密，读取返回的是加密态；需要明文时由调用方显式 decrypt_secret。
本层只负责持久化，不在返回值里回显明文密钥。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db import get_conn
from app.security import encrypt_secret, hash_password


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


# ─────────────────────────── 管理员 ───────────────────────────

def get_admin_by_username(username: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM admin_users WHERE username = ? AND enabled = 1", (username,)
        ).fetchone()
        return dict(row) if row else None


def ensure_initial_admin(username: str, password: str) -> None:
    """首次启动时创建初始管理员（若同名不存在）。password 为空则跳过。"""
    if not username or not password:
        return
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM admin_users WHERE username = ?", (username,)
        ).fetchone()
        if exists:
            return
        conn.execute(
            "INSERT INTO admin_users (id, username, password_hash, display_name, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (_new_id(), username, hash_password(password), username, _now()),
        )


def touch_admin_login(admin_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE admin_users SET last_login_at = ? WHERE id = ?", (_now(), admin_id)
        )


# ─────────────────────────── 租户 ───────────────────────────

def list_tenants() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tenants ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_tenant(tenant_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        return dict(row) if row else None


def get_tenant_by_code(client_code: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tenants WHERE client_code = ?", (client_code,)
        ).fetchone()
        return dict(row) if row else None


def create_tenant(
    *,
    client_code: str,
    client_name: str,
    app_key: str | None = None,
    app_secret: str | None = None,
    msg_secret: str | None = None,
    certificate: str | None = None,
    contact: str | None = None,
    phone: str | None = None,
    remark: str | None = None,
) -> str:
    """新建租户。敏感凭据（appSecret/消息秘钥/certificate）在此加密入库。

    :return: 新租户 id
    :raises ValueError: client_code 已存在
    """
    if get_tenant_by_code(client_code):
        raise ValueError(f"客户编码已存在：{client_code}")

    tenant_id = _new_id()
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO tenants ("
            "  id, client_code, client_name, contact, phone, remark, enabled,"
            "  app_key, app_secret_enc, msg_secret_enc, certificate_enc,"
            "  auth_status, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                tenant_id, client_code, client_name, contact, phone, remark,
                app_key or None,
                encrypt_secret(app_secret) if app_secret else None,
                encrypt_secret(msg_secret) if msg_secret else None,
                encrypt_secret(certificate) if certificate else None,
                now, now,
            ),
        )
    return tenant_id


def update_tenant_credentials(
    tenant_id: str,
    *,
    app_key: str | None = None,
    app_secret: str | None = None,
    msg_secret: str | None = None,
    certificate: str | None = None,
) -> None:
    """更新租户凭据。仅更新显式传入（非 None）的字段，敏感字段加密。"""
    sets: list[str] = []
    params: list[Any] = []
    if app_key is not None:
        sets.append("app_key = ?")
        params.append(app_key or None)
    if app_secret is not None:
        sets.append("app_secret_enc = ?")
        params.append(encrypt_secret(app_secret) if app_secret else None)
    if msg_secret is not None:
        sets.append("msg_secret_enc = ?")
        params.append(encrypt_secret(msg_secret) if msg_secret else None)
    if certificate is not None:
        sets.append("certificate_enc = ?")
        params.append(encrypt_secret(certificate) if certificate else None)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(_now())
    params.append(tenant_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE tenants SET {', '.join(sets)} WHERE id = ?", params)


def update_tenant_basic(
    tenant_id: str,
    *,
    client_name: str | None = None,
    contact: str | None = None,
    phone: str | None = None,
    remark: str | None = None,
) -> None:
    sets: list[str] = []
    params: list[Any] = []
    for col, val in (
        ("client_name", client_name),
        ("contact", contact),
        ("phone", phone),
        ("remark", remark),
    ):
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(val)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(_now())
    params.append(tenant_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE tenants SET {', '.join(sets)} WHERE id = ?", params)


def set_tenant_enabled(tenant_id: str, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE tenants SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, _now(), tenant_id),
        )


def update_app_ticket(tenant_id: str, app_ticket: str) -> None:
    """更新该租户滚动 appTicket（平台每 10 分钟推送，30 分钟有效）。"""
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE tenants SET app_ticket = ?, ticket_updated_at = ?, updated_at = ? WHERE id = ?",
            (app_ticket, now, now, tenant_id),
        )


def record_pending_auth_code(tenant_id: str, auth_code: str) -> None:
    """记录待处理的企业临时授权码（10 分钟有效），阶段 4 换取 certificate 时消费。"""
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE tenants SET pending_auth_code = ?, updated_at = ? WHERE id = ?",
            (auth_code, now, tenant_id),
        )


def save_token_bundle(
    tenant_id: str,
    *,
    access_token: str,
    refresh_token: str | None,
    token_expires_at: str | None,
    refresh_expires_at: str | None,
    org_id: str | None,
    user_id: str | None,
    scope: str | None,
    app_name: str | None,
) -> None:
    """落库 generateToken 返回的全部字段（access/refresh token 加密），并标记授权成功。

    到期时间由调用方（token_mgr）用 now + expiresIn 换算为绝对 ISO 时间后传入。
    """
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE tenants SET "
            "  access_token_enc = ?, refresh_token_enc = ?, "
            "  token_expires_at = ?, refresh_expires_at = ?, token_refreshed_at = ?, "
            "  org_id = ?, user_id = ?, scope = ?, app_name = ?, "
            "  auth_status = 'authorized', updated_at = ? "
            "WHERE id = ?",
            (
                encrypt_secret(access_token),
                encrypt_secret(refresh_token) if refresh_token else None,
                token_expires_at,
                refresh_expires_at,
                now,
                org_id,
                user_id,
                scope,
                app_name,
                now,
                tenant_id,
            ),
        )


def clear_token(tenant_id: str) -> None:
    """清除缓存 token（解除授权或 token 失效时用），授权态置回 pending。"""
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE tenants SET "
            "  access_token_enc = NULL, refresh_token_enc = NULL, "
            "  token_expires_at = NULL, refresh_expires_at = NULL, "
            "  auth_status = 'pending', updated_at = ? "
            "WHERE id = ?",
            (now, tenant_id),
        )


def credential_status(tenant: dict[str, Any]) -> dict[str, bool]:
    """返回各凭据是否已配置（供页面展示状态，不回显明文）。"""
    return {
        "app_key": bool(tenant.get("app_key")),
        "app_secret": bool(tenant.get("app_secret_enc")),
        "msg_secret": bool(tenant.get("msg_secret_enc")),
        "certificate": bool(tenant.get("certificate_enc")),
    }


# ─────────────────────────── MCP Key ───────────────────────────

def create_mcp_key(
    *,
    tenant_id: str,
    client_name: str,
    scopes: list[str] | None = None,
) -> dict[str, Any]:
    """生成一个 MCP Key。返回含 plain_key（仅此一次），其余只存哈希+前缀。"""
    from app.security import generate_mcp_key

    tenant = get_tenant(tenant_id)
    if tenant is None:
        raise ValueError("租户不存在")

    plain, prefix, key_hash = generate_mcp_key(tenant["client_code"])
    key_id = _new_id()
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO mcp_clients ("
            "  id, tenant_id, client_name, key_prefix, api_key_hash,"
            "  scopes_json, enabled, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                key_id, tenant_id, client_name, prefix, key_hash,
                json.dumps(scopes or []), now, now,
            ),
        )
    return {
        "id": key_id,
        "client_name": client_name,
        "key_prefix": prefix,
        "plain_key": plain,
        "scopes": scopes or [],
    }


def list_mcp_keys(tenant_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM mcp_clients WHERE tenant_id = ? ORDER BY created_at DESC",
            (tenant_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_active_client_by_key(api_key_hash: str) -> dict[str, Any] | None:
    """按 Key 哈希查有效的 mcp_client（enabled 且未吊销），联表带出租户。

    返回含 mcp_client 字段 + tenant_code / tenant_enabled；租户停用则返回 None。
    注意：SELECT 同时含 c.tenant_id，与 mcp_client 的 tenant_id 同值，不冲突。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT c.*, t.client_code AS tenant_code, t.enabled AS tenant_enabled "
            "FROM mcp_clients c JOIN tenants t ON c.tenant_id = t.id "
            "WHERE c.api_key_hash = ? AND c.enabled = 1 AND c.revoked_at IS NULL",
            (api_key_hash,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        if not d.get("tenant_enabled"):
            return None
        return d


def touch_mcp_key(key_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE mcp_clients SET last_used_at = ? WHERE id = ?", (_now(), key_id)
        )


def revoke_mcp_key(key_id: str) -> None:
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE mcp_clients SET revoked_at = ?, enabled = 0, updated_at = ? WHERE id = ?",
            (now, now, key_id),
        )


def set_mcp_key_enabled(key_id: str, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE mcp_clients SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, _now(), key_id),
        )


# ─────────────────────────── 调用日志（最小化，不记录 Key/Token/业务数据）───────────────────────────

def log_call(
    *,
    tenant_id: str | None,
    client_name: str | None,
    tool_name: str | None,
    status: str,
    error_code: str | None,
    duration_ms: int,
) -> None:
    """记录一条 MCP 工具调用日志。仅记录非敏感摘要（见计划 §十）。"""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO call_logs (id, tenant_id, client_name, tool_name, status, "
            "error_code, duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _new_id(), tenant_id, client_name, tool_name, status,
                error_code, duration_ms, _now(),
            ),
        )


def list_call_logs(tenant_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    with get_conn() as conn:
        if tenant_id:
            rows = conn.execute(
                "SELECT * FROM call_logs WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM call_logs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def update_trusted_domain(tenant_id: str, *, domain: str | None, check_content: str | None) -> None:
    """更新可信域名与验证文件内容。

    domain：传入即更新（空字符串→清空）。
    check_content：None 表示保持原内容不变（只改域名时不清空文件）；
                   传入字符串则覆盖（含空串→清空）。
    """
    now = _now()
    sets = ["trusted_domain = ?"]
    params: list[Any] = [(domain or "").strip().lower() or None]
    if check_content is not None:
        sets.append("trusted_check_content = ?")
        params.append(check_content)
    sets.append("updated_at = ?")
    params.append(now)
    params.append(tenant_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE tenants SET {', '.join(sets)} WHERE id = ?", params)


def get_tenant_by_domain(domain: str) -> dict[str, Any] | None:
    """按可信域名查租户（供平台拨测 /CHANJET_CHECK.txt 时定位）。"""
    d = (domain or "").strip().lower()
    if not d:
        return None
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tenants WHERE trusted_domain = ?", (d,)).fetchone()
        return dict(row) if row else None


def delete_tenant(tenant_id: str) -> None:
    """删除租户及其级联数据（mcp_clients/account_sets/call_logs）。"""
    with get_conn() as conn:
        conn.execute("DELETE FROM mcp_clients WHERE tenant_id = ?", (tenant_id,))
        conn.execute("DELETE FROM account_sets WHERE tenant_id = ?", (tenant_id,))
        conn.execute("DELETE FROM call_logs WHERE tenant_id = ?", (tenant_id,))
        conn.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))
