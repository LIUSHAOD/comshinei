"""tests/test_styles.py — 风格库端点单测（mock CLIP，无需外部服务）"""

import struct
import zlib

import pytest
from fastapi.testclient import TestClient

import app.api.v1.styles as styles_module
from app.main import app
from app.models.style_image import StyleImage
from app.storage.paths import style_images_dir


def _png_bytes(rgb: tuple[int, int, int], size: int = 16) -> bytes:
    row = b"\x00" + bytes(rgb) * size

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * size, 9))
        + chunk(b"IEND", b"")
    )


class FakeClipService:
    def __init__(self, *, upsert_fails: bool = False):
        self.upsert_fails = upsert_fails
        self.deleted_ids: list[list[str]] = []

    async def encode_images(self, paths):
        return [[0.1] * 512 for _ in paths]

    async def upsert_styles(self, items):
        if self.upsert_fails:
            raise RuntimeError("Qdrant 抖动")
        return [f"point_{i}" for i in range(len(items))]

    async def delete_styles(self, ids):
        self.deleted_ids.append(list(ids))


@pytest.fixture
def fake_clip(monkeypatch):
    clip = FakeClipService()

    async def _get():
        return clip

    monkeypatch.setattr(styles_module, "get_clip_service", _get)
    return clip


@pytest.fixture
def client(fake_clip):
    with TestClient(app) as c:
        yield c


class TestUpload:
    def test_batch_upload_partial_failure(self, client, db_session):
        files = [
            ("files", ("a.png", _png_bytes((200, 50, 50)), "image/png")),
            ("files", ("b.png", _png_bytes((50, 50, 200)), "image/png")),
            ("files", ("readme.txt", b"not an image", "text/plain")),
        ]
        resp = client.post("/api/styles", files=files)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["imported"] == 2
        assert len(data["failed"]) == 1 and data["failed"][0]["filename"] == "readme.txt"
        assert all(item["image_url"].startswith("/api/images/uploads/styles/") for item in data["items"])

        # MySQL 元数据落行
        ids = [item["id"] for item in data["items"]]
        rows = db_session.query(StyleImage).filter(StyleImage.id.in_(ids)).all()
        assert len(rows) == 2
        assert all(r.qdrant_point_id.startswith("point_") for r in rows)
        assert all(r.source == "upload" for r in rows)

    def test_upload_no_files_rejected(self, client):
        resp = client.post("/api/styles", files=[])
        assert resp.status_code == 422  # FastAPI 对空文件列表直接参数校验失败


class TestCompensation:
    """失败逆序补偿：任何一步失败都不留孤儿文件/孤儿向量（review #1）。"""

    def test_upsert_failure_cleans_files(self, fake_clip, client):
        fake_clip.upsert_fails = True
        before = set(style_images_dir().iterdir())
        resp = client.post(
            "/api/styles",
            files=[("files", ("a.png", _png_bytes((1, 2, 3)), "image/png"))],
        )
        assert resp.status_code == 502
        assert "Qdrant 写入失败" in resp.json()["message"]
        assert set(style_images_dir().iterdir()) == before  # 无孤儿文件

    def test_persist_failure_deletes_points_and_files(self, fake_clip, monkeypatch):
        from app.db.session import get_db as real_get_db

        class FailingSession:
            def add(self, obj):
                pass

            def commit(self):
                raise RuntimeError("db down")

            def scalars(self, *a, **k):
                raise RuntimeError("db down")

        app.dependency_overrides[real_get_db] = lambda: FailingSession()
        try:
            with TestClient(app) as c:
                before = set(style_images_dir().iterdir())
                resp = c.post(
                    "/api/styles",
                    files=[("files", ("a.png", _png_bytes((4, 5, 6)), "image/png"))],
                )
            assert resp.status_code == 500
            assert "元数据落库失败" in resp.json()["message"]
            # 补偿：Qdrant 点被删（delete_styles 收到全部 id）+ 文件被清
            assert len(fake_clip.deleted_ids) == 1
            assert len(fake_clip.deleted_ids[0]) == 1
            assert set(style_images_dir().iterdir()) == before
        finally:
            app.dependency_overrides.clear()

    def test_oversize_file_rejected(self, client, monkeypatch):
        monkeypatch.setattr(styles_module, "_MAX_FILE_MB", 0)  # 阈值归零 → 任何文件都超
        resp = client.post(
            "/api/styles",
            files=[("files", ("big.png", _png_bytes((7, 8, 9)), "image/png"))],
        )
        data = resp.json()["data"]
        assert data["imported"] == 0
        assert "超过单文件上限" in data["failed"][0]["reason"]

    def test_batch_cap_rejected(self, client, monkeypatch):
        monkeypatch.setattr(styles_module, "_MAX_FILES_PER_BATCH", 1)
        files = [("files", (f"{n}.png", _png_bytes((1, 1, 1)), "image/png")) for n in ("a", "b")]
        resp = client.post("/api/styles", files=files)
        assert resp.status_code == 400
        assert "单批次最多" in resp.json()["message"]


class TestListAndDelete:
    def test_list_and_delete(self, client, db_session):
        files = [("files", (f"{name}.png", _png_bytes((10, 20, 30)), "image/png")) for name in ("x1", "x2", "x3")]
        data = client.post("/api/styles", files=files).json()["data"]
        ids = [item["id"] for item in data["items"]]

        lst = client.get("/api/styles").json()["data"]
        assert lst["total"] >= 3
        listed_ids = {r["id"] for r in lst["records"]}
        assert set(ids) <= listed_ids

        # 删除：行 + 文件 +（mock 的）Qdrant 点
        first_file = style_images_dir() / data["items"][0]["image_url"].rsplit("/", 1)[-1]
        assert first_file.exists()
        resp = client.delete(f"/api/styles/{ids[0]}")
        assert resp.status_code == 200
        assert not first_file.exists()
        assert db_session.get(StyleImage, ids[0]) is None

        # 再删一次 → 404
        assert client.delete(f"/api/styles/{ids[0]}").status_code == 404
