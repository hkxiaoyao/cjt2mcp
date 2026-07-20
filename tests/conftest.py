"""测试夹具：在导入应用前注入平台级环境变量，并使用临时数据库。

crypto 字段加密依赖 MASTER_KEY；Session/管理员等依赖对应 env。测试统一在此
设置，保证 get_settings 首次读取即拿到测试值，且每个测试用独立临时库互不干扰。
"""

from __future__ import annotations

import base64
import os

# 必须在任何 app.* 导入前设置，否则 get_settings 的 lru_cache 会缓存空值
os.environ.setdefault("MASTER_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "test-pw")
os.environ.setdefault("SESSION_SECRET", "test-session-secret")

import pytest


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """为单个测试提供独立的 SQLite 文件并建表。"""
    from app.config import get_settings
    from app import db as db_mod

    db_file = tmp_path / "test.db"
    get_settings.cache_clear()
    monkeypatch.setenv("DB_PATH", str(db_file))
    get_settings.cache_clear()
    db_mod.init_db()
    yield str(db_file)
    get_settings.cache_clear()
