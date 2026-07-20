"""平台级配置。

只承载**平台级机密**（AES-GCM 主密钥、管理员初始账号、Session 密钥等），
租户的畅捷通凭据一律走后台录入并加密入库，不在此处。
"""

from __future__ import annotations

import base64
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # AES-GCM 主密钥（Base64 编码的 32 字节），加密租户敏感字段
    master_key: str = ""

    # 管理员初始账号（首次启动自动创建）
    admin_username: str = "admin"
    admin_password: str = ""

    # Session 签名密钥
    session_secret: str = ""
    session_max_age_minutes: int = 480

    # 对外访问域名（生成 webhook / MCP 地址提示用）
    public_base_url: str = "https://mcp.example.com"

    # SQLite 数据库文件路径
    db_path: str = "data/cjt2mcp.db"

    def master_key_bytes(self) -> bytes:
        """解码主密钥为 32 字节。未配置或长度错误时抛错。"""
        if not self.master_key:
            raise RuntimeError("未配置 MASTER_KEY 环境变量")
        key = base64.b64decode(self.master_key)
        if len(key) != 32:
            raise RuntimeError(f"MASTER_KEY 解码后须为 32 字节，实际 {len(key)}")
        return key


@lru_cache
def get_settings() -> Settings:
    return Settings()
