"""app/api/v1/comfy.py — ComfyUI 直调接口（M2 联调 / 排障用）

不经过 LangGraph，直接：加载模板（DB 或内联 JSON）→ patch 输入 → ComfyUIRunner
执行 → 返回产物路径。正式业务链路（M3+）走 /api/projects，由图节点内部调 runner。
"""

import json as jsonlib
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.db.session import get_db
from app.repositories.comfy_workflow_repository import ComfyWorkflowRepository
from app.services.comfy import ComfyUIRunner
from app.services.workflow_parse import (
    input_node_ids,
    parse_dynamic_outputs,
    patch_workflow,
)
from app.storage.paths import outputs_dir
from app.utils.response import success

router = APIRouter(prefix="/comfy", tags=["comfy"])


class ComfyRunIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    workflow_key: str | None = Field(None, description="comfy_workflows 表里的业务键，如 lineart / generate")
    json_: dict[str, Any] | None = Field(None, alias="json", description="或直接内联 API 格式 JSON")
    # 键支持两种：输入名（(Input) 标题去后缀，如 "提示词"）或 node_id（如 "11"）
    input_values: dict[str, Any] = Field(default_factory=dict)
    output_subdir: str | None = Field(None, description="产物子目录，默认 debug/<时间戳>")


@router.get("/ping")
async def ping():
    runner = ComfyUIRunner.from_settings()
    reachable = await runner.ping()
    return success({"base_url": runner.base_url, "reachable": reachable})


def _resolve_output_dir(subdir: str | None) -> Path:
    """把 output_subdir 约束在 outputs 根目录内：拒绝绝对路径、盘符与 '..' 段。"""
    root = outputs_dir()
    if not subdir:
        return root / "debug" / str(int(time.time()))
    p = Path(subdir)
    if p.is_absolute() or p.drive or ".." in p.parts:
        raise HTTPException(status_code=400, detail="output_subdir 非法：不允许绝对路径或 '..'")
    return root / p


@router.post("/run")
async def run_workflow(payload: ComfyRunIn, db: Session = Depends(get_db)):
    if payload.workflow_key:
        # 同步 SQLAlchemy 会话丢线程池，避免阻塞事件循环
        repo = ComfyWorkflowRepository(db)
        record = await run_in_threadpool(repo.get_by_key, payload.workflow_key)
        if not record:
            raise HTTPException(status_code=404, detail=f"工作流不存在: {payload.workflow_key}")
        workflow = jsonlib.loads(record.json)
    elif payload.json_:
        workflow = payload.json_
    else:
        raise HTTPException(status_code=400, detail="workflow_key 与 json 必须提供一个")

    outputs_spec = parse_dynamic_outputs(workflow)
    if not outputs_spec:
        raise HTTPException(status_code=400, detail="工作流没有任何 (Output) 节点，无法下载产物")

    # 输入名 → node_id 映射；不在映射里的键按 node_id 原样透传
    key_to_id = input_node_ids(workflow)
    by_node_id = {key_to_id.get(k, k): v for k, v in payload.input_values.items()}
    patched = patch_workflow(workflow, by_node_id)

    out_dir = _resolve_output_dir(payload.output_subdir)

    runner = ComfyUIRunner.from_settings()
    result = await runner.run(patched, output_dir=out_dir, outputs_spec=outputs_spec)
    if not result.success:
        raise HTTPException(status_code=502, detail=result.error)

    return success(
        {
            "prompt_id": result.prompt_id,
            "output_dir": str(out_dir),
            "outputs": {key: str(path) for key, path in result.outputs.items()},
        }
    )
