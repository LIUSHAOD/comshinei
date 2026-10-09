"""app/main.py — FastAPI 入口：路由挂载、CORS、异常处理"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import v1_router
from app.config import settings
from app.db import redis as redis_module
from app.middleware.exception_handler import (
    global_exception_handler,
    http_exception_handler,
    validation_exception_handler,
    value_error_handler,
)
from app.middleware.request_log import RequestLogMiddleware
from app.utils.logger import logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 存储目录
    try:
        from app.storage.paths import ensure_storage_dirs

        ensure_storage_dirs()
    except Exception as e:
        logger.error(f"Failed to initialize storage directories: {e}")

    # 开发便利：缺表时自动建表（正式路径仍是 alembic upgrade head）；DB 未起不阻断启动。
    # 与 alembic 双轨协调：仅当库从未被 alembic 管理（无版本记录）时，
    # 建表后把版本戳到当前最新迁移，避免之后 upgrade head 撞 "table already exists"。
    try:
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory

        from app.db.session import engine
        from app.models.base import Base
        import app.models  # noqa: F401

        Base.metadata.create_all(bind=engine)
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            if ctx.get_current_revision() is None:
                script = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "alembic"))
                ctx.stamp(script, script.get_current_head())
                logger.info(f"Fresh database stamped at alembic revision {script.get_current_head()}")
    except Exception as e:
        logger.warning(f"Auto table creation skipped or failed (use alembic): {e}")

    await redis_module.init_redis()

    # 定时 TTL 清理（APScheduler 随 backend 启动；手动触发走 POST /api/cleanup/run）
    scheduler = None
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        from app.services.cleanup_service import get_cleanup_service

        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            get_cleanup_service().run_all,
            "interval",
            hours=settings.CLEANUP_INTERVAL_HOURS,
            id="image_ttl_cleanup",
            replace_existing=True,
        )
        scheduler.start()
        logger.info(f"TTL 清理定时任务已启动（每 {settings.CLEANUP_INTERVAL_HOURS}h）")
    except Exception as e:
        logger.error(f"Failed to start cleanup scheduler: {e}")

    logger.info("Comshinei backend started")
    yield
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
    await redis_module.close_redis()


app = FastAPI(title="Comshinei 室内设计 Agent", version="0.2.0", lifespan=lifespan)

allowed_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
]

app.add_middleware(RequestLogMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(v1_router, prefix="/api")

app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(ValueError, value_error_handler)
app.add_exception_handler(Exception, global_exception_handler)


@app.get("/health")
async def health():
    return {"status": "ok"}
