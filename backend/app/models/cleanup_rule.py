"""app/models/cleanup_rule.py — 图片清理规则（单行配置即可）"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def new_cleanup_rule_id() -> str:
    return f"rule_{uuid.uuid4().hex}"


class CleanupRule(Base):
    __tablename__ = "cleanup_rules"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_cleanup_rule_id)
    rule_type: Mapped[str] = mapped_column(String(16), default="ttl")  # manual | ttl
    ttl_days: Mapped[int] = mapped_column(Integer, default=30)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
