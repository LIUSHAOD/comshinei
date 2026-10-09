"""app/runtime/state.py — LangGraph 共享状态（开发计划 §5.2）"""

import operator
from typing import Annotated, Any, Awaitable, Callable, TypedDict

from langgraph.graph.message import add_messages

# 节点内推送 SSE 事件的函数签名：emit(event_type, data)
EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]

# 阶段机：created → lineart(阶段1并行检索) → selecting → prompting → generating → done | failed
STAGE_CREATED = "created"
STAGE_LINEART = "lineart"
STAGE_SELECTING = "selecting"
STAGE_PROMPTING = "prompting"
STAGE_GENERATING = "generating"
STAGE_DONE = "done"
STAGE_FAILED = "failed"

# SSE 事件类型（计划 §7）
EVENT_STAGE_CHANGE = "stage_change"
EVENT_LINEART_DONE = "lineart_done"
EVENT_CANDIDATES = "candidates"
EVENT_PROGRESS = "progress"
EVENT_IMAGE_DONE = "image_done"
EVENT_ERROR = "error"
EVENT_COMPLETED = "completed"

# 终止事件：completed 总是终止；error 仅当 data.fatal 为真时终止
# （节点级非致命错误——如 retrieve 失败——也会推 error 事件，但流程继续，SSE 不该断）
TERMINAL_EVENTS = {EVENT_COMPLETED, EVENT_ERROR}


def is_terminal_event(event: dict) -> bool:
    if event.get("type") == EVENT_COMPLETED:
        return True
    return event.get("type") == EVENT_ERROR and bool((event.get("data") or {}).get("fatal"))


class DesignState(TypedDict, total=False):
    project_id: str
    photo_path: str
    requirements: str

    # 阶段 1 产物
    lineart_url: str | None
    mlsd_url: str | None
    depth_url: str | None
    seg_url: str | None                # M1 lineart 工作流的第 4 产物（语义分割，下游暂不用）
    candidates: list[dict]             # [{id, image_url, score}]
    exclude_ids: Annotated[list[str], operator.add]   # 重查时累加

    # 阶段 2（M5）
    selected_ref: dict | None
    prompt: str | None
    negative_prompt: str | None
    result_url: str | None

    # 通用
    stage: str
    errors: Annotated[list[str], operator.add]
    messages: Annotated[list, add_messages]
    emit: EmitFn
