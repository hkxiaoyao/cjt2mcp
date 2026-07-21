"""后台管理路由（Jinja2 + HTMX）。

阶段 2 覆盖：管理员登录/登出、客户列表、添加客户（含凭据录入）、客户详情。
后续阶段在此挂载 MCP Key、账套、工具权限、测试连接等。

页面请求要求登录：未登录访问受保护页面时重定向到登录页。
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import store
from app.chanjet import conntest
from app.config import get_settings
from app.mcpsrv import tools
from app.security import (
    current_admin,
    login_session,
    logout_session,
    verify_password,
)

router = APIRouter(prefix="/admin", tags=["admin"])

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# 客户编码规则：字母、数字、横线，全局唯一，创建后不改
_CODE_RE = re.compile(r"^[A-Za-z0-9-]+$")


def _require_login(request: Request) -> RedirectResponse | None:
    """页面级登录校验；未登录返回重定向响应，已登录返回 None。"""
    if current_admin(request) is None:
        return RedirectResponse(url="/admin/login", status_code=303)
    return None


# ─────────────────────────── 登录 / 登出 ───────────────────────────

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_admin(request) is not None:
        return RedirectResponse(url="/admin/tenants", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    admin = store.get_admin_by_username(username)
    if admin is None or not verify_password(password, admin["password_hash"]):
        return templates.TemplateResponse(
            request, "login.html", {"error": "用户名或密码错误"}, status_code=401
        )
    login_session(request, admin["id"], admin["username"])
    store.touch_admin_login(admin["id"])
    return RedirectResponse(url="/admin/tenants", status_code=303)


@router.get("/logout")
def logout(request: Request):
    logout_session(request)
    return RedirectResponse(url="/admin/login", status_code=303)


# ─────────────────────────── 客户列表 ───────────────────────────

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
@router.get("/tenants", response_class=HTMLResponse)
def tenant_list(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect

    tenants = store.list_tenants()
    total = len(tenants)
    normal = sum(1 for t in tenants if t["enabled"])
    pending = sum(1 for t in tenants if t["auth_status"] == "pending")
    disabled = sum(1 for t in tenants if not t["enabled"])
    return templates.TemplateResponse(
        request,
        "tenants.html",
        {
            "admin": current_admin(request),
            "tenants": tenants,
            "stats": {"total": total, "normal": normal, "pending": pending, "disabled": disabled},
        },
    )


# ─────────────────────────── 添加客户 ───────────────────────────

@router.get("/tenants/new", response_class=HTMLResponse)
def tenant_new_page(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request, "tenant_new.html", {"admin": current_admin(request), "error": None, "form": {}}
    )


@router.post("/tenants", response_class=HTMLResponse)
def tenant_create(
    request: Request,
    client_code: str = Form(...),
    client_name: str = Form(...),
    app_key: str = Form(""),
    app_secret: str = Form(""),
    msg_secret: str = Form(""),
    certificate: str = Form(""),
    contact: str = Form(""),
    phone: str = Form(""),
    remark: str = Form(""),
):
    redirect = _require_login(request)
    if redirect:
        return redirect

    form = {
        "client_code": client_code, "client_name": client_name,
        "app_key": app_key, "contact": contact, "phone": phone, "remark": remark,
    }

    def _err(msg: str):
        return templates.TemplateResponse(
            request, "tenant_new.html",
            {"admin": current_admin(request), "error": msg, "form": form},
            status_code=400,
        )

    code = client_code.strip()
    if not _CODE_RE.match(code):
        return _err("客户编码只能包含字母、数字、横线")
    if not client_name.strip():
        return _err("客户名称不能为空")
    if store.get_tenant_by_code(code):
        return _err(f"客户编码已存在：{code}")

    tenant_id = store.create_tenant(
        client_code=code,
        client_name=client_name.strip(),
        app_key=app_key.strip() or None,
        app_secret=app_secret.strip() or None,
        msg_secret=msg_secret.strip() or None,
        certificate=certificate.strip() or None,
        contact=contact.strip() or None,
        phone=phone.strip() or None,
        remark=remark.strip() or None,
    )
    return RedirectResponse(url=f"/admin/tenants/{tenant_id}", status_code=303)


# ─────────────────────────── 客户详情 ───────────────────────────

@router.get("/tenants/{tenant_id}", response_class=HTMLResponse)
def tenant_detail(request: Request, tenant_id: str):
    redirect = _require_login(request)
    if redirect:
        return redirect

    if store.get_tenant(tenant_id) is None:
        return HTMLResponse("客户不存在", status_code=404)

    return _render_detail(request, tenant_id)


def _compute_ticket_info(tenant: dict) -> dict:
    """计算 appTicket 新鲜度供页面展示。

    appTicket 有效期 30 分钟，平台每 10 分钟推送一次；据 ticket_updated_at
    判断是否新鲜（fresh/stale/none/error），并给出 appTicket 值与北京时间。
    """
    from datetime import datetime, timedelta, timezone

    raw = tenant.get("app_ticket")
    updated = tenant.get("ticket_updated_at")
    if not raw or not updated:
        return {"state": "none", "prefix": None, "beijing": None, "mins": None}
    try:
        dt = datetime.fromisoformat(updated)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        mins = int((datetime.now(timezone.utc) - dt).total_seconds() / 60)
        beijing = dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        state = "fresh" if mins <= 30 else "stale"
        return {"state": state, "prefix": raw, "beijing": beijing, "mins": mins}
    except (TypeError, ValueError):
        return {"state": "error", "prefix": raw, "beijing": None, "mins": None}


def _render_detail(request: Request, tenant_id: str, *, new_key=None, status_code: int = 200):
    """渲染详情页（生成 Key 后带一次性明文 new_key）。"""
    tenant = store.get_tenant(tenant_id)
    if tenant is None:
        return HTMLResponse("客户不存在", status_code=404)
    settings = get_settings()
    base = settings.public_base_url.rstrip("/")
    return templates.TemplateResponse(
        request,
        "tenant_detail.html",
        {
            "admin": current_admin(request),
            "tenant": tenant,
            "cred_status": store.credential_status(tenant),
            "ticket_info": _compute_ticket_info(tenant),
            "webhook_url": f"{base}/webhook/chanjet/{tenant['client_code']}",
            "mcp_url": f"{base}/chanjet/{tenant['client_code']}/mcp",
            "mcp_keys": store.list_mcp_keys(tenant_id),
            "scope_catalog": tools.SCOPE_CATALOG,
            "default_scopes": tools.DEFAULT_READONLY_SCOPES,
            "new_key": new_key,
        },
        status_code=status_code,
    )


# ─────────────────────────── MCP Key 管理 ───────────────────────────

@router.post("/tenants/{tenant_id}/mcp-keys", response_class=HTMLResponse)
async def mcp_key_create(request: Request, tenant_id: str):
    redirect = _require_login(request)
    if redirect:
        return redirect
    tenant = store.get_tenant(tenant_id)
    if tenant is None:
        return HTMLResponse("客户不存在", status_code=404)

    form = await request.form()
    client_name = (form.get("client_name") or "").strip()
    if not client_name:
        return _render_detail(request, tenant_id, status_code=400)
    # 勾选的 scope（多选）
    scopes = [s for s in form.getlist("scopes") if s in {c["scope"] for c in tools.SCOPE_CATALOG}]

    created = store.create_mcp_key(tenant_id=tenant_id, client_name=client_name, scopes=scopes)
    # created 含一次性明文 plain_key
    return _render_detail(request, tenant_id, new_key=created)


@router.post("/mcp-keys/{key_id}/revoke")
def mcp_key_revoke(request: Request, key_id: str, tenant_id: str = Form(...)):
    redirect = _require_login(request)
    if redirect:
        return redirect
    store.revoke_mcp_key(key_id)
    return RedirectResponse(url=f"/admin/tenants/{tenant_id}", status_code=303)


@router.post("/mcp-keys/{key_id}/enable")
def mcp_key_enable(request: Request, key_id: str, tenant_id: str = Form(...), enabled: str = Form("1")):
    redirect = _require_login(request)
    if redirect:
        return redirect
    store.set_mcp_key_enabled(key_id, enabled == "1")
    return RedirectResponse(url=f"/admin/tenants/{tenant_id}", status_code=303)


# ─────────────────────────── 凭据更新 ───────────────────────────

@router.post("/tenants/{tenant_id}/credentials")
def tenant_update_credentials(
    request: Request,
    tenant_id: str,
    app_key: str = Form(""),
    app_secret: str = Form(""),
    msg_secret: str = Form(""),
    certificate: str = Form(""),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    if store.get_tenant(tenant_id) is None:
        return HTMLResponse("客户不存在", status_code=404)
    # 仅更新非空字段（留空表示不改，避免误清空已配置的加密凭据）
    store.update_tenant_credentials(
        tenant_id,
        app_key=app_key.strip() if app_key.strip() else None,
        app_secret=app_secret.strip() if app_secret.strip() else None,
        msg_secret=msg_secret.strip() if msg_secret.strip() else None,
        certificate=certificate.strip() if certificate.strip() else None,
    )
    return RedirectResponse(url=f"/admin/tenants/{tenant_id}", status_code=303)


# ─────────────────────────── 连接测试 ───────────────────────────

@router.post("/tenants/{tenant_id}/chanjet/test")
def tenant_connection_test(request: Request, tenant_id: str):
    redirect = _require_login(request)
    if redirect:
        return redirect
    if store.get_tenant(tenant_id) is None:
        return JSONResponse({"detail": "客户不存在"}, status_code=404)
    result = conntest.run_connection_test(tenant_id)
    return JSONResponse(result)


# ─────────────────────────── MCP 配置生成 ───────────────────────────

@router.get("/tenants/{tenant_id}/mcp-config")
def tenant_mcp_config(request: Request, tenant_id: str, client_type: str = "workbuddy"):
    redirect = _require_login(request)
    if redirect:
        return redirect
    tenant = store.get_tenant(tenant_id)
    if tenant is None:
        return JSONResponse({"detail": "客户不存在"}, status_code=404)
    base = get_settings().public_base_url.rstrip("/")
    code = tenant["client_code"]
    # 日常页面 Key 位置为占位符（DB 只有哈希，无法还原明文）
    config = {
        "mcpServers": {
            f"tplus-{code.lower()}": {
                "type": "streamable-http",
                "url": f"{base}/chanjet/{code}/mcp",
                "headers": {"Authorization": "Bearer <请填入已保存的 MCP Key>"},
            }
        }
    }
    return JSONResponse(config)


# ─────────────────────────── 基本信息编辑 ───────────────────────────

@router.post("/tenants/{tenant_id}/basic")
def tenant_update_basic(
    request: Request,
    tenant_id: str,
    client_name: str = Form(...),
    contact: str = Form(""),
    phone: str = Form(""),
    remark: str = Form(""),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    if store.get_tenant(tenant_id) is None:
        return HTMLResponse("客户不存在", status_code=404)
    if not client_name.strip():
        return RedirectResponse(url=f"/admin/tenants/{tenant_id}", status_code=303)
    store.update_tenant_basic(
        tenant_id,
        client_name=client_name.strip(),
        contact=contact.strip(),
        phone=phone.strip(),
        remark=remark.strip(),
    )
    return RedirectResponse(url=f"/admin/tenants/{tenant_id}", status_code=303)


# ─────────────────────────── 可信域名 ───────────────────────────

@router.post("/tenants/{tenant_id}/trusted-domain")
async def tenant_update_trusted_domain(request: Request, tenant_id: str):
    redirect = _require_login(request)
    if redirect:
        return redirect
    if store.get_tenant(tenant_id) is None:
        return HTMLResponse("客户不存在", status_code=404)

    form = await request.form()
    domain = (form.get("trusted_domain") or "").strip()

    # 文件优先：上传了 CHANJET_CHECK.txt 就用文件内容；否则用文本框内容。
    # 两者都为空时 check_content 保持 None，store 侧不覆盖已存内容。
    check_content = None
    upload = form.get("check_file")
    if upload is not None and hasattr(upload, "read"):
        raw = await upload.read()
        if raw:
            check_content = raw.decode("utf-8", errors="replace").strip()
    if check_content is None:
        text = form.get("check_content")
        if text is not None and text.strip():
            check_content = text.strip()

    store.update_trusted_domain(tenant_id, domain=domain or None, check_content=check_content)
    return RedirectResponse(url=f"/admin/tenants/{tenant_id}", status_code=303)


# ─────────────────────────── 删除租户 ───────────────────────────

@router.post("/tenants/{tenant_id}/delete")
def tenant_delete(request: Request, tenant_id: str):
    redirect = _require_login(request)
    if redirect:
        return redirect
    store.delete_tenant(tenant_id)
    return RedirectResponse(url="/admin/tenants", status_code=303)
