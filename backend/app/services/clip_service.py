"""app/services/clip_service.py — CLIP 编码 + Qdrant 检索/写入

模式移植自 demo-food-discovery（SentenceTransformer + QdrantClient），exclude 语义用
must_not + payload 过滤（业务 id 存 payload.style_image_id，点 id 用独立 uuid）。

CLIP 模型与同步 QdrantClient 都是重资源：模型懒加载一次；所有阻塞调用经
run_in_threadpool 包成 async，避免卡事件循环。
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.utils.logger import logger

if settings.HF_ENDPOINT:
    # 须在 sentence_transformers 导入前生效
    os.environ.setdefault("HF_ENDPOINT", settings.HF_ENDPOINT)


class ClipService:
    def __init__(self) -> None:
        self._model: Any = None
        self._model_lock = threading.Lock()
        self._qdrant: Any = None

    # ---- 重资源（同步，线程池内调用） ----

    def _get_model(self):
        with self._model_lock:
            if self._model is None:
                from sentence_transformers import SentenceTransformer

                logger.info("加载 CLIP 模型 %s ...", settings.CLIP_MODEL_NAME)
                self._model = SentenceTransformer(settings.CLIP_MODEL_NAME)
            return self._model

    def _get_qdrant(self):
        if self._qdrant is None:
            from qdrant_client import QdrantClient

            self._qdrant = QdrantClient(url=settings.QDRANT_URL, timeout=10)
        return self._qdrant

    def _ensure_collection(self) -> None:
        from qdrant_client import models

        client = self._get_qdrant()
        name = settings.QDRANT_COLLECTION
        if not client.collection_exists(name):
            client.create_collection(
                collection_name=name,
                vectors_config=models.VectorParams(
                    size=settings.CLIP_VECTOR_DIM, distance=models.Distance.COSINE
                ),
            )
            logger.info("Qdrant collection 已创建: %s (%d 维 cosine)", name, settings.CLIP_VECTOR_DIM)

    # ---- 编码 ----

    def _encode_text(self, text: str) -> list[float]:
        return self._get_model().encode([text])[0].tolist()

    def _encode_image(self, path: Path) -> list[float]:
        from PIL import Image

        with Image.open(path) as img:
            return self._get_model().encode([img.convert("RGB")])[0].tolist()

    def _encode_images(self, paths: list[Path]) -> list[list[float]]:
        """批量图像编码（单次 forward，远快于逐张）。"""
        from PIL import Image

        images = []
        for p in paths:
            with Image.open(p) as img:
                images.append(img.convert("RGB"))
        return [v.tolist() for v in self._get_model().encode(images)]

    # ---- 检索 / 写入（同步实现，供线程池调用） ----

    def _search(self, query_text: str, exclude_ids: list[str], limit: int) -> list[dict[str, Any]]:
        from qdrant_client import models

        self._ensure_collection()
        vector = self._encode_text(query_text)
        query_filter = None
        if exclude_ids:
            query_filter = models.Filter(
                must_not=[
                    models.FieldCondition(
                        key="style_image_id",
                        match=models.MatchAny(any=exclude_ids),
                    )
                ]
            )
        result = self._get_qdrant().query_points(
            collection_name=settings.QDRANT_COLLECTION,
            query=vector,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
        return [
            {
                "id": p.payload.get("style_image_id"),
                "image_url": f"/api/images/{p.payload.get('file_path')}",
                "score": round(p.score, 4),
            }
            for p in result.points
        ]

    def _upsert(self, style_image_id: str, file_ref: str, vector: list[float]) -> str:
        """写入一个风格图向量，返回 qdrant_point_id。"""
        from qdrant_client import models

        self._ensure_collection()
        point_id = uuid.uuid4().hex
        self._get_qdrant().upsert(
            collection_name=settings.QDRANT_COLLECTION,
            wait=True,
            points=[
                models.PointStruct(
                    id=point_id,
                    vector=vector,
                    payload={
                        "style_image_id": style_image_id,
                        "file_path": file_ref,
                        "created_ts": int(time.time()),
                    },
                )
            ],
        )
        return point_id

    def _upsert_many(self, items: list[tuple[str, str, list[float]]]) -> list[str]:
        """批量写入 (style_image_id, file_ref, vector)，返回 point_id 列表（单次请求）。"""
        from qdrant_client import models

        if not items:
            return []
        self._ensure_collection()
        point_ids = [uuid.uuid4().hex for _ in items]
        self._get_qdrant().upsert(
            collection_name=settings.QDRANT_COLLECTION,
            wait=True,
            points=[
                models.PointStruct(
                    id=pid,
                    vector=vector,
                    payload={
                        "style_image_id": sid,
                        "file_path": ref,
                        "created_ts": int(time.time()),
                    },
                )
                for pid, (sid, ref, vector) in zip(point_ids, items)
            ],
        )
        return point_ids

    def _delete_points(self, style_image_ids: list[str]) -> None:
        from qdrant_client import models

        self._get_qdrant().delete(
            collection_name=settings.QDRANT_COLLECTION,
            wait=True,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="style_image_id",
                            match=models.MatchAny(any=style_image_ids),
                        )
                    ]
                )
            ),
        )

    # ---- async 包装 ----

    async def encode_text(self, text: str) -> list[float]:
        return await run_in_threadpool(self._encode_text, text)

    async def encode_image(self, path: Path) -> list[float]:
        return await run_in_threadpool(self._encode_image, path)

    async def encode_images(self, paths: list[Path]) -> list[list[float]]:
        return await run_in_threadpool(self._encode_images, paths)

    async def search_styles(
        self, query_text: str, exclude_ids: list[str] | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        return await run_in_threadpool(
            self._search,
            query_text,
            exclude_ids or [],
            limit or settings.SEARCH_CANDIDATE_LIMIT,
        )

    async def upsert_style(self, style_image_id: str, file_ref: str, vector: list[float]) -> str:
        return await run_in_threadpool(self._upsert, style_image_id, file_ref, vector)

    async def upsert_styles(self, items: list[tuple[str, str, list[float]]]) -> list[str]:
        return await run_in_threadpool(self._upsert_many, items)

    async def delete_styles(self, style_image_ids: list[str]) -> None:
        return await run_in_threadpool(self._delete_points, style_image_ids)

    async def ensure_collection(self) -> None:
        await run_in_threadpool(self._ensure_collection)


_clip_service: ClipService | None = None
_service_lock = asyncio.Lock()


async def get_clip_service() -> ClipService:
    global _clip_service
    if _clip_service is None:
        async with _service_lock:
            if _clip_service is None:
                _clip_service = ClipService()
    return _clip_service
