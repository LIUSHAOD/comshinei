"""app/api/v1/projects.py — 项目创建 / 查询 / 确认续跑 / 取消"""

import json as jsonlib

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.db.session import get_db
from app.repositories.comfy_workflow_repository import ComfyWorkflowRepository
from app.repositories.project_repository import ProjectRepository
from app.runtime import store
from app.runtime.runner import TaskAlreadyRunningError, get_running_task, get_runner
from app.runtime.state import EVENT_COMPLETED, STAGE_FAILED, STAGE_SELECTING
from app.services.clip_service import get_clip_service
from app.services.comfy import ComfyUIRunner
from app.services.prompt_service import get_prompt_service
from app.storage.paths import project_outputs_dir, project_upload_dir, to_image_ref
from app.utils.response import success

router = APIRouter(prefix="/projects", tags=["projects"])

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}


@router.post("")
async def create_project(
    photo: UploadFile = File(...),
    requirements: str = Form(...),
    db: Session = Depends(get_db),
):
    """multipart 上传实拍图 + 需求 → 创建项目并启动阶段 1（异步，进度走 SSE）。"""
    suffix = ("." + (photo.filename or "").rsplit(".", 1)[-1].lower()) if photo.filename and "." in photo.filename else ""
    if suffix not in _IMAGE_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"不支持的图片格式: {photo.filename}")

    # lineart 工作流模板必须先注册
    wf_record = await run_in_threadpool(ComfyWorkflowRepository(db).get_by_key, "lineart")
    if not wf_record:
        raise HTTPException(status_code=409, detail="lineart 工作流未注册，先 POST /api/workflows")

    photo_bytes = await photo.read()

    # Session 不可跨线程迁移：建行 + 落盘 + 更新放进同一次线程池调用
    def _persist() -> str:
        repo = ProjectRepository(db)
        project = repo.create(requirements=requirements, stage="created")
        photo_path = project_upload_dir(project.id) / f"photo{suffix}"
        photo_path.write_bytes(photo_bytes)
        repo.update(project, photo_path=str(photo_path))
        return project.id

    project_id = await run_in_threadpool(_persist)

    await get_runner().start_stage1(
        project_id=project_id,
        photo_path=str(project_upload_dir(project_id) / f"photo{suffix}"),
        requirements=requirements,
        lineart_workflow=jsonlib.loads(wf_record.json),
        output_dir=project_outputs_dir(project_id),
        comfy_runner=ComfyUIRunner.from_settings(),
        clip_service=await get_clip_service(),
    )
    return success({"id": project_id, "stage": "lineart", "stream_url": f"/api/projects/{project_id}/stream"})


