"""阶段 6 后台补齐测试：MCP Key 生成（含 scope 勾选）、一次性明文回显、
吊销、凭据更新、连接测试端点。用 TestClient 走真实登录 Session。
"""

from __future__ import annotations

import contextlib

import pytest


@contextlib.contextmanager
def _client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c


def _login(c):
    r = c.post(
        "/admin/login",
        data={"username": "admin", "password": "test-pw"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def _make_tenant(temp_db):
    from app import store

    return store.create_tenant(
        client_code="ZYSL",
        client_name="振原水泥",
        app_key="0aMlbJaE",
        app_secret="secret-xyz",
        msg_secret="fuxinqiche202607",
        certificate="cert-abc",
    )


def test_create_mcp_key_shows_plaintext_once(temp_db):
    tid = _make_tenant(temp_db)
    with _client() as c:
        _login(c)
        r = c.post(
            f"/admin/tenants/{tid}/mcp-keys",
            data={"client_name": "WorkBuddy正式", "scopes": ["stock:read", "archive:read"]},
        )
        assert r.status_code == 200
        # 一次性明文 Key 出现在页面
        assert "mcp_zysl_" in r.text
        assert "仅显示这一次" in r.text


def test_created_key_authenticates_mcp(temp_db):
    """后台生成的 Key 能真正通过 MCP 端点鉴权，且 scope 生效。"""
    from app import store

    tid = _make_tenant(temp_db)
    created = store.create_mcp_key(
        tenant_id=tid, client_name="t", scopes=["stock:read"]
    )
    plain = created["plain_key"]

    with _client() as c:
        r = c.post(
            "/chanjet/ZYSL/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": f"Bearer {plain}"},
        )
        assert r.status_code == 200
        names = {t["name"] for t in r.json()["result"]["tools"]}
        # 只勾了 stock:read → 只可见 query_current_stock
        assert names == {"query_current_stock"}


def test_revoke_key_disables_auth(temp_db):
    from app import store

    tid = _make_tenant(temp_db)
    created = store.create_mcp_key(tenant_id=tid, client_name="t", scopes=["stock:read"])
    plain = created["plain_key"]

    with _client() as c:
        _login(c)
        r = c.post(
            f"/admin/mcp-keys/{created['id']}/revoke",
            data={"tenant_id": tid},
            follow_redirects=False,
        )
        assert r.status_code == 303

        # 吊销后 Key 无法再鉴权
        r2 = c.post(
            "/chanjet/ZYSL/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": f"Bearer {plain}"},
        )
        assert r2.status_code == 401


def test_update_credentials_only_nonempty(temp_db):
    """更新凭据：留空字段不覆盖已有加密值。"""
    from app import store
    from app.security import decrypt_secret

    tid = _make_tenant(temp_db)
    with _client() as c:
        _login(c)
        # 只改 certificate，其余留空
        r = c.post(
            f"/admin/tenants/{tid}/credentials",
            data={"app_key": "", "app_secret": "", "msg_secret": "", "certificate": "new-cert"},
            follow_redirects=False,
        )
        assert r.status_code == 303

    t = store.get_tenant(tid)
    # certificate 更新
    assert decrypt_secret(t["certificate_enc"]) == "new-cert"
    # app_secret 未被清空
    assert decrypt_secret(t["app_secret_enc"]) == "secret-xyz"
    # app_key 未被清空
    assert t["app_key"] == "0aMlbJaE"


def test_connection_test_reports_missing_app_ticket(temp_db):
    """连接测试：凭据齐全但没有 appTicket 时，明确指出该步失败。"""
    tid = _make_tenant(temp_db)
    with _client() as c:
        _login(c)
        r = c.post(f"/admin/tenants/{tid}/chanjet/test")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        # 前两步（身份、凭据）应通过，appTicket 步失败
        steps = {s["name"]: s for s in data["steps"]}
        assert steps["凭据配置"]["ok"] is True
        assert steps["appTicket"]["ok"] is False
        assert steps["appTicket"]["code"] == "MISSING_APP_TICKET"


def test_connection_test_requires_login(temp_db):
    tid = _make_tenant(temp_db)
    with _client() as c:
        r = c.post(f"/admin/tenants/{tid}/chanjet/test", follow_redirects=False)
        # 未登录被重定向到登录页
        assert r.status_code == 303
