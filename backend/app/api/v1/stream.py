"""app/api/v1/stream.py — SSE 进度流

事件真相在 Redis（sse:{pid} list）：端点先回放 after_seq 之后的历史事件，再轮询增量，
收到终止事件（completed / error）或客户端断开即结束。每 15s 心跳注释防代理断连。
断线重连：客户端带 Last-Event-ID 头或 ?after=<seq> 即可从断点续传。
"""

import asyncio
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.db.session import get_db
from app.repositories.project_repository import ProjectRepository
from app.runtime import store
from app.runtime.state import is_terminal_event

router = APIRouter(prefix="/projects", tags=["stream"])

_POLL_INTERVAL = 0.5
_HEARTBEAT_INTERVAL = 15.0


@router.get("/{project_id}/stream")
async def stream_project(
    project_id: str,
    request: Request,
    after: int = 0,
    db: Session = Depends(get_db),
):
    repo = ProjectRepository(db)
    if not await run_in_threadpool(repo.get, project_id):
        raise HTTPException(status_code=404, detail=f"项目不存在: {project_id}")

    # EventSource 断线重连会带 Last-Event-ID
    last_event_id = request.headers.get("Last-Event-ID")
    if last_event_id and last_event_id.isdigit():
        after = max(after, int(last_event_id))

    async def gen():
        last = after
        last_heartbeat = time.monotonic()
        while True:
            events = await store.get_events(project_id, after_seq=last)
            for ev in events:
                yield (
                    f"id: {ev['seq']}\n"
                    f"event: {ev['type']}\n"
                    f"data: {json.dumps(ev['data'], ensure_ascii=False)}\n\n"
                )
                last = ev["seq"]
                if is_terminal_event(ev):
                    return
            if await request.is_disconnected():
                return
            now = time.monotonic()
            if now - last_heartbeat >= _HEARTBEAT_INTERVAL:
                yield ": ping\n\n"
                last_heartbeat = now
            await asyncio.sleep(_POLL_INTERVAL)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx 关缓冲，SSE 不被截留
        },
    )
