"""app/models/style_image.py — 风格库图片元数据（向量落在 Qdrant）"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def new_style_image_id() -> str:
    return f"style_{uuid.uuid4().hex}"


class StyleImage(Base):
    __tablename__ = "style_images"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_style_image_id)
    qdrant_point_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    file_path: Mapped[str] = mapped_column(String(512))
    source: Mapped[str] = mapped_column(String(16), default="upload")  # upload | import
    # 预留软删除位（当前实现为硬删除：删行时同步删 Qdrant 点与文件）
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | deleted
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
