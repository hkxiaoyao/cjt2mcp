"""SQLite 访问层：连接管理 + 建表。

表结构对应计划 §三。所有租户级畅捷通凭据字段以 *_enc 结尾，落库前经
app.crypto.encrypt_field（AES-GCM）加密；此层不做加解密，只负责持久化。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from app.config import get_settings


SCHEMA = """
-- 租户（客户）= 一套独立自建应用 + 授权状态 + token 缓存
CREATE TABLE IF NOT EXISTS tenants (
    id                  TEXT PRIMARY KEY,
    client_code         TEXT NOT NULL UNIQUE,
    client_name         TEXT NOT NULL,
    contact             TEXT,
    phone               TEXT,
    remark              TEXT,
    enabled             INTEGER NOT NULL DEFAULT 1,
    -- 该租户自建应用凭据（敏感字段 AES-GCM 加密）
    app_key             TEXT,
    app_secret_enc      TEXT,
    msg_secret_enc      TEXT,
    certificate_enc     TEXT,
    app_ticket          TEXT,
    ticket_updated_at   TEXT,
    -- 授权与企业
    org_id              TEXT,
    user_id             TEXT,
    scope               TEXT,
    app_name            TEXT,
    auth_status         TEXT NOT NULL DEFAULT 'pending',
    -- token 缓存（AES-GCM 加密）
    access_token_enc    TEXT,
    refresh_token_enc   TEXT,
    token_expires_at    TEXT,
    refresh_expires_at  TEXT,
    token_refreshed_at  TEXT,
    -- 待处理的企业临时授权码（webhook 收到后暂存，阶段 4 换取 certificate；10 分钟有效）
    pending_auth_code       TEXT,
    pending_auth_code_at    TEXT,
    -- 默认账套
    default_account_key TEXT,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 账套（一个企业可有多账套）
CREATE TABLE IF NOT EXISTS account_sets (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    account_key TEXT NOT NULL,
    name        TEXT NOT NULL,
    alias       TEXT,
    is_default  INTEGER NOT NULL DEFAULT 0,
    enabled     INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id),
    UNIQUE(tenant_id, account_key)
);

-- MCP Key（一个租户可多 Key）
CREATE TABLE IF NOT EXISTS mcp_clients (
    id           TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    client_name  TEXT NOT NULL,
    key_prefix   TEXT NOT NULL,
    api_key_hash TEXT NOT NULL UNIQUE,
    scopes_json  TEXT NOT NULL DEFAULT '[]',
    enabled      INTEGER NOT NULL DEFAULT 1,
    expires_at   TEXT,
    revoked_at   TEXT,
    last_used_at TEXT,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);

-- 管理员
CREATE TABLE IF NOT EXISTS admin_users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name  TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    last_login_at TEXT,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 最小调用日志（不记录 Key/Token/业务数据/查询条件）
CREATE TABLE IF NOT EXISTS call_logs (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT,
    client_name TEXT,
    tool_name   TEXT,
    status      TEXT,
    error_code  TEXT,
    duration_ms INTEGER,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mcp_clients_tenant ON mcp_clients(tenant_id);
CREATE INDEX IF NOT EXISTS idx_account_sets_tenant ON account_sets(tenant_id);
CREATE INDEX IF NOT EXISTS idx_call_logs_tenant ON call_logs(tenant_id);
"""


def _db_path() -> str:
    return get_settings().db_path


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """获取一个自动提交/回滚的 SQLite 连接。行以 sqlite3.Row 返回。"""
    path = _db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# 增量列迁移：(表, 列, 列定义)。对旧库补列，幂等。
# CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，故新增列须在此登记。
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("tenants", "pending_auth_code", "TEXT"),
]


def _migrate_columns(conn: sqlite3.Connection) -> None:
    for table, column, coldef in _COLUMN_MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")


def init_db() -> None:
    """建表 + 增量列迁移（均幂等）。应在应用启动时调用。"""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate_columns(conn)
