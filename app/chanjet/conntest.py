"""连接测试：串联"凭据→token→轻量业务接口"，每步返回明确结果。

供后台"测试连接"页调用。失败时给出可读原因与错误码（见计划 §九），
不返回业务数据本身，只验证链路是否通。
"""

from __future__ import annotations

from typing import Any

from app import store
from app.chanjet import client, token_mgr


def _step(name: str, ok: bool, detail: str = "", code: str | None = None) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail, "code": code}


def run_connection_test(tenant_id: str) -> dict[str, Any]:
    """执行连接测试，返回 {ok, steps:[...]}。

    步骤：
      1. 租户存在且启用；
      2. 凭据齐全（appKey/appSecret/certificate）；
      3. 已收到 appTicket（webhook 推送）；
      4. 换取 accessToken（token_mgr，必要时实调 generateToken）；
      5. 轻量业务接口（现存量查询 1 条）连通。
    任一步失败即停止，返回已完成步骤。
    """
    steps: list[dict[str, Any]] = []

    tenant = store.get_tenant(tenant_id)
    if tenant is None:
        steps.append(_step("租户身份", False, "租户不存在", "TENANT_NOT_FOUND"))
        return {"ok": False, "steps": steps}
    if not tenant.get("enabled"):
        steps.append(_step("租户身份", False, "租户已停用", "TENANT_DISABLED"))
        return {"ok": False, "steps": steps}
    steps.append(_step("租户身份", True, tenant["client_name"]))

    # 凭据齐全
    missing = []
    if not tenant.get("app_key") or not tenant.get("app_secret_enc"):
        missing.append("appKey/appSecret")
    if not tenant.get("certificate_enc"):
        missing.append("certificate")
    if missing:
        steps.append(_step("凭据配置", False, f"缺少：{'、'.join(missing)}", "MISSING_CREDENTIAL"))
        return {"ok": False, "steps": steps}
    steps.append(_step("凭据配置", True, "appKey/appSecret/certificate 已配置"))

    # appTicket
    if not tenant.get("app_ticket"):
        steps.append(_step(
            "appTicket", False,
            "尚未收到 appTicket（确认自建应用已配置消息接收地址并等待平台推送）",
            "MISSING_APP_TICKET",
        ))
        return {"ok": False, "steps": steps}
    steps.append(_step("appTicket", True, "已收到"))

    # 换取 token
    try:
        open_token = token_mgr.ensure_access_token(tenant_id)
    except token_mgr.TokenError as exc:
        steps.append(_step("换取 accessToken", False, str(exc), exc.code))
        return {"ok": False, "steps": steps}
    steps.append(_step("换取 accessToken", True, "accessToken 有效"))

    # 轻量业务接口
    try:
        rows = client.query_current_stock(
            app_key=tenant["app_key"],
            app_secret=_decrypt(tenant, "app_secret_enc"),
            open_token=open_token,
            param={"PageIndex": 1, "PageSize": 1},
        )
        steps.append(_step("业务接口连通", True, f"现存量查询正常（返回 {len(rows)} 条）"))
    except client.ChanjetApiError as exc:
        steps.append(_step("业务接口连通", False, str(exc), exc.code or "CHANJET_QUERY_FAILED"))
        return {"ok": False, "steps": steps}
    except Exception as exc:  # 网络层
        steps.append(_step("业务接口连通", False, f"网络异常：{exc}", "CHANJET_NETWORK_ERROR"))
        return {"ok": False, "steps": steps}

    return {"ok": True, "steps": steps}


def _decrypt(tenant: dict[str, Any], col: str) -> str:
    from app.security import decrypt_secret

    return decrypt_secret(tenant[col])
