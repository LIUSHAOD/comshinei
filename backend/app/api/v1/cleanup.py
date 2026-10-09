"""app/api/v1/cleanup.py — 手动执行清理"""

from fastapi import APIRouter

from app.services.cleanup_service import get_cleanup_service
from app.utils.response import success

router = APIRouter(prefix="/cleanup", tags=["cleanup"])


@router.post("/run")
async def run_cleanup():
    """手动执行一轮清理：TTL 超龄项目 + Qdrant 孤儿点对账，返回统计。"""
    report = await get_cleanup_service().run_all()
    return success(report)
