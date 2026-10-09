"""app/models/project.py — 一次生图任务 = 一行"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def new_project_id() -> str:
    return f"proj_{uuid.uuid4().hex}"


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_project_id)
    requirements: Mapped[str] = mapped_column(Text, default="")
    # lineart | retrieving | selecting | prompting | generating | done | failed
    stage: Mapped[str] = mapped_column(String(32), default="created", index=True)

    photo_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    lineart_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mlsd_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    depth_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    negative_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    ref_image_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
