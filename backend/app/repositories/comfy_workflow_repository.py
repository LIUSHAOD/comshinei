"""app/repositories/comfy_workflow_repository.py"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.comfy_workflow import ComfyWorkflow
from app.repositories.base import BaseRepository


class ComfyWorkflowRepository(BaseRepository[ComfyWorkflow]):
    def __init__(self, db: Session):
        super().__init__(ComfyWorkflow, db)

    def get_by_key(self, workflow_key: str) -> ComfyWorkflow | None:
        stmt = select(ComfyWorkflow).where(ComfyWorkflow.workflow_key == workflow_key)
        return self.db.scalars(stmt).first()
