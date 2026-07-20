"""MCP 工具定义、权限裁剪与执行。

首版只读查询类工具（计划 §五）。每个工具声明：
- name / description / inputSchema（暴露给 MCP 客户端）；
- scope：租户 scopes_json 里需包含该 scope 才对该租户可见/可调用；
- handler：执行逻辑（经 token_mgr 拿 openToken 再调 client）。

现存量工具已按坐实的 T+ 规范实装；其余 5 个业务接口的确切规范尚待联调坐实
（计划 §五 "待坐实"），先注册为已定义工具，调用时返回明确的 PENDING 说明，
使 tools/list 能按权限展示，同时不谎报未验证的能力。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from app import store
from app.chanjet import client, token_mgr


class ToolError(Exception):
    """工具执行失败，code 用于日志与错误反馈。"""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    scope: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any], dict[str, Any]], Any]

    def definition(self) -> dict[str, Any]:
        """tools/list 暴露的结构。"""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


# ─────────────────────────── 工具 handler ───────────────────────────

_PAGINATION_SCHEMA = {
    "page_index": {"type": "integer", "description": "页码，从 1 开始", "minimum": 1},
    "page_size": {"type": "integer", "description": "每页条数", "minimum": 1},
}


def _handle_current_stock(tenant: dict[str, Any], args: dict[str, Any]) -> list[dict[str, Any]]:
    """查询现存量。经 token_mgr 拿 openToken，构造 T+ param 后调 client。"""
    open_token = token_mgr.ensure_access_token(tenant["id"])
    app_secret = store_decrypt(tenant, "app_secret_enc")

    param: dict[str, Any] = {}
    if args.get("warehouse_code"):
        param["Warehouse"] = [{"Code": str(args["warehouse_code"])}]
    if args.get("inventory_code"):
        param["Inventory"] = [{"Code": str(args["inventory_code"])}]
    if args.get("inventory_name"):
        param["InventoryName"] = str(args["inventory_name"])
    param["PageIndex"] = int(args.get("page_index", 1))
    param["PageSize"] = int(args.get("page_size", 100))

    try:
        return client.query_current_stock(
            app_key=tenant["app_key"],
            app_secret=app_secret,
            open_token=open_token,
            param=param,
        )
    except client.ChanjetApiError as exc:
        raise ToolError(f"现存量查询失败：{exc}", code=exc.code or "CHANJET_QUERY_FAILED") from exc
    except Exception as exc:  # 网络层
        raise ToolError(f"现存量查询网络异常：{exc}", code="CHANJET_NETWORK_ERROR") from exc


def _open_ctx(tenant: dict[str, Any]) -> tuple[str, str]:
    """公共前置：拿有效 openToken + 解密 appSecret。返回 (open_token, app_secret)。"""
    open_token = token_mgr.ensure_access_token(tenant["id"])
    app_secret = store_decrypt(tenant, "app_secret_enc")
    return open_token, app_secret


def _base_param(args: dict[str, Any]) -> dict[str, Any]:
    """构造含分页与关键字过滤的 T+ param 基底（字段首字母大写）。"""
    param: dict[str, Any] = {}
    if args.get("code"):
        param["Code"] = str(args["code"])
    if args.get("name"):
        param["Name"] = str(args["name"])
    param["PageIndex"] = int(args.get("page_index", 1))
    param["PageSize"] = int(args.get("page_size", 100))
    return param


def _run_query(desc: str, fn: Callable[[], Any]) -> Any:
    """统一执行业务查询并把畅捷通/网络异常转成 ToolError。"""
    try:
        return fn()
    except client.ChanjetApiError as exc:
        raise ToolError(f"{desc}失败：{exc}", code=exc.code or "CHANJET_QUERY_FAILED") from exc
    except Exception as exc:  # 网络层
        raise ToolError(f"{desc}网络异常：{exc}", code="CHANJET_NETWORK_ERROR") from exc


def _handle_inventory(tenant: dict[str, Any], args: dict[str, Any]) -> list[dict[str, Any]]:
    """查询存货档案。"""
    open_token, app_secret = _open_ctx(tenant)
    return _run_query("存货查询", lambda: client.query_inventory(
        app_key=tenant["app_key"], app_secret=app_secret,
        open_token=open_token, param=_base_param(args),
    ))


def _handle_customer(tenant: dict[str, Any], args: dict[str, Any]) -> list[dict[str, Any]]:
    """查询客户档案（往来单位，按 PartnerType='客户' 过滤）。"""
    open_token, app_secret = _open_ctx(tenant)
    return _run_query("客户查询", lambda: client.query_partner(
        app_key=tenant["app_key"], app_secret=app_secret,
        open_token=open_token, partner_type="客户", param=_base_param(args),
    ))


def _handle_warehouse(tenant: dict[str, Any], args: dict[str, Any]) -> list[dict[str, Any]]:
    """查询仓库档案。"""
    open_token, app_secret = _open_ctx(tenant)
    return _run_query("仓库查询", lambda: client.query_warehouse(
        app_key=tenant["app_key"], app_secret=app_secret,
        open_token=open_token, param=_base_param(args),
    ))


def _handle_sales_order(tenant: dict[str, Any], args: dict[str, Any]) -> list[dict[str, Any]]:
    """查询销售订单。支持按单据编号、往来单位编码过滤。"""
    open_token, app_secret = _open_ctx(tenant)
    param = _base_param(args)
    if args.get("partner_code"):
        param["Partner"] = {"Code": str(args["partner_code"])}
    return _run_query("销售订单查询", lambda: client.query_sale_order(
        app_key=tenant["app_key"], app_secret=app_secret,
        open_token=open_token, param=param,
    ))


def _handle_purchase_order(tenant: dict[str, Any], args: dict[str, Any]) -> list[dict[str, Any]]:
    """查询采购订单。支持按供应商编码过滤。"""
    open_token, app_secret = _open_ctx(tenant)
    param = _base_param(args)
    if args.get("vendor_code"):
        param["Vendor"] = {"Code": str(args["vendor_code"])}
    return _run_query("采购订单查询", lambda: client.query_purchase_order(
        app_key=tenant["app_key"], app_secret=app_secret,
        open_token=open_token, param=param,
    ))


def store_decrypt(tenant: dict[str, Any], col: str) -> str:
    """解密租户某个 *_enc 字段，缺失抛 ToolError。"""
    from app.security import decrypt_secret

    val = tenant.get(col)
    if not val:
        raise ToolError(f"租户未配置 {col}", code="MISSING_CREDENTIAL")
    return decrypt_secret(val)


# ─────────────────────────── 工具注册表 ───────────────────────────

ALL_TOOLS: list[Tool] = [
    Tool(
        name="query_current_stock",
        description="查询 T+ 现存量（库存数量）。可按仓库编码、存货编码或存货名称筛选。",
        scope="stock:read",
        input_schema={
            "type": "object",
            "properties": {
                "warehouse_code": {"type": "string", "description": "仓库编码，可选"},
                "inventory_code": {"type": "string", "description": "存货编码，可选"},
                "inventory_name": {"type": "string", "description": "存货名称模糊匹配，可选"},
                **_PAGINATION_SCHEMA,
            },
        },
        handler=_handle_current_stock,
    ),
    Tool(
        name="query_inventory",
        description="查询 T+ 存货档案。可按存货编码、名称、规格筛选。",
        scope="archive:read",
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "存货编码，可选"},
                "name": {"type": "string", "description": "存货名称，可选"},
                **_PAGINATION_SCHEMA,
            },
        },
        handler=_handle_inventory,
    ),
    Tool(
        name="query_customer",
        description="查询 T+ 客户档案（往来单位中类型为客户的记录）。可按编码、名称筛选。",
        scope="archive:read",
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "客户编码，可选"},
                "name": {"type": "string", "description": "客户名称，可选"},
                **_PAGINATION_SCHEMA,
            },
        },
        handler=_handle_customer,
    ),
    Tool(
        name="query_warehouse",
        description="查询 T+ 仓库档案。可按仓库编码、名称筛选。",
        scope="archive:read",
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "仓库编码，可选"},
                "name": {"type": "string", "description": "仓库名称，可选"},
                **_PAGINATION_SCHEMA,
            },
        },
        handler=_handle_warehouse,
    ),
    Tool(
        name="query_sales_order",
        description="查询 T+ 销售订单。可按单据编号、往来单位编码筛选。",
        scope="sales:read",
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "销售订单编号，可选"},
                "partner_code": {"type": "string", "description": "往来单位编码，可选"},
                **_PAGINATION_SCHEMA,
            },
        },
        handler=_handle_sales_order,
    ),
    Tool(
        name="query_purchase_order",
        description="查询 T+ 采购订单。可按单据编号、供应商编码筛选。",
        scope="purchase:read",
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "采购订单编号，可选"},
                "vendor_code": {"type": "string", "description": "供应商编码，可选"},
                **_PAGINATION_SCHEMA,
            },
        },
        handler=_handle_purchase_order,
    ),
]

_TOOLS_BY_NAME = {t.name: t for t in ALL_TOOLS}

# 快捷权限集（后台工具权限页用；计划 §五）
DEFAULT_READONLY_SCOPES = ["stock:read", "archive:read", "sales:read", "purchase:read"]

# scope 目录：供后台建 Key 时勾选权限。label 中文，tools 为该 scope 下的工具名。
SCOPE_CATALOG: list[dict[str, Any]] = [
    {"scope": "stock:read", "label": "库存查询", "tools": ["query_current_stock"]},
    {
        "scope": "archive:read",
        "label": "基础档案查询",
        "tools": ["query_inventory", "query_customer", "query_warehouse"],
    },
    {"scope": "sales:read", "label": "销售查询", "tools": ["query_sales_order"]},
    {"scope": "purchase:read", "label": "采购查询", "tools": ["query_purchase_order"]},
]

_VALID_SCOPES = {s["scope"] for s in SCOPE_CATALOG}


def sanitize_scopes(scopes: list[str]) -> list[str]:
    """过滤出合法 scope，保持 SCOPE_CATALOG 顺序，去重。"""
    chosen = set(scopes)
    return [s["scope"] for s in SCOPE_CATALOG if s["scope"] in chosen]


def tenant_scopes(tenant: dict[str, Any]) -> set[str]:
    """解析租户 scopes_json；解析失败视为空集合（无权限）。"""
    raw = tenant.get("scopes_json")
    if not raw:
        return set()
    try:
        data = json.loads(raw)
        return set(data) if isinstance(data, list) else set()
    except (ValueError, TypeError):
        return set()


def visible_tools(scopes: set[str]) -> list[Tool]:
    """按 scope 裁剪出对该租户可见的工具。"""
    return [t for t in ALL_TOOLS if t.scope in scopes]


def get_tool(name: str) -> Tool | None:
    return _TOOLS_BY_NAME.get(name)
