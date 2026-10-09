"""app/api/v1/styles.py — 风格库管理：批量上传 / 列表 / 删除

上传流程（入库即向量化，计划 §1/§6）：multipart 多文件 → 落盘 uploads/styles/
→ CLIP 批量编码 → Qdrant 批量 upsert（payload: style_image_id + file_path）
→ style_images 元数据落行。逐文件容错，部分失败不影响其余。

一致性约定：检索读 Qdrant、管理读 MySQL，**不允许出现无元数据行的孤儿向量**。
任何一步失败按写入的相反顺序补偿（后写的先回滚），不留无管理入口的脏数据。
"""

import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.db.session import get_db
from app.models.style_image import StyleImage
from app.repositories.style_image_repository import StyleImageRepository
from app.services.clip_service import get_clip_service
from app.storage.paths import resolve_image_ref, style_images_dir
from app.utils.logger import logger
from app.utils.response import success

router = APIRouter(prefix="/styles", tags=["styles"])

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}
_MAX_FILE_MB = 10          # 单文件上限
_MAX_FILES_PER_BATCH = 50  # 单批次上限（控制内存与编码时长）


def _serialize(row: StyleImage) -> dict:
    return {
        "id": row.id,
        "image_url": f"/api/images/{row.file_path}",
        "source": row.source,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _safe_name(filename: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", filename) or "image"


def _unlink_all(paths: list[Path]) -> None:
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("补偿清理文件失败 %s: %s", p, exc)


@router.get("")
async def list_styles(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    repo = StyleImageRepository(db)
    rows, total = await run_in_threadpool(
        lambda: (repo.list_active(skip=skip, limit=limit), repo.count_active())
    )
    return success({"total": total, "records": [_serialize(r) for r in rows]})


@router.post("")
async def upload_styles(
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """批量上传：逐文件校验 → 落盘 → CLIP 批量编码 → Qdrant → MySQL（失败逆序补偿）。"""
    if not files:
        raise HTTPException(status_code=400, detail="未收到文件")
    if len(files) > _MAX_FILES_PER_BATCH:
        raise HTTPException(status_code=400, detail=f"单批次最多 {_MAX_FILES_PER_BATCH} 张，当前 {len(files)} 张")

    # 1) 校验 + 读入内存
    accepted: list[tuple[str, bytes]] = []
    failed: list[dict] = []
    for f in files:
        name = f.filename or "unnamed"
        suffix = Path(name).suffix.lower()
        if suffix not in _IMAGE_SUFFIXES:
            failed.append({"filename": name, "reason": f"不支持的格式: {suffix or '无扩展名'}"})
            continue
        data = await f.read()
        if not data:
            failed.append({"filename": name, "reason": "空文件"})
            continue
        if len(data) > _MAX_FILE_MB * 1024 * 1024:
            failed.append({"filename": name, "reason": f"超过单文件上限 {_MAX_FILE_MB}MB"})
            continue
        accepted.append((name, data))

    imported: list[dict] = []
    if accepted:
        clip = await get_clip_service()

        # 2) 落盘（uuid 前缀防重名覆盖）
        saved: list[tuple[str, str, Path]] = []  # (style_id, file_ref, abs_path)
        for name, data in accepted:
            style_id = f"style_{uuid.uuid4().hex}"
            filename = f"{uuid.uuid4().hex[:8]}_{_safe_name(name)}"
            path = style_images_dir() / filename
            path.write_bytes(data)
            saved.append((style_id, f"uploads/styles/{filename}", path))

        paths = [p for _, _, p in saved]

        # 3) 批量编码（失败补偿：删文件）
        try:
            vectors = await clip.encode_images(paths)
        except Exception as exc:  # noqa: BLE001
            _unlink_all(paths)
            raise HTTPException(status_code=500, detail=f"CLIP 编码失败: {exc}")

        # 4) Qdrant 批量 upsert（失败补偿：删文件）
        items = [(sid, ref, vec) for (sid, ref, _), vec in zip(saved, vectors)]
        try:
            point_ids = await clip.upsert_styles(items)
        except Exception as exc:  # noqa: BLE001
            _unlink_all(paths)
            raise HTTPException(status_code=502, detail=f"Qdrant 写入失败: {exc}")

        # 5) MySQL 元数据落行（失败补偿：删 Qdrant 点 + 删文件，杜绝无管理入口的孤儿向量；
        #    commit 后 ORM 属性过期，序列化必须在线程池内完成）
        def _persist() -> list[dict]:
            sids = [sid for sid, _, _ in saved]
            for (sid, ref, _), point_id in zip(saved, point_ids):
                db.add(StyleImage(id=sid, qdrant_point_id=point_id, file_path=ref, source="upload"))
            db.commit()
            # commit 后属性过期：单次 SELECT 重新加载（本函数全程在线程池内，I/O 合法），
            # 避免把刷新查询漏到事件循环线程
            rows = db.scalars(select(StyleImage).where(StyleImage.id.in_(sids))).all()
            by_id = {r.id: r for r in rows}
            return [_serialize(by_id[sid]) for sid in sids]

        try:
            imported = await run_in_threadpool(_persist)
        except Exception as exc:  # noqa: BLE001
            try:
                await clip.delete_styles([sid for sid, _, _ in saved])
            except Exception:  # noqa: BLE001
                logger.exception("补偿删除 Qdrant 点失败: %s", [sid for sid, _, _ in saved])
            _unlink_all(paths)
            raise HTTPException(status_code=500, detail=f"元数据落库失败: {exc}")

    return success(
        {
            "imported": len(imported),
            "failed": failed,
            "items": imported,
        },
        message="ok" if not failed else "partial",
    )


@router.delete("/{style_id}")
async def delete_style(style_id: str, db: Session = Depends(get_db)):
    """删除风格图：Qdrant 点 → 文件 → 元数据行。

    行是点的唯一管理入口：Qdrant 删不动就中止报错（行保留可重试），
    避免留下「列表可见但永远搜不到」或「搜得到但删不掉」的不一致。
    """
    repo = StyleImageRepository(db)
    row = await run_in_threadpool(repo.get, style_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"风格图不存在: {style_id}")
    file_ref = row.file_path

    clip = await get_clip_service()
    try:
        await clip.delete_styles([style_id])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Qdrant 点删除失败（数据未动，可重试）: {exc}")

    fp = resolve_image_ref(file_ref)
    if fp:
        try:
            fp.unlink(missing_ok=True)
        except OSError as exc:  # 文件删不动不阻断（点已删、行将删，孤儿文件由清理服务兜底）
            logger.warning("风格图文件删除失败 %s: %s", fp, exc)

    await run_in_threadpool(repo.remove, style_id)
    return success(message="deleted")
