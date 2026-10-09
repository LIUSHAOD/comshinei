"""app/runtime/runner.py — asyncio.Queue 进度传送带 + emit 封装 + 图执行器

架构（计划 §5.3）：
- 节点经 emit(event_type, data) 把事件扔进进程内 asyncio.Queue（不直接碰 Redis，
  节点逻辑不被 I/O 拖住）；
- 发布协程排空队列 → 写 Redis（sse:{pid} 事件缓冲 + job:{pid} 状态哈希），SSE 端点
  纯从 Redis 读，进程内 Queue 只是生产侧缓冲，多 worker / 断线重连不受影响；
- 图执行结束后把关键产物回写 projects 表（恢复现场用）。

进程内只保留 task 注册表（取消用），状态真相在 Redis + MySQL。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from langgraph.graph.state import CompiledStateGraph

from app.config import settings
from app.db.session import SessionLocal
from app.models.project import Project
from app.runtime import store
from app.runtime.state import (
    EVENT_CANDIDATES,
    EVENT_COMPLETED,
    EVENT_ERROR,
    EVENT_LINEART_DONE,
    EVENT_STAGE_CHANGE,
    STAGE_DONE,
    STAGE_FAILED,
    DesignState,
)
from app.utils.logger import logger

# 进行中任务的注册表（pid → asyncio.Task），仅用于取消；状态不在此保存
_running: dict[str, asyncio.Task] = {}

# 每项目一把启动锁：check-and-register 原子化（confirm/requery 快速双击/并发防重）
_start_locks: dict[str, asyncio.Lock] = {}


class TaskAlreadyRunningError(RuntimeError):
    def __init__(self, project_id: str):
        super().__init__(f"项目已有进行中的任务: {project_id}")
        self.project_id = project_id


def _start_lock(project_id: str) -> asyncio.Lock:
    lock = _start_locks.get(project_id)
    if lock is None:
        lock = _start_locks[project_id] = asyncio.Lock()
    return lock


def _update_project_row(project_id: str, fields: dict[str, Any]) -> None:
    """同步写 projects 表（short op；由 asyncio.to_thread 调用）。"""
    db = SessionLocal()
    try:
        row = db.get(Project, project_id)
        if row is None:
            return
        for k, v in fields.items():
            if hasattr(row, k):
                setattr(row, k, v)
        db.commit()
    except Exception:
        logger.exception("更新 projects 行失败: %s", project_id)
        db.rollback()
    finally:
        db.close()


class _EventPublisher:
    """排空进程内事件队列 → Redis。事件同时驱动 job hash 的关键字段更新。"""

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def emit(self, event_type: str, data: dict[str, Any]) -> None:
        self.queue.put_nowait({"type": event_type, "data": data})

    async def run(self) -> None:
        pid = self.project_id
        while True:
            event = await self.queue.get()
            if event is None:  # 结束哨兵
                return
            event_type, data = event["type"], event["data"]
            try:
                await store.push_event(pid, event_type, data)
                if event_type == EVENT_STAGE_CHANGE:
                    await store.set_job_fields(pid, {"stage": data.get("stage")})
                elif event_type == EVENT_ERROR and data.get("fatal"):
                    # 致命失败：job stage 同步为 failed（持久化在 runner 收尾才落 DB，
                    # 不这么做的话 GET 会在窗口期读到停滞的中间阶段）
                    await store.set_job_fields(pid, {"stage": "failed"})
                elif event_type == EVENT_CANDIDATES:
                    await store.set_job_fields(pid, {"candidates": data.get("candidates", [])})
                elif event_type == EVENT_LINEART_DONE:
                    await store.set_job_fields(
                        pid,
                        {
                            "lineart_url": data.get("lineart_url"),
                            "mlsd_url": data.get("mlsd_url"),
                            "depth_url": data.get("depth_url"),
                            "seg_url": data.get("seg_url"),
                        },
                    )
            except Exception:
                # 事件落 Redis 失败不拖垮图执行，记日志即可
                logger.exception("事件发布失败: %s %s", pid, event_type)


class GraphRunner:
    """阶段 1 / 阶段 2 / 检索重跑的执行器。"""

    def __init__(self, stage1_graph: CompiledStateGraph, stage2_graph: CompiledStateGraph | None = None):
        self.stage1_graph = stage1_graph
        self.stage2_graph = stage2_graph

    async def start_stage1(
        self,
        *,
        project_id: str,
        photo_path: str,
        requirements: str,
        lineart_workflow: dict[str, Any],
        output_dir: Path,
        comfy_runner: Any,
        clip_service: Any,
    ) -> asyncio.Task:
        """启动阶段 1（立即返回，图在后台任务执行，事件经 Redis 流出）。"""
        publisher = _EventPublisher(project_id)

        state: DesignState = {
            "project_id": project_id,
            "photo_path": photo_path,
            "requirements": requirements,
            "exclude_ids": await store.get_excludes(project_id),
            "stage": "created",
            "emit": publisher.emit,
        }
        config = {
            "configurable": {
                "comfy_runner": comfy_runner,
                "clip_service": clip_service,
                "lineart_workflow": lineart_workflow,
                "output_dir": str(output_dir),
                "candidate_limit": settings.SEARCH_CANDIDATE_LIMIT,
            }
        }

        async def _run() -> None:
            publisher_task = asyncio.create_task(publisher.run())
            final_state: dict[str, Any] = {}
            cancelled = False
            try:
                final_state = await self.stage1_graph.ainvoke(state, config=config)
            except asyncio.CancelledError:
                # 取消语义由 cancel 端点全权负责（状态回退/事件），此处不持久化
                cancelled = True
                logger.info("阶段 1 任务被取消: %s", project_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("阶段 1 图执行异常: %s", project_id)
                await publisher.emit(EVENT_ERROR, {"node": "graph", "message": str(exc), "fatal": True})
                final_state = {"stage": STAGE_FAILED, "errors": [str(exc)]}

            # 队列 FIFO + 哨兵垫后：所有事件先于 None 被处理，天然排空
            publisher.queue.put_nowait(None)
            await publisher_task
            _running.pop(project_id, None)
            if cancelled:
                raise asyncio.CancelledError

            stage = final_state.get("stage", STAGE_FAILED)
            db_fields: dict[str, Any] = {"stage": stage}
            # 产物回写 projects 表：存图片引用（outputs/... 相对串），URL 由读取方拼
            for state_key, col in (
                ("lineart_url", "lineart_path"),
                ("mlsd_url", "mlsd_path"),
                ("depth_url", "depth_path"),
            ):
                url = final_state.get(state_key)
                if url:
                    db_fields[col] = str(url).removeprefix("/api/images/")
            if final_state.get("errors"):
                db_fields["error"] = "; ".join(str(e) for e in final_state["errors"])
            await asyncio.to_thread(_update_project_row, project_id, db_fields)
            logger.info("阶段 1 结束: %s stage=%s", project_id, stage)

        # check-and-register 原子化：锁内查重 + 注册，杜绝双击/并发双任务
        async with _start_lock(project_id):
            existing = _running.get(project_id)
            if existing is not None and not existing.done():
                raise TaskAlreadyRunningError(project_id)
            task = asyncio.create_task(_run())
            _running[project_id] = task
            return task

    async def start_stage2(
        self,
        *,
        project_id: str,
        photo_path: str,
        requirements: str,
        selected_ref: dict[str, Any],
        lineart_url: str,
        mlsd_url: str,
        depth_url: str,
        generate_workflow: dict[str, Any],
        output_dir: Path,
        comfy_runner: Any,
        prompt_service: Any,
    ) -> asyncio.Task:
        """confirm 后续跑阶段 2（prompt → generate → assemble），立即返回后台任务。"""
        if self.stage2_graph is None:
            raise RuntimeError("GraphRunner 未配置阶段 2 图")

        publisher = _EventPublisher(project_id)
        state: DesignState = {
            "project_id": project_id,
            "photo_path": photo_path,
            "requirements": requirements,
            "selected_ref": selected_ref,
            "lineart_url": lineart_url,
            "mlsd_url": mlsd_url,
            "depth_url": depth_url,
            "stage": "selecting",
            "emit": publisher.emit,
        }
        config = {
            "configurable": {
                "prompt_service": prompt_service,
                "comfy_runner": comfy_runner,
                "generate_workflow": generate_workflow,
                "output_dir": str(output_dir),
            }
        }

        async def _run() -> None:
            publisher_task = asyncio.create_task(publisher.run())
            final_state: dict[str, Any] = {}
            cancelled = False
            try:
                final_state = await self.stage2_graph.ainvoke(state, config=config)
            except asyncio.CancelledError:
                cancelled = True
                logger.info("阶段 2 任务被取消: %s", project_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("阶段 2 图执行异常: %s", project_id)
                await publisher.emit(EVENT_ERROR, {"node": "graph", "message": str(exc), "fatal": True})
                final_state = {"stage": STAGE_FAILED, "errors": [str(exc)]}

            publisher.queue.put_nowait(None)
            await publisher_task
            _running.pop(project_id, None)
            if cancelled:
                raise asyncio.CancelledError

            stage = final_state.get("stage", STAGE_FAILED)
            db_fields: dict[str, Any] = {"stage": stage}
            if final_state.get("prompt"):
                db_fields["prompt"] = final_state["prompt"]
            if final_state.get("negative_prompt"):
                db_fields["negative_prompt"] = final_state["negative_prompt"]
            if final_state.get("result_url"):
                db_fields["result_path"] = str(final_state["result_url"]).removeprefix("/api/images/")
            if final_state.get("errors"):
                db_fields["error"] = "; ".join(str(e) for e in final_state["errors"])
            elif stage == STAGE_DONE:
                db_fields["error"] = None  # 重试成功，清掉上一次失败的历史错误
            await asyncio.to_thread(_update_project_row, project_id, db_fields)
            logger.info("阶段 2 结束: %s stage=%s", project_id, stage)

        # check-and-register 原子化：锁内查重 + 注册，杜绝双击/并发双任务
        async with _start_lock(project_id):
            existing = _running.get(project_id)
            if existing is not None and not existing.done():
                raise TaskAlreadyRunningError(project_id)
            task = asyncio.create_task(_run())
            _running[project_id] = task
            return task

    async def requery(
        self,
        *,
        project_id: str,
        requirements: str,
        clip_service: Any,
    ) -> asyncio.Task:
        """重查：exclude 累加（Redis set）后单独跑 retrieve 逻辑，新候选经事件流推送。"""
        from app.runtime.nodes import retrieve_node

        publisher = _EventPublisher(project_id)

        async def _run() -> None:
            publisher_task = asyncio.create_task(publisher.run())
            try:
                exclude_ids = await store.get_excludes(project_id)
                state: DesignState = {
                    "project_id": project_id,
                    "requirements": requirements,
                    "exclude_ids": exclude_ids,
                    "emit": publisher.emit,
                }
                config = {"configurable": {"clip_service": clip_service, "candidate_limit": settings.SEARCH_CANDIDATE_LIMIT}}
                update = await retrieve_node(state, config)
                # job.candidates 由 publisher 监听 candidates 事件统一更新
                if update.get("candidates") is not None:
                    # 自包含事件流：以 completed 终止（重查接口已清空旧事件）
                    await publisher.emit(
                        EVENT_COMPLETED,
                        {"stage": "selecting", "candidates": update["candidates"]},
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("重查异常: %s", project_id)
                # 重查的事件流自包含：失败也必须终止，否则 SSE 挂死
                await publisher.emit(EVENT_ERROR, {"node": "retrieve", "message": str(exc), "fatal": True})
            finally:
                publisher.queue.put_nowait(None)
                await publisher_task
                _running.pop(project_id, None)

        # check-and-register 原子化：锁内查重 + 注册，杜绝双击/并发双任务
        async with _start_lock(project_id):
            existing = _running.get(project_id)
            if existing is not None and not existing.done():
                raise TaskAlreadyRunningError(project_id)
            task = asyncio.create_task(_run())
            _running[project_id] = task
            return task


def get_running_task(project_id: str) -> asyncio.Task | None:
    return _running.get(project_id)


_runner: GraphRunner | None = None


def get_runner() -> GraphRunner:
    global _runner
    if _runner is None:
        from app.runtime.graph import get_stage1_graph, get_stage2_graph

        _runner = GraphRunner(get_stage1_graph(), get_stage2_graph())
    return _runner
