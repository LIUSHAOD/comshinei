"""app/api/v1/router.py — v1 路由汇总"""

from fastapi import APIRouter

from app.api.v1.cleanup import router as cleanup_router
from app.api.v1.comfy import router as comfy_router
from app.api.v1.images import router as images_router
from app.api.v1.projects import router as projects_router
from app.api.v1.search import router as search_router
from app.api.v1.stream import router as stream_router
from app.api.v1.styles import router as styles_router
from app.api.v1.workflows import router as workflows_router

v1_router = APIRouter()
v1_router.include_router(workflows_router)
v1_router.include_router(comfy_router)
v1_router.include_router(projects_router)
v1_router.include_router(stream_router)
v1_router.include_router(search_router)
v1_router.include_router(styles_router)
v1_router.include_router(cleanup_router)
v1_router.include_router(images_router)
