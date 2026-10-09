"""app/repositories/style_image_repository.py"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.style_image import StyleImage
from app.repositories.base import BaseRepository


class StyleImageRepository(BaseRepository[StyleImage]):
    def __init__(self, db: Session):
        super().__init__(StyleImage, db)

    def list_active(self, *, skip: int = 0, limit: int = 100) -> list[StyleImage]:
        stmt = (
            select(StyleImage)
            .where(StyleImage.status == "active")
            .order_by(StyleImage.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        return list(self.db.scalars(stmt).all())

    def count_active(self) -> int:
        stmt = select(func.count()).select_from(StyleImage).where(StyleImage.status == "active")
        return self.db.scalar(stmt) or 0
