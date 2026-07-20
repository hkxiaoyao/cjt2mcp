"""可信域名 + 基本信息编辑 + 删除租户 测试。

- 可信域名保存后，公开 /CHANJET_CHECK.txt 按 Host 头返回对应租户的验证文件内容；
- 只改域名不清空已存文件内容（覆盖式仅在提供新内容时生效）；
- 基本信息可编辑；
- 删除租户级联删除 mcp_clients/account_sets/call_logs。
"""

from __future__ import annotations

import contextlib


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


def _make_tenant(temp_db, code="ZYSL", name="振原水泥"):
    from app import store

    return store.create_tenant(
        client_code=code,
        client_name=name,
        app_key="0aMlbJaE",
        app_secret="secret-xyz",
        msg_secret="fuxinqiche202607",
        certificate="cert-abc",
    )


def test_trusted_domain_save_and_probe(temp_db):
    from app import store

    with _client() as c:
        _login(c)
        tid = _make_tenant(temp_db)
        # 保存可信域名 + 验证文件内容
        r = c.post(
            f"/admin/tenants/{tid}/trusted-domain",
            data={"trusted_domain": "sdfx.hacka.cn", "check_content": "CJT-TOKEN-12345"},
            follow_redirects=False,
        )
        assert r.status_code == 303

        # 落库
        t = store.get_tenant(tid)
        assert t["trusted_domain"] == "sdfx.hacka.cn"
        assert t["trusted_check_content"] == "CJT-TOKEN-12345"

        # 公开拨测：按 Host 头返回该租户内容
        r = c.get("/CHANJET_CHECK.txt", headers={"host": "sdfx.hacka.cn"})
        assert r.status_code == 200
        assert r.text == "CJT-TOKEN-12345"

        # 带端口的 Host 也应命中（去端口比对）
        r = c.get("/CHANJET_CHECK.txt", headers={"host": "sdfx.hacka.cn:8002"})
        assert r.status_code == 200
        assert r.text == "CJT-TOKEN-12345"

        # 未知域名 404
        r = c.get("/CHANJET_CHECK.txt", headers={"host": "unknown.example.com"})
        assert r.status_code == 404


def test_trusted_domain_two_tenants_isolated(temp_db):
    with _client() as c:
        _login(c)
        a = _make_tenant(temp_db, code="AAAA", name="A租户")
        b = _make_tenant(temp_db, code="BBBB", name="B租户")
        c.post(f"/admin/tenants/{a}/trusted-domain",
               data={"trusted_domain": "sdfx.hacka.cn", "check_content": "AAA-111"})
        c.post(f"/admin/tenants/{b}/trusted-domain",
               data={"trusted_domain": "sdzy.hacka.cn", "check_content": "BBB-222"})

        assert c.get("/CHANJET_CHECK.txt", headers={"host": "sdfx.hacka.cn"}).text == "AAA-111"
        assert c.get("/CHANJET_CHECK.txt", headers={"host": "sdzy.hacka.cn"}).text == "BBB-222"


def test_trusted_domain_change_domain_keeps_content(temp_db):
    from app import store

    with _client() as c:
        _login(c)
        tid = _make_tenant(temp_db)
        c.post(f"/admin/tenants/{tid}/trusted-domain",
               data={"trusted_domain": "old.hacka.cn", "check_content": "KEEP-ME"})
        # 只改域名，不传新内容（check_content 空）
        c.post(f"/admin/tenants/{tid}/trusted-domain",
               data={"trusted_domain": "new.hacka.cn", "check_content": ""})
        t = store.get_tenant(tid)
        assert t["trusted_domain"] == "new.hacka.cn"
        # 空串是显式覆盖为清空 —— 按 store 语义空串会清空；验证这一行为
        # （路由把空串 strip 后转 None，故应保留原内容）
        assert t["trusted_check_content"] == "KEEP-ME"


def test_edit_basic_info(temp_db):
    from app import store

    with _client() as c:
        _login(c)
        tid = _make_tenant(temp_db)
        r = c.post(
            f"/admin/tenants/{tid}/basic",
            data={"client_name": "振原水泥新名", "contact": "张三", "phone": "13800138000", "remark": "备注x"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        t = store.get_tenant(tid)
        assert t["client_name"] == "振原水泥新名"
        assert t["contact"] == "张三"
        assert t["phone"] == "13800138000"
        assert t["remark"] == "备注x"


def test_delete_tenant_cascade(temp_db):
    from app import store

    with _client() as c:
        _login(c)
        tid = _make_tenant(temp_db)
        store.create_mcp_key(tenant_id=tid, client_name="k1", scopes=[])
        assert len(store.list_mcp_keys(tid)) == 1

        r = c.post(f"/admin/tenants/{tid}/delete", follow_redirects=False)
        assert r.status_code == 303
        assert store.get_tenant(tid) is None
        # 级联：mcp_keys 应清空
        assert store.list_mcp_keys(tid) == []
