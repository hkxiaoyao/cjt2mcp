"""token_mgr 测试。

mock client.generate_token 避免真实网络。覆盖：
- 凭据缺失三态（appKey/appSecret、certificate、appTicket）各自的错误码；
- 有效 token 直接返回，不触发续取；
- token 缺失/过期时自动续取并把返回字段全部落库映射；
- 租户不存在/停用。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

# 权威响应示例（畅捷通文档），value 含全部落库字段
_TOKEN_VALUE = {
    "expiresIn": 518400,
    "appName": "accounting",
    "scope": "auth_all",
    "accessToken": "at-新令牌-eyJhbGci",
    "userId": "61000426758",
    "orgId": "90001203036",
    "refreshToken": "rt-7fe41dfbcc8446f9",
    "refreshExpiresIn": 2512799,
}


def _make_tenant(temp_db, **overrides):
    """建一个带完整凭据的租户，返回其 id。overrides 可覆盖列以模拟缺凭据。"""
    from app import store

    tid = store.create_tenant(
        client_code="ZYSL",
        client_name="振原水泥有限公司",
        app_key="0aMlbJaE",
        app_secret="secret-xyz",
        msg_secret="fuxinqiche202607",
        certificate="cert-abc",
    )
    # 默认给一个 appTicket（webhook 滚动写入的模拟）
    store.update_app_ticket(tid, "t-c93a53709d5b4df28cee22f3dab63fbd")
    return tid


def _save_value(tid, value):
    """把权威响应 value 按真实签名落库（测试预置用）。"""
    from datetime import datetime, timedelta, timezone
    from app import store

    now = datetime.now(timezone.utc)

    def _exp(sec):
        return (now + timedelta(seconds=int(sec))).isoformat()

    store.save_token_bundle(
        tid,
        access_token=value["accessToken"],
        refresh_token=value.get("refreshToken"),
        token_expires_at=_exp(value["expiresIn"]),
        refresh_expires_at=_exp(value["refreshExpiresIn"]),
        org_id=value.get("orgId"),
        user_id=value.get("userId"),
        scope=value.get("scope"),
        app_name=value.get("appName"),
    )


def test_missing_certificate(temp_db):
    from app import store
    from app.chanjet import token_mgr

    tid = store.create_tenant(
        client_code="MYJC", client_name="蚂蚁建材",
        app_key="k", app_secret="s", msg_secret="m",
        # 无 certificate
    )
    store.update_app_ticket(tid, "t-x")
    with pytest.raises(token_mgr.TokenError) as ei:
        token_mgr.ensure_access_token(tid)
    assert ei.value.code == "MISSING_CERTIFICATE"


def test_missing_app_ticket(temp_db):
    from app import store
    from app.chanjet import token_mgr

    tid = store.create_tenant(
        client_code="MYJC", client_name="蚂蚁建材",
        app_key="k", app_secret="s", msg_secret="m", certificate="c",
        # 未收到 appTicket
    )
    with pytest.raises(token_mgr.TokenError) as ei:
        token_mgr.ensure_access_token(tid)
    assert ei.value.code == "MISSING_APP_TICKET"


def test_missing_app_credentials(temp_db):
    from app import store
    from app.chanjet import token_mgr

    tid = store.create_tenant(
        client_code="MYJC", client_name="蚂蚁建材",
        # 无 app_key/app_secret
        msg_secret="m", certificate="c",
    )
    store.update_app_ticket(tid, "t-x")
    with pytest.raises(token_mgr.TokenError) as ei:
        token_mgr.ensure_access_token(tid)
    assert ei.value.code == "MISSING_APP_CREDENTIALS"


def test_tenant_not_found(temp_db):
    from app.chanjet import token_mgr

    with pytest.raises(token_mgr.TokenError) as ei:
        token_mgr.ensure_access_token("nonexistent")
    assert ei.value.code == "TENANT_NOT_FOUND"


def test_tenant_disabled(temp_db):
    from app import store
    from app.chanjet import token_mgr

    tid = _make_tenant(temp_db)
    store.set_tenant_enabled(tid, False)
    with pytest.raises(token_mgr.TokenError) as ei:
        token_mgr.ensure_access_token(tid)
    assert ei.value.code == "TENANT_DISABLED"


def test_refresh_and_persist_all_fields(temp_db, monkeypatch):
    """token 缺失时自动续取，并把返回字段全部落库映射。"""
    from app import store
    from app.chanjet import client, token_mgr
    from app.security import decrypt_secret

    tid = _make_tenant(temp_db)

    called = {}

    def fake_generate_token(*, app_key, app_secret, app_ticket, certificate):
        called["args"] = (app_key, app_secret, app_ticket, certificate)
        return dict(_TOKEN_VALUE)

    monkeypatch.setattr(client, "generate_token", fake_generate_token)

    token = token_mgr.ensure_access_token(tid)
    assert token == _TOKEN_VALUE["accessToken"]
    # 用租户明文凭据调用（appSecret/certificate 已解密）
    assert called["args"][0] == "0aMlbJaE"
    assert called["args"][1] == "secret-xyz"
    assert called["args"][3] == "cert-abc"

    # 落库字段全部映射
    t = store.get_tenant(tid)
    assert t["auth_status"] == "authorized"
    assert t["org_id"] == "90001203036"
    assert t["user_id"] == "61000426758"
    assert t["scope"] == "auth_all"
    assert t["app_name"] == "accounting"
    assert t["token_expires_at"] is not None
    assert t["refresh_expires_at"] is not None
    assert t["token_refreshed_at"] is not None
    # token 加密存储，非明文
    assert t["access_token_enc"] != _TOKEN_VALUE["accessToken"]
    assert decrypt_secret(t["access_token_enc"]) == _TOKEN_VALUE["accessToken"]
    assert decrypt_secret(t["refresh_token_enc"]) == _TOKEN_VALUE["refreshToken"]


def test_valid_token_no_refresh(temp_db, monkeypatch):
    """已有有效 token 时直接返回，不调 generateToken。"""
    from app import store
    from app.chanjet import client, token_mgr

    tid = _make_tenant(temp_db)
    # 先落一个有效 token
    _save_value(tid, _TOKEN_VALUE)

    def boom(**kwargs):
        raise AssertionError("不应触发续取")

    monkeypatch.setattr(client, "generate_token", boom)
    token = token_mgr.ensure_access_token(tid)
    assert token == _TOKEN_VALUE["accessToken"]


def test_expired_token_triggers_refresh(temp_db, monkeypatch):
    """token 已过期（含提前量）时触发续取。"""
    from app import store
    from app.chanjet import client, token_mgr
    from app.db import get_conn

    tid = _make_tenant(temp_db)
    _save_value(tid, _TOKEN_VALUE)
    # 手动把到期时间改到过去
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE tenants SET token_expires_at = ? WHERE id = ?", (past, tid))

    new_value = dict(_TOKEN_VALUE, accessToken="at-续取后的新令牌")
    monkeypatch.setattr(client, "generate_token", lambda **kw: new_value)

    token = token_mgr.ensure_access_token(tid)
    assert token == "at-续取后的新令牌"
