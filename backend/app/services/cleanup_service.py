"""app/services/cleanup_service.py — 图片手动清理 + TTL 过期清理（计划 §6/§7）

三件事：
1. TTL 清理：超龄项目（projects.created_at < now - ttl_days）连锅端——
   删 projects 行 + 上传/产物目录 + Redis 键（sse/sse_seq/job/exclude）；
2. Qdrant 对账：删除没有 style_images 行的孤儿向量点（M4 上传补偿双重故障的兜底）；
3. cleanup_rules 单行配置记录 last_run_at。

同步落盘操作走线程池；APScheduler 在 app 事件循环上直接调度 async run()。
"""

from __future__ import annotations

import asyncio
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.db.session import SessionLocal
from app.models.cleanup_rule import CleanupRule
from app.models.project import Project
from app.models.style_image import StyleImage
from app.runtime import store
from app.services.clip_service import get_clip_service
from app.storage.paths import outputs_dir, project_upload_dir
from app.utils.logger import logger


class CleanupService:
    # ---- 单项目连锅端（DELETE /projects/{id} 复用） ----

    async def delete_project(self, project_id: str) -> bool:
        """删项目行 + 文件 + Redis 键；项目不存在返回 False。"""
        def _delete_row() -> bool:
            db = SessionLocal()
            try:
                row = db.get(Project, project_id)
                if not row:
                    return False
                db.delete(row)
                db.commit()
                return True
            finally:
                db.close()

        if not await run_in_threadpool(_delete_row):
            return False

        def _rm_files() -> None:
            shutil.rmtree(project_upload_dir(project_id), ignore_errors=True)
            shutil.rmtree(outputs_dir() / project_id, ignore_errors=True)

        await run_in_threadpool(_rm_files)
        redis = await store.get_redis()
        await redis.delete(
            f"sse:{project_id}", f"sse_seq:{project_id}", f"job:{project_id}", f"exclude:{project_id}"
        )
        return True

    # ---- TTL 批量清理 ----

    async def run_ttl(self, ttl_days: int | None = None) -> dict[str, Any]:
        ttl = ttl_days if ttl_days is not None else settings.IMAGE_TTL_DAYS
        cutoff = datetime.now() - timedelta(days=ttl)

        def _expired_ids() -> list[str]:
            db = SessionLocal()
            try:
                rows = db.query(Project.id).filter(Project.created_at < cutoff).all()
                return [r[0] for r in rows]
            finally:
                db.close()

        ids = await run_in_threadpool(_expired_ids)
        deleted = 0
        for pid in ids:
            if await self.delete_project(pid):
                deleted += 1
        logger.info("TTL 清理: %d 个超龄项目（>%d 天）", deleted, ttl)
        return {"ttl_days": ttl, "projects_deleted": deleted}

    # ---- Qdrant 孤儿点对账 ----

    async def run_qdrant_reconcile(self) -> dict[str, Any]:
        """删除 Qdrant 中没有对应 style_images 行的点（payload.style_image_id 无行）。

        上传链路是先写 Qdrant 再落 MySQL，对账若恰好跑在两者之间会误删在途点
        （变成反向孤儿：有行无向量）。豁免最近 10 分钟写入的点来合上窗口。
        """

        def _style_ids() -> set[str]:
            db = SessionLocal()
            try:
                return {r[0] for r in db.query(StyleImage.id).all()}
            finally:
                db.close()

        existing = await run_in_threadpool(_style_ids)
        clip = await get_clip_service()

        def _reconcile() -> int:
            from qdrant_client import models

            grace_ts = int(time.time()) - 600  # 在途上传豁免窗口
            client = clip._get_qdrant()
            if not client.collection_exists(settings.QDRANT_COLLECTION):
                return 0
            orphans: list[str] = []
            offset = None
            while True:
                points, offset = client.scroll(
                    collection_name=settings.QDRANT_COLLECTION,
                    limit=256,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for p in points:
                    payload = p.payload or {}
                    created_ts = payload.get("created_ts")
                    if isinstance(created_ts, int) and created_ts > grace_ts:
                        continue  # 可能正在上传途中，跳过
                    if payload.get("style_image_id") not in existing:
                        orphans.append(p.id)
                if offset is None:
                    break
            if orphans:
                client.delete(
                    collection_name=settings.QDRANT_COLLECTION,
                    wait=True,
                    points_selector=models.PointIdsList(points=orphans),
                )
            return len(orphans)

        deleted = await run_in_threadpool(_reconcile)
        if deleted:
            logger.info("Qdrant 对账: 删除 %d 个孤儿点", deleted)
        return {"orphan_points_deleted": deleted}

    # ---- 总入口（定时任务 + 手动接口） ----

    async def run_all(self) -> dict[str, Any]:
        ttl = await self.run_ttl()
        reconcile = await self.run_qdrant_reconcile()
        last_run_at = datetime.now()

        def _touch_rule() -> None:
            db = SessionLocal()
            try:
                rule = db.query(CleanupRule).first()
                if rule is None:
                    rule = CleanupRule(rule_type="ttl", ttl_days=settings.IMAGE_TTL_DAYS)
                    db.add(rule)
                rule.last_run_at = last_run_at
                db.commit()
            finally:
                db.close()

        await run_in_threadpool(_touch_rule)
        report = {**ttl, **reconcile, "last_run_at": last_run_at.isoformat()}
        logger.info("清理完成: %s", report)
        return report


_cleanup_service: CleanupService | None = None


def get_cleanup_service() -> CleanupService:
    global _cleanup_service
    if _cleanup_service is None:
        _cleanup_service = CleanupService()
    return _cleanup_service
