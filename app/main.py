"""FastAPI 入口。

启动时初始化数据库（建表幂等）+ 创建初始管理员，挂载各路由。
后续阶段在此挂载 webhook、MCP 路由；当前已挂载 admin 后台。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from app.admin.routes import router as admin_router
from app.chanjet.webhook import router as webhook_router
from app.mcpsrv.server import router as mcp_router
from app.config import get_settings
from app.db import init_db
from app.store import ensure_initial_admin


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：建表（幂等）+ 创建初始管理员
    init_db()
    settings = get_settings()
    ensure_initial_admin(settings.admin_username, settings.admin_password)
    yield


app = FastAPI(title="畅捷通 OpenAPI → MCP 转换服务", lifespan=lifespan)

_settings = get_settings()
app.add_middleware(
    SessionMiddleware,
    secret_key=_settings.session_secret or "dev-insecure-session-secret",
    max_age=_settings.session_max_age_minutes * 60,
    same_site="lax",
    https_only=False,
)

app.include_router(admin_router)
app.include_router(webhook_router)
app.include_router(mcp_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def root():
    return RedirectResponse(url="/admin/tenants", status_code=303)
