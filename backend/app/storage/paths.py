"""app/storage/paths.py — uploads / outputs 唯一磁盘路径访问层

所有业务通过本模块取路径，禁止散写 Path(...) / "data"。
目录根由 .env 的 UPLOAD_DIR / OUTPUT_DIR 决定（相对路径相对 backend/ 解析）。
"""

from __future__ import annotations

from pathlib import Path

from app.config import settings


def uploads_dir() -> Path:
    """用户上传（实拍图、风格库图片）根目录。"""
    d = settings.resolve_dir(settings.UPLOAD_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def outputs_dir() -> Path:
    """ComfyUI 产物根目录。"""
    d = settings.resolve_dir(settings.OUTPUT_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_outputs_dir(project_id: str) -> Path:
    """单个项目的产物目录（线稿/深度图/效果图），outputs/{project_id}/。"""
    d = outputs_dir() / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def style_images_dir() -> Path:
    """风格库图片落盘目录，uploads/styles/。"""
    d = uploads_dir() / "styles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_storage_dirs() -> None:
    uploads_dir()
    outputs_dir()
    style_images_dir()


def project_upload_dir(project_id: str) -> Path:
    """单个项目的上传目录（实拍图原图），uploads/projects/{project_id}/。"""
    d = uploads_dir() / "projects" / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def to_image_ref(path: Path) -> str:
    """绝对路径 → 图片引用（'outputs/...' 或 'uploads/...' 相对串），供 /api/images/{ref} 访问。"""
    path = Path(path).resolve()
    for name, root in (("outputs", outputs_dir()), ("uploads", uploads_dir())):
        root = root.resolve()
        if path.is_relative_to(root):
            return f"{name}/{path.relative_to(root).as_posix()}"
    raise ValueError(f"路径不在 uploads/outputs 根内: {path}")


def resolve_image_ref(ref: str) -> Path | None:
    """图片引用 → 磁盘路径（防路径逃逸；不在 uploads/outputs 根内返回 None）。"""
    p = Path(ref)
    if p.is_absolute() or p.drive or ".." in p.parts:
        return None
    parts = p.parts
    if len(parts) < 2:
        return None
    roots = {"uploads": uploads_dir, "outputs": outputs_dir}
    root_fn = roots.get(parts[0])
    if root_fn is None:
        return None
    candidate = (root_fn() / Path(*parts[1:])).resolve()
    return candidate if candidate.is_relative_to(root_fn().resolve()) and candidate.is_file() else None
