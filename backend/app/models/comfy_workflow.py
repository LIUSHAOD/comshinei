"""app/models/comfy_workflow.py — ComfyUI 工作流模板库（工作流即数据）

模板 JSON 为 ComfyUI 导出的 API 格式，节点按 (Input)/(Output) 标题后缀约定命名
（见 services/workflow_parse.py）。新增工作流 = 插一行，零代码改动。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def new_workflow_id() -> str:
    return f"wf_{uuid.uuid4().hex}"


class ComfyWorkflow(Base):
    __tablename__ = "comfy_workflows"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_workflow_id)
    # 业务键，如 lineart / generate，后端按此选用模板
    workflow_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16), default="image")  # 产物类型：image
    # API JSON 可能超过 TEXT 的 64KB，MySQL 上用 LONGTEXT
    json: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