@router.get("/{project_id}")
async def get_project(project_id: str, db: Session = Depends(get_db)):
    """项目详情 + 当前 stage + 线稿/候选/结果 url（刷新恢复现场）。"""
    repo = ProjectRepository(db)
    row = await run_in_threadpool(repo.get, project_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"项目不存在: {project_id}")

    job = await store.get_job(project_id)
    exclude_ids = await store.get_excludes(project_id)

    def _db_url(ref: str | None) -> str | None:
        return f"/api/images/{ref}" if ref else None

    return success(
        {
            "id": row.id,
            # stage 优先取 Redis job（图执行中实时更新），回退 DB 行
            "stage": job.get("stage") or row.stage,
            "requirements": row.requirements,
            "photo_url": _db_url(to_image_ref(row.photo_path)) if row.photo_path else None,
            # 中间产物优先取 Redis job（新鲜），回退 DB 持久化字段
            "lineart_url": job.get("lineart_url") or _db_url(row.lineart_path),
            "mlsd_url": job.get("mlsd_url") or _db_url(row.mlsd_path),
            "depth_url": job.get("depth_url") or _db_url(row.depth_path),
            "candidates": job.get("candidates") or [],
            "exclude_ids": exclude_ids,
            "prompt": row.prompt,
            "negative_prompt": row.negative_prompt,
            "result_url": _db_url(row.result_path),
            "error": row.error,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
    )


class ConfirmIn(BaseModel):
    style_image_id: str


@router.post("/{project_id}/confirm")
async def confirm_project(project_id: str, payload: ConfirmIn, db: Session = Depends(get_db)):
    """用户选中风格图 → 从持久化状态恢复，续跑阶段 2（prompt → generate → assemble）。"""
    repo = ProjectRepository(db)
    row = await run_in_threadpool(repo.get, project_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"项目不存在: {project_id}")
    # selecting 正常确认；failed 允许重试（阶段 2 失败有产物可复用——
    # 阶段 1 失败没有线稿产物，会被下方的产物完整性检查挡住）
    if row.stage not in (STAGE_SELECTING, STAGE_FAILED):
        raise HTTPException(status_code=409, detail=f"当前 stage={row.stage}，仅 selecting/failed 可确认生成")
    if get_running_task(project_id) is not None:
        raise HTTPException(status_code=409, detail="项目已有进行中的任务")

    job = await store.get_job(project_id)
    candidates = job.get("candidates") or []
    selected = next((c for c in candidates if c.get("id") == payload.style_image_id), None)
    if not selected:
        raise HTTPException(status_code=400, detail=f"style_image_id 不在当前候选中: {payload.style_image_id}")

    wf_record = await run_in_threadpool(ComfyWorkflowRepository(db).get_by_key, "generate")
    if not wf_record:
        raise HTTPException(status_code=409, detail="generate 工作流未注册，先 POST /api/workflows")

    def _url(ref: str | None) -> str | None:
        return f"/api/images/{ref}" if ref else None

    lineart_url = job.get("lineart_url") or _url(row.lineart_path)
    mlsd_url = job.get("mlsd_url") or _url(row.mlsd_path)
    depth_url = job.get("depth_url") or _url(row.depth_path)
    if not (lineart_url and mlsd_url and depth_url):
        raise HTTPException(status_code=409, detail="阶段 1 产物不完整，无法续跑")

    await run_in_threadpool(repo.update, row, ref_image_id=payload.style_image_id)

    # 事件流按阶段划分：清空阶段 1 的事件列表（保留 sse_seq 计数器——
    # 跨阶段重连的客户端按单调 seq 续传，不丢阶段 2 事件）
    redis = await store.get_redis()
    await redis.delete(f"sse:{project_id}")

    try:
        await get_runner().start_stage2(
            project_id=project_id,
            photo_path=row.photo_path,
            requirements=row.requirements,
            selected_ref=selected,
            lineart_url=lineart_url,
            mlsd_url=mlsd_url,
            depth_url=depth_url,
            generate_workflow=jsonlib.loads(wf_record.json),
            output_dir=project_outputs_dir(project_id),
            comfy_runner=ComfyUIRunner.from_settings(),
            prompt_service=get_prompt_service(),
        )
    except TaskAlreadyRunningError:
        raise HTTPException(status_code=409, detail="项目已有进行中的任务")
    return success({"id": project_id, "stage": "prompting", "stream_url": f"/api/projects/{project_id}/stream"})


@router.post("/{project_id}/cancel")
async def cancel_project(project_id: str, db: Session = Depends(get_db)):
    """取消进行中的任务（调 ComfyUI /interrupt）；按所处阶段回退状态。

    阶段判定读 Redis job stage（图执行实时更新）——DB 行的 stage 在图结束前
    是滞后的，拿它当真相会把进行中的阶段 2 误判成 selecting。
    """
    repo = ProjectRepository(db)
    row = await run_in_threadpool(repo.get, project_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"项目不存在: {project_id}")

    task = get_running_task(project_id)
    if task is None or task.done():
        raise HTTPException(status_code=409, detail="无进行中的任务")
    task.cancel()

    interrupted = await ComfyUIRunner.from_settings().interrupt()

    job = await store.get_job(project_id)
    # 已知盲区：job 恰好丢失（Redis 重启/过期）而任务在跑时，回退到滞后的 DB stage
    # 可能误判阶段（窗口极窄，任务本身就持锁，出错也只是回退方向反了，可接受）
    current = job.get("stage") or row.stage
    if current in ("prompting", "generating"):
        # 阶段 2 取消：阶段 1 产物仍在，回退到选图，可重新 confirm
        rollback = STAGE_SELECTING
        await run_in_threadpool(repo.update, row, stage=rollback, error=None)
    else:
        # 阶段 1 取消：线稿未出无法续跑，标记失败
        rollback = STAGE_FAILED
        await run_in_threadpool(repo.update, row, stage=rollback, error="用户取消")
    await store.set_job_fields(project_id, {"stage": rollback})
    # 推终止事件收尾本轮事件流，SSE 不挂死
    await store.push_event(
        project_id, EVENT_COMPLETED, {"stage": rollback, "cancelled": True, "message": "任务已取消"}
    )

    return success({"stage": rollback, "comfy_interrupted": interrupted}, message="cancelled")


@router.delete("/{project_id}")
async def delete_project(project_id: str):
    """删除项目及其图片文件、Redis 键。进行中任务先取消（task.cancel + ComfyUI
    interrupt）再删——否则后台 publisher 会重建 sse 键写入孤儿事件。"""
    from app.services.cleanup_service import get_cleanup_service

    task = get_running_task(project_id)
    if task is not None and not task.done():
        task.cancel()
        await ComfyUIRunner.from_settings().interrupt()

    if not await get_cleanup_service().delete_project(project_id):
        raise HTTPException(status_code=404, detail=f"项目不存在: {project_id}")
    return success(message="deleted")
