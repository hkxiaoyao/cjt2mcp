"""MCP 端点端到端测试。

覆盖：
- Bearer 鉴权：无 Key 401、错误 Key 401、跨租户 Key 拒绝；
- initialize 返回协议版本与 Mcp-Session-Id 头；
- notifications/initialized 返回 202 无 body；
- tools/list 按该 Key 的 scopes 动态裁剪；
- tools/call：现存量工具走 mock 打通；无权限工具拒绝；pending 工具返回明确错误。
"""

from __future__ import annotations

import json


def _make_tenant_with_key(temp_db, scopes):
    """建一个带完整凭据的租户 + 一个指定 scopes 的 MCP Key，返回 (client_code, plain_key, tenant_id)。"""
    from app import store

    tid = store.create_tenant(
        client_code="ZYSL",
        client_name="振原水泥",
        app_key="0aMlbJaE",
        app_secret="secret-xyz",
        msg_secret="fuxinqiche202607",
        certificate="cert-abc",
    )
    store.update_app_ticket(tid, "t-abc123")
    key = store.create_mcp_key(tenant_id=tid, client_name="WorkBuddy正式", scopes=scopes)
    return "ZYSL", key["plain_key"], tid


def _client():
    from starlette.testclient import TestClient
    from app.main import app

    return TestClient(app)


def _rpc(method, params=None, req_id=1):
    body = {"jsonrpc": "2.0", "method": method}
    if req_id is not None:
        body["id"] = req_id
    if params is not None:
        body["params"] = params
    return body


def test_auth_missing_key(temp_db):
    code, _, _ = _make_tenant_with_key(temp_db, ["stock:read"])
    with _client() as c:
        r = c.post(f"/chanjet/{code}/mcp", json=_rpc("initialize"))
        assert r.status_code == 401


def test_auth_wrong_key(temp_db):
    code, _, _ = _make_tenant_with_key(temp_db, ["stock:read"])
    with _client() as c:
        r = c.post(
            f"/chanjet/{code}/mcp",
            json=_rpc("initialize"),
            headers={"Authorization": "Bearer mcp_zysl_wrongkey"},
        )
        assert r.status_code == 401


def test_auth_cross_tenant_rejected(temp_db):
    """一个租户的 Key 不能访问另一个租户的端点。"""
    from app import store

    code, key, _ = _make_tenant_with_key(temp_db, ["stock:read"])
    # 另建一个租户
    other = store.create_tenant(client_code="MYJC", client_name="蚂蚁建材")
    with _client() as c:
        r = c.post(
            "/chanjet/MYJC/mcp",
            json=_rpc("initialize"),
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 401


def test_initialize(temp_db):
    code, key, _ = _make_tenant_with_key(temp_db, ["stock:read"])
    with _client() as c:
        r = c.post(
            f"/chanjet/{code}/mcp",
            json=_rpc("initialize"),
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["result"]["protocolVersion"]
        assert data["result"]["serverInfo"]["name"] == "cjt2mcp"
        assert r.headers.get("Mcp-Session-Id")


def test_notification_returns_202(temp_db):
    code, key, _ = _make_tenant_with_key(temp_db, ["stock:read"])
    with _client() as c:
        r = c.post(
            f"/chanjet/{code}/mcp",
            json=_rpc("notifications/initialized", req_id=None),
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 202
        assert r.content == b""


def test_tools_list_scope_filtering(temp_db):
    """只给 stock:read 时，仅现存量工具可见。"""
    code, key, _ = _make_tenant_with_key(temp_db, ["stock:read"])
    with _client() as c:
        r = c.post(
            f"/chanjet/{code}/mcp",
            json=_rpc("tools/list"),
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 200
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert names == {"query_current_stock"}


def test_tools_list_full_scopes(temp_db):
    """给全部只读 scope 时，6 个工具都可见。"""
    code, key, _ = _make_tenant_with_key(
        temp_db, ["stock:read", "archive:read", "sales:read", "purchase:read"]
    )
    with _client() as c:
        r = c.post(
            f"/chanjet/{code}/mcp",
            json=_rpc("tools/list"),
            headers={"Authorization": f"Bearer {key}"},
        )
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert len(names) == 6
        assert "query_current_stock" in names
        assert "query_purchase_order" in names


def test_tools_call_current_stock(temp_db, monkeypatch):
    """现存量工具打通：mock generateToken 与业务接口。"""
    from app.chanjet import client, token_mgr

    code, key, _ = _make_tenant_with_key(temp_db, ["stock:read"])

    # mock token 换取与业务查询
    monkeypatch.setattr(
        token_mgr, "ensure_access_token", lambda tid: "open-token-xyz"
    )
    fake_rows = [{"WarehouseCode": "001", "InventoryCode": "10001", "ExistingQuantity": "5.0"}]
    monkeypatch.setattr(client, "query_current_stock", lambda **kw: fake_rows)

    with _client() as c:
        r = c.post(
            f"/chanjet/{code}/mcp",
            json=_rpc("tools/call", {"name": "query_current_stock", "arguments": {"warehouse_code": "001"}}),
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 200
        result = r.json()["result"]
        assert result["isError"] is False
        payload = json.loads(result["content"][0]["text"])
        assert payload == fake_rows


def test_tools_call_no_permission(temp_db):
    """无对应 scope 时调用被拒绝。"""
    code, key, _ = _make_tenant_with_key(temp_db, ["stock:read"])
    with _client() as c:
        r = c.post(
            f"/chanjet/{code}/mcp",
            json=_rpc("tools/call", {"name": "query_sales_order", "arguments": {}}),
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 200
        assert "error" in r.json()


def test_tools_call_customer_query(temp_db, monkeypatch):
    """客户查询工具（往来单位按 PartnerType=客户 过滤）打通：mock token 与业务接口。"""
    from app.chanjet import client, token_mgr

    code, key, _ = _make_tenant_with_key(temp_db, ["archive:read"])
    monkeypatch.setattr(token_mgr, "ensure_access_token", lambda tid: "open-token-xyz")

    captured = {}

    def fake_partner(*, app_key, app_secret, open_token, partner_type=None, param=None):
        captured["partner_type"] = partner_type
        return [{"Code": "C001", "Name": "测试客户"}]

    monkeypatch.setattr(client, "query_partner", fake_partner)

    with _client() as c:
        r = c.post(
            f"/chanjet/{code}/mcp",
            json=_rpc("tools/call", {"name": "query_customer", "arguments": {"name": "测试"}}),
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 200
        result = r.json()["result"]
        assert result["isError"] is False
        payload = json.loads(result["content"][0]["text"])
        assert payload == [{"Code": "C001", "Name": "测试客户"}]
        # 关键：客户查询必须按 PartnerType=客户 过滤
        assert captured["partner_type"] == "客户"
