"""app/api/v1/search.py — 重新查询（exclude 语义）；风格库批量上传在 M4 接入"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.db.session import get_db
from app.repositories.project_repository import ProjectRepository
from app.runtime import store
from app.runtime.runner import TaskAlreadyRunningError, get_running_task, get_runner
from app.runtime.state import STAGE_SELECTING
from app.services.clip_service import get_clip_service
from app.utils.response import success

router = APIRouter(prefix="/projects", tags=["search"])


@router.post("/{project_id}/requery")
async def requery(project_id: str, db: Session = Depends(get_db)):
    """把当前候选累加进 exclude 集合 → 后台重跑检索 → 新候选经 SSE 推送。

    阶段判定读 Redis job stage（DB 行的 stage 在图结束前是滞后的）；
    有进行中任务时拒绝——requery 会清空事件列表，不能污染进行中阶段的 SSE。
    """
    repo = ProjectRepository(db)
    row = await run_in_threadpool(repo.get, project_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"项目不存在: {project_id}")
    if get_running_task(project_id) is not None:
        raise HTTPException(status_code=409, detail="项目已有进行中的任务")
    job = await store.get_job(project_id)
    current_stage = job.get("stage") or row.stage
    if current_stage != STAGE_SELECTING:
        raise HTTPException(status_code=409, detail=f"当前 stage={current_stage}，仅 selecting 可重查")

    # 当前候选 id 累加进 exclude（Redis set，TTL 24h）
    current_ids = [c["id"] for c in (job.get("candidates") or []) if c.get("id")]
    await store.add_excludes(project_id, current_ids)

    # 事件流按次划分：清空旧事件列表（保留 sse_seq 计数器，跨次重连按单调 seq 续传）
    redis = await store.get_redis()
    await redis.delete(f"sse:{project_id}")

    try:
        await get_runner().requery(
            project_id=project_id,
            requirements=row.requirements,
            clip_service=await get_clip_service(),
        )
    except TaskAlreadyRunningError:
        raise HTTPException(status_code=409, detail="项目已有进行中的任务")
    exclude_total = len(await store.get_excludes(project_id))
    return success({"excluded_added": len(current_ids), "exclude_total": exclude_total})
