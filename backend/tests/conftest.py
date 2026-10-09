"""tests/conftest.py"""

# 测试会话统一隔离：sqlite 测试库 + Redis db1（须在导入任何 app 模块前设置）
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./data/test.db")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/1")

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def lineart_workflow() -> dict:
    return json.loads((FIXTURES / "lineart_api.json").read_text(encoding="utf-8"))


@pytest.fixture
def generate_workflow() -> dict:
    return json.loads((FIXTURES / "generate_api.json").read_text(encoding="utf-8"))


@pytest.fixture
def test_photo() -> Path:
    return FIXTURES / "test_photo.png"


@pytest.fixture
def db_session():
    """sqlite 测试库会话（建表幂等）。"""
    from app.db.session import SessionLocal, engine
    from app.models.base import Base
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def fake_redis(monkeypatch):
    """用 fakeredis 替换 store 层的 Redis 连接（单测无需真实 Redis）。"""
    import fakeredis.aioredis

    import app.runtime.store as store_module

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def _get():
        return fake

    monkeypatch.setattr(store_module, "get_redis", _get)
    return fake


@pytest.fixture(autouse=True)
async def _reset_redis_singleton(request):
    """集成测试前后重置全局 Redis 客户端单例。

    pytest-asyncio 每个 async 测试独立事件循环；进程级 _redis 的连接绑定在
    上一个测试已关闭的循环上，下个测试复用即报错（曾致集成测试被静默 skip）。
    """
    if "integration" not in request.keywords:
        yield
        return
    import app.db.redis as redis_module

    try:
        await redis_module.close_redis()
    except Exception:
        redis_module._redis = None
    yield
    try:
        await redis_module.close_redis()
    except Exception:
        redis_module._redis = None
