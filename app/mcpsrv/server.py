"""MCP streamable-http 端点（每租户独立）+ Bearer 鉴权。

路由：POST /chanjet/{client_code}/mcp
鉴权：Authorization: Bearer <MCP Key> → SHA-256 哈希比对 mcp_clients。

采用手写精简 JSON-RPC 处理器（而非官方 FastMCP），因为需要：
- 每租户独立 URL + 动态工具裁剪（按该 Key 的 scopes）；
- 与既有 Bearer/SQLite 鉴权体系统一。

按 MCP streamable-http 规范（见计划 §5 调研）：
- 请求为单条 JSON-RPC，服务端对 request 返回 application/json 单对象；
- notification（无 id，如 notifications/initialized）返回 202 无 body；
- initialize 响应带 Mcp-Session-Id 头。

不实现 SSE 流（规范允许纯 application/json 响应），保持简单。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app import store
from app.mcpsrv import tools
from app.security import hash_api_key

router = APIRouter(tags=["mcp"])

# 协议版本：跟随规范默认协商版本
PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "cjt2mcp", "version": "0.1.0"}


# ─────────────────────────── JSON-RPC 辅助 ───────────────────────────

def _rpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


# JSON-RPC 标准错误码
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603


def _extract_bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _authenticate(request: Request, client_code: str) -> dict[str, Any] | None:
    """校验 Bearer Key，且该 Key 必须属于 URL 中的 client_code 租户。

    返回 mcp_client 记录（含 tenant_id/tenant_code/scopes_json）；失败返回 None。
    """
    key = _extract_bearer(request)
    if not key:
        return None
    record = store.get_active_client_by_key(hash_api_key(key))
    if record is None:
        return None
    # Key 必须匹配 URL 的租户，防止跨租户使用
    if record.get("tenant_code") != client_code:
        return None
    return record


def _scopes_of(record: dict[str, Any]) -> set[str]:
    import json

    raw = record.get("scopes_json")
    if not raw:
        return set()
    try:
        data = json.loads(raw)
        return set(data) if isinstance(data, list) else set()
    except (ValueError, TypeError):
        return set()


# ─────────────────────────── 方法分发 ───────────────────────────

def _handle_initialize(req_id: Any) -> dict[str, Any]:
    return _rpc_result(
        req_id,
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        },
    )


def _handle_tools_list(req_id: Any, scopes: set[str]) -> dict[str, Any]:
    visible = tools.visible_tools(scopes)
    return _rpc_result(req_id, {"tools": [t.definition() for t in visible]})


def _handle_tools_call(
    req_id: Any, params: dict[str, Any], scopes: set[str], tenant: dict[str, Any]
) -> dict[str, Any]:
    name = params.get("name")
    args = params.get("arguments") or {}
    tool = tools.get_tool(name)
    if tool is None:
        return _rpc_error(req_id, _METHOD_NOT_FOUND, f"未知工具：{name}")
    if tool.scope not in scopes:
        return _rpc_error(req_id, _INVALID_REQUEST, f"无权限调用工具：{name}")

    started = time.monotonic()
    status = "success"
    error_code: str | None = None
    try:
        data = tool.handler(tenant, args)
        # MCP tools/call 结果：content 为文本块，附结构化数据
        import json as _json

        text = _json.dumps(data, ensure_ascii=False, default=str)
        return _rpc_result(
            req_id,
            {"content": [{"type": "text", "text": text}], "isError": False},
        )
    except tools.ToolError as exc:
        status = "error"
        error_code = exc.code
        return _rpc_result(
            req_id,
            {"content": [{"type": "text", "text": f"[{exc.code}] {exc}"}], "isError": True},
        )
    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
        store.log_call(
            tenant_id=tenant["id"],
            client_name=tenant.get("_client_name"),
            tool_name=name,
            status=status,
            error_code=error_code,
            duration_ms=duration_ms,
        )


@router.post("/chanjet/{client_code}/mcp")
async def mcp_endpoint(client_code: str, request: Request) -> Response:
    # 鉴权
    record = _authenticate(request, client_code)
    if record is None:
        return JSONResponse(
            _rpc_error(None, _INVALID_REQUEST, "未授权：无效或缺失的 MCP Key"),
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 解析请求体
    try:
        message = await request.json()
    except Exception:
        return JSONResponse(_rpc_error(None, _PARSE_ERROR, "请求体非法 JSON"), status_code=400)

    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return JSONResponse(_rpc_error(None, _INVALID_REQUEST, "非法 JSON-RPC 请求"), status_code=400)

    method = message.get("method")
    req_id = message.get("id")
    params = message.get("params") or {}

    # notification（无 id）：如 notifications/initialized → 202 无 body
    if req_id is None and isinstance(method, str) and method.startswith("notifications/"):
        return Response(status_code=202)

    store.touch_mcp_key(record["id"])
    scopes = _scopes_of(record)

    # 组装 tenant 上下文（附 client_name 供日志）
    tenant = store.get_tenant(record["tenant_id"])
    if tenant is None:
        return JSONResponse(_rpc_error(req_id, _INTERNAL_ERROR, "租户不存在"), status_code=500)
    tenant = dict(tenant)
    tenant["_client_name"] = record.get("client_name")

    # 分发
    if method == "initialize":
        resp = _handle_initialize(req_id)
        return JSONResponse(resp, headers={"Mcp-Session-Id": uuid.uuid4().hex})
    if method == "tools/list":
        return JSONResponse(_handle_tools_list(req_id, scopes))
    if method == "tools/call":
        return JSONResponse(_handle_tools_call(req_id, params, scopes, tenant))
    if method == "ping":
        return JSONResponse(_rpc_result(req_id, {}))

    return JSONResponse(_rpc_error(req_id, _METHOD_NOT_FOUND, f"未知方法：{method}"))
