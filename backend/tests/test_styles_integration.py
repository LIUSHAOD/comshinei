"""tests/test_styles_integration.py — M4 验收：批量上传 50 张图后检索返回合理近邻

流程：TestClient 经真 HTTP 上传 50 张不同色系的合成风格图（真 CLIP 编码 + 真 Qdrant），
然后用真实检索验证近邻合理性：查红色系，top-1 必须落在红色家族。

注意：查询词用英文——clip-ViT-B-32 是英文模型，中文查询语义近邻质量差（计划 §11 已列
风险与对策：预留换 Chinese-CLIP，改编码模型与 collection 即可）。实测：同一批合成图，
英文 "red modern living room" top-1 命中红色（0.227 vs 次名 0.175），中文查询不排序。

前置：Qdrant 可达（QDRANT_URL）；CLIP 模型已缓存（首次需 HF_ENDPOINT 镜像下载）。
运行：uv run pytest -m integration -v -k styles
"""

import struct
import zlib

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.storage.paths import style_images_dir

pytestmark = pytest.mark.integration

_TEST_COLLECTION = "interior_styles_m4_test"

# 5 个色系 × 10 个明度变体 = 50 张
_FAMILIES = {
    "red": [(180 + i * 5, 40 + i * 3, 40 + i * 3) for i in range(10)],
    "blue": [(40 + i * 3, 40 + i * 3, 180 + i * 5) for i in range(10)],
    "green": [(40 + i * 3, 150 + i * 5, 40 + i * 3) for i in range(10)],
    "wood": [(150 + i * 5, 110 + i * 4, 60 + i * 2) for i in range(10)],
    "gray": [(60 + i * 16, 60 + i * 16, 60 + i * 16) for i in range(10)],
}


def _png(rgb: tuple[int, int, int], size: int = 224) -> bytes:
    def clamp(v):
        return max(0, min(255, v))

    row = b"\x00" + bytes(clamp(c) for c in rgb) * size

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * size, 9))
        + chunk(b"IEND", b"")
    )


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_upload_50_and_search_neighbors(client, monkeypatch):
    import httpx

    try:
        r = httpx.get(f"{settings.QDRANT_URL}/collections", timeout=5)
        assert r.status_code == 200
    except Exception:
        pytest.skip("Qdrant 不可达")

    monkeypatch.setattr(settings, "QDRANT_COLLECTION", _TEST_COLLECTION)
    from app.services.clip_service import get_clip_service

    # ---- 批量上传 50 张 ----
    files = []
    for family, variants in _FAMILIES.items():
        for i, rgb in enumerate(variants):
            files.append(("files", (f"{family}_{i}.png", _png(rgb), "image/png")))
    assert len(files) == 50

    resp = client.post("/api/styles", files=files)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["imported"] == 50, data["failed"][:3]
    assert data["failed"] == []

    # ---- 真实检索：红色系查询，top-1 必须是红色家族 ----
    async def _search():
        clip = await get_clip_service()
        return await clip.search_styles("red modern living room, warm lighting", limit=10)

    import asyncio

    results = asyncio.run(_search())

    assert results, "检索无结果"
    top1_name = results[0]["image_url"]
    assert "red_" in top1_name, f"top-1 不是红色家族: {[(r['image_url'], r['score']) for r in results[:5]]}"

    # 蓝色查询 top-1 应是蓝色家族
    results_blue = asyncio.run(_search_blue())
    assert "blue_" in results_blue[0]["image_url"], f"蓝色 top-1 异常: {[r['image_url'] for r in results_blue[:5]]}"

    # ---- 清理：走 DELETE API 逐张删除（行 + Qdrant 点 + 文件一并清，顺带覆盖删除路径） ----
    for item in data["items"]:
        resp = client.delete(f"/api/styles/{item['id']}")
        assert resp.status_code == 200

    async def _verify_empty():
        import httpx

        clip = await get_clip_service()
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{settings.QDRANT_URL}/collections/{_TEST_COLLECTION}/points/count", json={}
            )
            assert r.json()["result"]["count"] == 0, "Qdrant 点未删净"
        clip._get_qdrant().delete_collection(_TEST_COLLECTION)

    asyncio.run(_verify_empty())
    for item in data["items"]:
        p = style_images_dir() / item["image_url"].rsplit("/", 1)[-1]
        assert not p.exists(), f"文件未删除: {p}"


async def _search_blue():
    from app.services.clip_service import get_clip_service

    clip = await get_clip_service()
    return await clip.search_styles("blue living room", limit=10)
