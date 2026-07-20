"""畅捷通开放平台 HTTP 客户端。

封装两类调用：
1. generateToken —— 自建应用换取 accessToken（每租户用各自 appKey/appSecret + appTicket + certificate）。
2. 业务接口调用 —— T+ 产品线接口（阶段 5 使用），带公共参数。

所有调用返回畅捷通统一结构 {result, value, error}；本模块只负责 HTTP 与
结构解析，不做 token 缓存/落库（那是 token_mgr 的职责）。
"""

from __future__ import annotations

from typing import Any

import httpx

# 换 token 接口（用户提供的权威规范，见计划 §1.2）
GENERATE_TOKEN_URL = "https://openapi.chanjet.com/v1/common/auth/selfBuiltApp/generateToken"

# 业务接口基址（T+ 产品线；确切路径在阶段 5 逐一坐实）
OPENAPI_BASE = "https://openapi.chanjet.com"

_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


class ChanjetApiError(Exception):
    """畅捷通接口返回 result=false 或 HTTP 错误。"""

    def __init__(self, message: str, *, code: str | None = None, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.hint = hint


def _parse_envelope(data: dict[str, Any]) -> dict[str, Any]:
    """解析畅捷通统一响应 {result, value, error}，失败抛 ChanjetApiError。"""
    if data.get("result") is True:
        return data.get("value") or {}
    error = data.get("error") or {}
    raise ChanjetApiError(
        error.get("msg") or "畅捷通接口返回失败",
        code=str(error.get("code")) if error.get("code") is not None else None,
        hint=error.get("hint"),
    )


def generate_token(
    *, app_key: str, app_secret: str, app_ticket: str, certificate: str
) -> dict[str, Any]:
    """调用 generateToken 换取 accessToken 等。

    :return: 响应 value（含 accessToken/refreshToken/expiresIn/orgId 等）
    :raises ChanjetApiError: 接口返回失败
    :raises httpx.HTTPError: 网络或 HTTP 层错误
    """
    headers = {
        "appKey": app_key,
        "appSecret": app_secret,
        "Content-Type": "application/json",
    }
    body = {"appTicket": app_ticket, "certificate": certificate}
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(GENERATE_TOKEN_URL, json=body, headers=headers)
        resp.raise_for_status()
        return _parse_envelope(resp.json())


def call_business_api(
    *,
    path: str,
    app_key: str,
    app_secret: str,
    open_token: str,
    param: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    """调用 T+ 业务接口（见计划 §1.5）。

    认证放 Header：appKey + appSecret + openToken（openToken 即 accessToken）。
    业务参数放 Body 的 param 对象。响应可能是对象数组（如现存量）或
    {result,value,error} 信封，本函数按结构自适应返回：
      - dict 且含 result 键 → 解析信封返回 value；
      - 其余（数组或裸对象）→ 原样返回。

    :param path: 相对 OPENAPI_BASE 的接口路径，如 /tplus/api/v2/currentStock/Query
    :param param: 业务查询条件对象（字段首字母大写，见 §1.5）
    :raises ChanjetApiError: 接口返回失败
    :raises httpx.HTTPError: 网络或 HTTP 层错误
    """
    headers = {
        "appKey": app_key,
        "appSecret": app_secret,
        "openToken": open_token,
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    body = {"param": param or {}}
    url = path if path.startswith("http") else f"{OPENAPI_BASE}{path}"
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    # 信封结构（如 result=false 表示失败）才走 _parse_envelope；数组/裸对象原样返回
    if isinstance(data, dict) and "result" in data:
        return _parse_envelope(data)
    return data


# T+ 现存量查询接口路径（见计划 §1.5）
CURRENT_STOCK_PATH = "/tplus/api/v2/currentStock/Query"


def query_current_stock(
    *, app_key: str, app_secret: str, open_token: str, param: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """T+ 现存量查询（薄封装 call_business_api）。

    返回对象数组（每元素含 WarehouseCode/InventoryCode/AvailableQuantity/
    ExistingQuantity 等，见计划 §1.5）。异常信封由 call_business_api 统一处理。
    """
    data = call_business_api(
        path=CURRENT_STOCK_PATH,
        app_key=app_key,
        app_secret=app_secret,
        open_token=open_token,
        param=param,
    )
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("value"), list):
        return data["value"]
    return []


# ─────────────────────────── T+ 基础档案 / 单据查询 ───────────────────────────
#
# 关键：这些接口的 result 字段本身就是数据载荷（数组，或含 Data[] 的对象），
# 只有失败时 result 才是布尔 false 且带 error。故不能用 call_business_api 的
# {result,value,error} 信封解析（那会误判成功响应）。这里用专门的 business_query：
#   - result 为 False → 失败，抛 ChanjetApiError；
#   - 否则 result 即载荷（分页对象或数组），原样返回；
#   - 无 result 键 → 原样返回 data。

# 接口路径（见记忆 chanjet-tplus-endpoints）
INVENTORY_PATH = "/tplus/api/v2/inventory/Query"
PARTNER_PATH = "/tplus/api/v2/partner/Query"
WAREHOUSE_PATH = "/tplus/api/v2/warehouse/Query"
SALE_ORDER_PATH = "/tplus/api/v2/saleOrder/QueryPage"
PURCHASE_ORDER_PATH = "/tplus/api/v2/purchaseOrder/Query"


def business_query(
    *, path: str, app_key: str, app_secret: str, open_token: str, param: dict[str, Any] | None = None
) -> Any:
    """T+ 基础档案 / 单据查询：result 即载荷，result=False 才是失败。

    :return: result 载荷（分页对象 {TotalCount,Data[...]} 或对象数组）
    :raises ChanjetApiError: result 为 False（带 error），或 HTTP 错误
    """
    headers = {
        "appKey": app_key,
        "appSecret": app_secret,
        "openToken": open_token,
        "Content-Type": "application/json",
    }
    body = {"param": param or {}}
    url = path if path.startswith("http") else f"{OPENAPI_BASE}{path}"
    with httpx.Client(timeout=_TIMEOUT) as http:
        resp = http.post(url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    if isinstance(data, dict) and "result" in data:
        result = data["result"]
        if result is False:
            error = data.get("error") or {}
            raise ChanjetApiError(
                error.get("msg") or "T+ 查询失败",
                code=str(error.get("code")) if error.get("code") is not None else None,
                hint=error.get("hint"),
            )
        return result
    return data


def _as_rows(payload: Any) -> list[dict[str, Any]]:
    """把 business_query 的载荷统一成对象数组。

    - 分页对象 {Data:[...]} → 取 Data；
    - 数组 → 原样；
    - result 有时是 JSON 字符串（如仓库查询）→ 解析后取数组；
    - 其它 → 空数组。
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("Data", "data", "InventoryPriceList"):
            if isinstance(payload.get(key), list):
                return payload[key]
        return [payload]
    if isinstance(payload, str):
        try:
            import json as _json

            parsed = _json.loads(payload)
            return parsed if isinstance(parsed, list) else [parsed]
        except (ValueError, TypeError):
            return []
    return []


def query_inventory(
    *, app_key: str, app_secret: str, open_token: str, param: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """T+ 存货档案查询。"""
    return _as_rows(business_query(
        path=INVENTORY_PATH, app_key=app_key, app_secret=app_secret,
        open_token=open_token, param=param,
    ))


def query_partner(
    *, app_key: str, app_secret: str, open_token: str, param: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """T+ 往来单位查询（客户/供应商，用 param.PartnerType.Name 区分）。"""
    return _as_rows(business_query(
        path=PARTNER_PATH, app_key=app_key, app_secret=app_secret,
        open_token=open_token, param=param,
    ))


def query_warehouse(
    *, app_key: str, app_secret: str, open_token: str, param: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """T+ 仓库档案查询。"""
    return _as_rows(business_query(
        path=WAREHOUSE_PATH, app_key=app_key, app_secret=app_secret,
        open_token=open_token, param=param,
    ))


def query_sale_order(
    *, app_key: str, app_secret: str, open_token: str, param: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """T+ 销售订单分页查询（saleOrder/QueryPage）。"""
    return _as_rows(business_query(
        path=SALE_ORDER_PATH, app_key=app_key, app_secret=app_secret,
        open_token=open_token, param=param,
    ))


def query_purchase_order(
    *, app_key: str, app_secret: str, open_token: str, param: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """T+ 采购订单列表查询（purchaseOrder/Query）。"""
    return _as_rows(business_query(
        path=PURCHASE_ORDER_PATH, app_key=app_key, app_secret=app_secret,
        open_token=open_token, param=param,
    ))
