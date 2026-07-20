"""每租户 accessToken 生命周期管理。

核心：ensure_access_token(tenant_id) —— 返回一个当前有效的 accessToken。

策略（见记忆 chanjet-auth-model）：
- accessToken 有效期约 6 天；提前 EXPIRY_SKEW_SECONDS 视为过期。
- 无独立 refreshToken 刷新接口；过期或缺失时，用该租户握有的
  (appKey + appSecret + appTicket + certificate) 重新调 generateToken。
- appTicket 由 webhook 滚动写入（30 分钟有效）；certificate 由管理员录入。
  任一缺失则无法换取，抛 TokenError 并给出明确原因（供连接测试展示）。

本模块不做 HTTP（交给 client），不直接写 SQL（交给 store），只编排。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import store
from app.chanjet import client
from app.security import decrypt_secret

# accessToken 到期前多少秒就提前续取，避免边界期调用失败
EXPIRY_SKEW_SECONDS = 600


class TokenError(Exception):
    """无法获得有效 accessToken。code 用于连接测试展示明确错误。"""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


def _is_token_valid(tenant: dict) -> bool:
    """判断已缓存 accessToken 是否仍在有效期内（含提前量）。"""
    if not tenant.get("access_token_enc"):
        return False
    expires_at = tenant.get("token_expires_at")
    if not expires_at:
        return False
    try:
        exp = datetime.fromisoformat(expires_at)
    except (TypeError, ValueError):
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    remaining = (exp - datetime.now(timezone.utc)).total_seconds()
    return remaining > EXPIRY_SKEW_SECONDS


def _refresh_token(tenant: dict) -> str:
    """用租户凭据重新调 generateToken，落库并返回新的 accessToken。

    :raises TokenError: 凭据缺失或接口失败
    """
    app_key = tenant.get("app_key")
    app_secret_enc = tenant.get("app_secret_enc")
    certificate_enc = tenant.get("certificate_enc")
    app_ticket = tenant.get("app_ticket")

    if not app_key or not app_secret_enc:
        raise TokenError("租户未配置 appKey/appSecret", code="MISSING_APP_CREDENTIALS")
    if not certificate_enc:
        raise TokenError("租户未配置 certificate（需管理员授权后录入）", code="MISSING_CERTIFICATE")
    if not app_ticket:
        raise TokenError(
            "尚未收到 appTicket（需在自建应用配置消息接收地址后等待平台推送）",
            code="MISSING_APP_TICKET",
        )

    app_secret = decrypt_secret(app_secret_enc)
    certificate = decrypt_secret(certificate_enc)

    try:
        value = client.generate_token(
            app_key=app_key,
            app_secret=app_secret,
            app_ticket=app_ticket,
            certificate=certificate,
        )
    except client.ChanjetApiError as exc:
        raise TokenError(
            f"换取 token 失败：{exc}", code=exc.code or "CHANJET_TOKEN_FAILED"
        ) from exc
    except Exception as exc:  # 网络/HTTP 层
        raise TokenError(f"换取 token 网络异常：{exc}", code="CHANJET_NETWORK_ERROR") from exc

    access_token = value.get("accessToken")
    if not access_token:
        raise TokenError("generateToken 未返回 accessToken", code="CHANJET_TOKEN_EMPTY")

    # expiresIn / refreshExpiresIn 单位为秒，在此换算为绝对到期时间（分层：换算属编排层）
    now_dt = datetime.now(timezone.utc)

    def _expiry(seconds) -> str | None:
        try:
            return (now_dt + timedelta(seconds=int(seconds))).isoformat()
        except (TypeError, ValueError):
            return None

    store.save_token_bundle(
        tenant["id"],
        access_token=access_token,
        refresh_token=value.get("refreshToken"),
        token_expires_at=_expiry(value.get("expiresIn")),
        refresh_expires_at=_expiry(value.get("refreshExpiresIn")),
        org_id=value.get("orgId"),
        user_id=value.get("userId"),
        scope=value.get("scope"),
        app_name=value.get("appName"),
    )
    return access_token


def ensure_access_token(tenant_id: str) -> str:
    """返回该租户当前有效的 accessToken；必要时自动续取。

    :raises TokenError: 租户不存在或凭据不足/接口失败
    """
    tenant = store.get_tenant(tenant_id)
    if tenant is None:
        raise TokenError("租户不存在", code="TENANT_NOT_FOUND")
    if not tenant.get("enabled"):
        raise TokenError("租户已停用", code="TENANT_DISABLED")

    if _is_token_valid(tenant):
        return decrypt_secret(tenant["access_token_enc"])

    return _refresh_token(tenant)
