"""畅捷通消息接收 webhook 处理。

平台把消息推送到每租户独立地址 /webhook/chanjet/{client_code}。
外层信封仅含 encryptMsg（Base64 AES 密文），需先按 URL 中的 client_code
取该租户的消息秘钥解密，才能读到内部 msgType 与业务字段——租户身份靠 URL 而非密文。

处理原则（防御式）：
- 消息秘钥/租户缺失、解密失败 → 明确失败，不进入业务分发；
- msgType 的确切字符串在开放平台文档中不完整（见计划 §1.6），故除已确认的
  验证消息（APP_TEST）外，用字段特征兜底识别 appTicket 与企业授权码；
- 未知 msgType 一律正常 ACK，避免平台判为推送失败而触发重试风暴；
- 无论何种业务处理结果，只要消息合法解密即返回 {"result":"success"}。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import store
from app.crypto import decrypt_chanjet_message
from app.security import decrypt_secret

logger = logging.getLogger(__name__)

# 平台要求的成功应答（1 秒内返回）
ACK_SUCCESS: dict[str, str] = {"result": "success"}

# 已确认的验证消息类型（首次配置消息接收地址时下发）
_MSG_TYPE_APP_TEST = "APP_TEST"


class WebhookError(Exception):
    """消息无法合法处理（未配置秘钥、密文缺失、解密失败）。"""


class UnknownTenantError(WebhookError):
    """租户不存在或已停用——应以 404 拒绝，而非 ACK。"""


def _extract_app_ticket(biz: Any) -> str | None:
    """从解密后的消息中兜底提取 appTicket。

    appTicket 形如 't-xxxxxxxx'。bizContent 结构随消息类型变化，文档未固定，
    故在 bizContent 的常见字段名中查找，找不到返回 None。
    """
    if not isinstance(biz, dict):
        return None
    for key in ("appTicket", "app_ticket", "ticket"):
        val = biz.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _extract_auth_code(biz: Any) -> str | None:
    """从解密后的消息中兜底提取企业临时授权码。

    授权码用于换取 certificate（10 分钟有效）。字段名以文档常见命名兜底。
    """
    if not isinstance(biz, dict):
        return None
    for key in ("authCode", "auth_code", "code", "tempAuthCode"):
        val = biz.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def handle_message(client_code: str, envelope: dict[str, Any]) -> dict[str, str]:
    """处理一条推送消息，返回给平台的应答体。

    :param client_code: URL 路径中的客户编码，用于定位租户消息秘钥
    :param envelope: 请求体，形如 {"encryptMsg": "..."}
    :raises WebhookError: 租户不存在/停用、未配置消息秘钥、密文缺失或解密失败
    """
    tenant = store.get_tenant_by_code(client_code)
    if tenant is None:
        raise UnknownTenantError("租户不存在")
    if not tenant.get("enabled"):
        raise UnknownTenantError("租户已停用")

    msg_secret_enc = tenant.get("msg_secret_enc")
    if not msg_secret_enc:
        raise WebhookError("租户未配置消息秘钥")

    encrypt_msg = envelope.get("encryptMsg")
    if not isinstance(encrypt_msg, str) or not encrypt_msg:
        raise WebhookError("缺少 encryptMsg")

    msg_secret = decrypt_secret(msg_secret_enc)
    try:
        plaintext = decrypt_chanjet_message(encrypt_msg, msg_secret)
        payload = json.loads(plaintext)
    except Exception as exc:  # 解密或 JSON 解析失败
        raise WebhookError("消息解密失败") from exc

    msg_type = payload.get("msgType")
    biz = payload.get("bizContent")

    # 已确认：验证消息，直接应答成功
    if msg_type == _MSG_TYPE_APP_TEST:
        logger.info("webhook[%s]: APP_TEST 验证消息", client_code)
        return ACK_SUCCESS

    # 兜底：appTicket 滚动更新
    ticket = _extract_app_ticket(biz)
    if ticket:
        store.update_app_ticket(tenant["id"], ticket)
        logger.info("webhook[%s]: 更新 appTicket", client_code)
        return ACK_SUCCESS

    # 兜底：企业临时授权码 → 交由 token 管理换取 certificate（阶段 4 接入）
    auth_code = _extract_auth_code(biz)
    if auth_code:
        logger.info("webhook[%s]: 收到企业授权码（阶段4处理）", client_code)
        # 阶段 4 将在此调用 token_mgr 用授权码换 certificate 并更新授权态；
        # 当前先记录，正常 ACK。
        store.record_pending_auth_code(tenant["id"], auth_code)
        return ACK_SUCCESS

    # 未知/暂不处理的类型（订单支付、产品线订阅等）：正常 ACK，避免重试风暴
    logger.info("webhook[%s]: 未处理的 msgType=%r，已 ACK", client_code, msg_type)
    return ACK_SUCCESS


# ─────────────────────────── FastAPI 路由 ───────────────────────────

router = APIRouter(prefix="/webhook/chanjet", tags=["webhook"])


@router.post("/{client_code}")
async def receive_message(client_code: str, request: Request) -> JSONResponse:
    """接收畅捷通平台推送的消息。

    响应策略（关键）：
    - 未知/停用租户 → 404，不泄露内部细节；
    - 请求体非法 JSON、解密失败等 → 记录日志但仍返回 200 ACK，
      避免平台判为推送失败而触发重试风暴（消息本身已丢弃，不影响安全）；
    - 正常处理 → 200 {"result":"success"}。
    """
    try:
        envelope = await request.json()
    except Exception:
        logger.warning("webhook[%s]: 请求体非法 JSON", client_code)
        return JSONResponse(ACK_SUCCESS)

    if not isinstance(envelope, dict):
        logger.warning("webhook[%s]: 请求体非对象", client_code)
        return JSONResponse(ACK_SUCCESS)

    try:
        result = handle_message(client_code, envelope)
        return JSONResponse(result)
    except UnknownTenantError:
        # 未知或停用租户：拒绝
        return JSONResponse({"result": "unknown tenant"}, status_code=404)
    except Exception:
        # 解密/解析失败等：记录但仍 ACK，避免重试风暴
        logger.exception("webhook[%s]: 处理异常，已 ACK 丢弃", client_code)
        return JSONResponse(ACK_SUCCESS)
