"""app/api/v1/workflows.py — ComfyUI 工作流模板注册 / 列表

工作流即数据：模板 API JSON 存 comfy_workflows 表；注册时按 (Input)/(Output)
后缀约定解析出 inputs/outputs 元数据一并返回。新增工作流零代码改动。
"""

import json as jsonlib
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.comfy_workflow import ComfyWorkflow
from app.repositories.comfy_workflow_repository import ComfyWorkflowRepository
from app.services.workflow_parse import parse_dynamic_inputs, parse_dynamic_outputs
from app.utils.response import success

router = APIRouter(prefix="/workflows", tags=["workflows"])


class WorkflowRegisterIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    workflow_key: str = Field(..., description="业务键，如 lineart / generate")
    name: str = Field(..., description="展示名")
    kind: Literal["image"] = "image"
    json_: dict[str, Any] = Field(..., alias="json", description="ComfyUI 导出的 API 格式 JSON")


def _serialize(record: ComfyWorkflow, *, with_json: bool = False) -> dict[str, Any]:
    workflow = jsonlib.loads(record.json)
    data: dict[str, Any] = {
        "id": record.id,
        "workflow_key": record.workflow_key,
        "name": record.name,
        "kind": record.kind,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "inputs": parse_dynamic_inputs(workflow),
        "outputs": parse_dynamic_outputs(workflow),
    }
    if with_json:
        data["json"] = workflow
    return data


@router.get("")
def list_workflows(db: Session = Depends(get_db)):
    repo = ComfyWorkflowRepository(db)
    records = repo.get_multi(limit=200)
    return success([_serialize(r) for r in records])


@router.get("/{workflow_key}")
def get_workflow(workflow_key: str, db: Session = Depends(get_db)):
    repo = ComfyWorkflowRepository(db)
    record = repo.get_by_key(workflow_key)
    if not record:
        raise HTTPException(status_code=404, detail=f"工作流不存在: {workflow_key}")
    return success(_serialize(record, with_json=True))


@router.post("")
def register_workflow(payload: WorkflowRegisterIn, db: Session = Depends(get_db)):
    repo = ComfyWorkflowRepository(db)
    if repo.get_by_key(payload.workflow_key):
        raise HTTPException(status_code=409, detail=f"workflow_key 已存在: {payload.workflow_key}")

    try:
        record = repo.create(
            workflow_key=payload.workflow_key,
            name=payload.name,
            kind=payload.kind,
            json=jsonlib.dumps(payload.json_, ensure_ascii=False),
        )
    except IntegrityError:
        # 并发下先查后插仍可能撞唯一键，归一为 409
        db.rollback()
        raise HTTPException(status_code=409, detail=f"workflow_key 已存在: {payload.workflow_key}")
    return success(_serialize(record), message="registered")


@router.delete("/{workflow_key}")
def delete_workflow(workflow_key: str, db: Session = Depends(get_db)):
    repo = ComfyWorkflowRepository(db)
    record = repo.get_by_key(workflow_key)
    if not record:
        raise HTTPException(status_code=404, detail=f"工作流不存在: {workflow_key}")
    repo.remove(record.id)
    return success(message="deleted")
