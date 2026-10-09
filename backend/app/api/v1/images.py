"""app/api/v1/images.py — 静态图片访问（ref 形如 outputs/... 或 uploads/...）"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.storage.paths import resolve_image_ref

router = APIRouter(prefix="/images", tags=["images"])


@router.get("/{ref:path}")
def get_image(ref: str):
    path = resolve_image_ref(ref)
    if path is None:
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(path)
