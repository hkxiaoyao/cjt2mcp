"""webhook 消息接收端点测试。

覆盖阶段 3 关键路径：按租户消息秘钥解密、msgType 分发、appTicket 落库、
企业授权码记录、未知租户 404、非法输入仍 ACK（避免平台重试风暴）。
"""

from __future__ import annotations

import json

import pytest

from app import store
from app.chanjet import webhook
from app.chanjet.webhook import (
    ACK_SUCCESS,
    UnknownTenantError,
    WebhookError,
    handle_message,
)
from app.crypto import encrypt_chanjet_message

MSG_SECRET = "fuxinqiche202607"  # 16 字节 = AES-128


def _make_tenant(code: str = "ZYSL", *, msg_secret: str | None = MSG_SECRET) -> str:
    return store.create_tenant(
        client_code=code,
        client_name="振原水泥有限公司",
        app_key="0aMlbJaE",
        msg_secret=msg_secret,
    )


def _envelope(payload: dict, secret: str = MSG_SECRET) -> dict:
    return {"encryptMsg": encrypt_chanjet_message(json.dumps(payload), secret)}


def test_app_test_verify(temp_db):
    _make_tenant()
    env = _envelope({"msgType": "APP_TEST", "appKey": "0aMlbJaE"})
    assert handle_message("ZYSL", env) == ACK_SUCCESS


def test_app_ticket_stored(temp_db):
    tenant_id = _make_tenant()
    env = _envelope({"msgType": "notice", "bizContent": {"appTicket": "t-abc123"}})
    assert handle_message("ZYSL", env) == ACK_SUCCESS

    tenant = store.get_tenant(tenant_id)
    assert tenant["app_ticket"] == "t-abc123"
    assert tenant["ticket_updated_at"] is not None


def test_auth_code_recorded(temp_db):
    tenant_id = _make_tenant()
    env = _envelope({"msgType": "notice", "bizContent": {"authCode": "ac-999"}})
    assert handle_message("ZYSL", env) == ACK_SUCCESS

    tenant = store.get_tenant(tenant_id)
    assert tenant["pending_auth_code"] == "ac-999"


def test_unknown_type_still_acks(temp_db):
    _make_tenant()
    env = _envelope({"msgType": "ORDER_PAID", "bizContent": {"orderId": "x"}})
    assert handle_message("ZYSL", env) == ACK_SUCCESS


def test_unknown_tenant_raises(temp_db):
    env = _envelope({"msgType": "APP_TEST"})
    with pytest.raises(UnknownTenantError):
        handle_message("NOPE", env)


def test_disabled_tenant_raises(temp_db):
    tenant_id = _make_tenant()
    store.set_tenant_enabled(tenant_id, False)
    env = _envelope({"msgType": "APP_TEST"})
    with pytest.raises(UnknownTenantError):
        handle_message("ZYSL", env)


def test_wrong_secret_fails_decrypt(temp_db):
    _make_tenant()
    # 用不同秘钥加密，服务端用租户秘钥解密应失败
    env = _envelope({"msgType": "APP_TEST"}, secret="0000000000000000")
    with pytest.raises(WebhookError):
        handle_message("ZYSL", env)


def test_missing_encrypt_msg(temp_db):
    _make_tenant()
    with pytest.raises(WebhookError):
        handle_message("ZYSL", {})


# ─── router 层行为（状态码） ───

def _client(temp_db):
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


def test_router_unknown_tenant_404(temp_db):
    with _client(temp_db) as client:
        env = _envelope({"msgType": "APP_TEST"})
        r = client.post("/webhook/chanjet/NOPE", json=env)
        assert r.status_code == 404


def test_router_bad_json_still_acks(temp_db):
    _make_tenant()
    with _client(temp_db) as client:
        r = client.post(
            "/webhook/chanjet/ZYSL",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 200
        assert r.json() == ACK_SUCCESS


def test_router_valid_app_test_200(temp_db):
    _make_tenant()
    with _client(temp_db) as client:
        env = _envelope({"msgType": "APP_TEST"})
        r = client.post("/webhook/chanjet/ZYSL", json=env)
        assert r.status_code == 200
        assert r.json() == ACK_SUCCESS
