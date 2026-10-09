"""tests/test_stage1_integration.py — 阶段 1 全链集成测试（真 ComfyUI + Qdrant + Redis + CLIP）

前置：ComfyUI (COMFYUI_HOST)、Qdrant (QDRANT_URL)、Redis (REDIS_URL) 均可达；
CLIP 模型首次运行需下载（约 350MB，走 HF_ENDPOINT 镜像）。

流程：种子风格图入库 → start_stage1（真 ComfyUI 跑 lineart）→ 轮询事件流直到终止 →
校验 lineart_done/candidates/completed → requery 校验 exclude 语义（新候选与旧的不相交）。

运行：uv run pytest -m integration -v -k stage1
"""

import asyncio
import struct
import zlib
from pathlib import Path

import pytest

from app.config import settings
from app.runtime import store
from app.runtime.graph import build_stage1_graph
from app.runtime.runner import GraphRunner
from app.runtime.state import is_terminal_event
from app.services.comfy import ComfyUIRunner
from app.storage.paths import outputs_dir, style_images_dir

pytestmark = pytest.mark.integration

_TEST_COLLECTION = "interior_styles_test"


def _solid_png(path: Path, rgb: tuple[int, int, int], size: int = 224) -> None:
    """纯 stdlib 生成纯色 PNG（种子风格图）。"""
    row = b"\x00" + bytes(rgb) * size
    raw = row * size

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


async def _service_available() -> tuple[bool, str]:
    import httpx

    if not await ComfyUIRunner.from_settings().ping():
        return False, "ComfyUI 不可达"
    try:
        redis = await store.get_redis()
        await redis.ping()
    except Exception as exc:
        # 打印真实异常再 skip（曾有全局客户端跨事件循环复用被静默吞掉的教训）
        print(f"[service-check] Redis ping 失败: {type(exc).__name__}: {exc}")
        return False, f"Redis 不可达: {exc}"
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{settings.QDRANT_URL}/collections")
            if r.status_code != 200:
                return False, "Qdrant 不可达"
    except Exception as exc:
        print(f"[service-check] Qdrant 访问失败: {type(exc).__name__}: {exc}")
        return False, f"Qdrant 不可达: {exc}"
    return True, ""


@pytest.fixture
async def seeded_styles(monkeypatch, tmp_path):
    """10 张不同色系的种子风格图：CLIP 编码 → Qdrant 测试 collection。"""
    ok, reason = await _service_available()
    if not ok:
        pytest.skip(reason)

    monkeypatch.setattr(settings, "QDRANT_COLLECTION", _TEST_COLLECTION)
    from app.services.clip_service import ClipService

    clip = ClipService()
    # 全新测试 collection
    client = clip._get_qdrant()
    if client.collection_exists(_TEST_COLLECTION):
        client.delete_collection(_TEST_COLLECTION)
    await clip.ensure_collection()

    colors = [
        ("red", (180, 60, 60)), ("blue", (60, 60, 180)), ("green", (60, 150, 60)),
        ("wood", (160, 120, 70)), ("gray", (120, 120, 120)), ("white", (235, 235, 235)),
        ("black", (30, 30, 30)), ("beige", (210, 190, 160)), ("navy", (40, 50, 100)),
        ("olive", (120, 130, 70)),
    ]
    style_ids = []
    for name, rgb in colors:
        img = tmp_path / f"{name}.png"
        _solid_png(img, rgb)
        target = style_images_dir() / f"itest_{name}.png"
        target.write_bytes(img.read_bytes())
        style_id = f"style_itest_{name}"
        vector = await clip.encode_image(target)
        await clip.upsert_style(style_id, f"uploads/styles/{target.name}", vector)
        style_ids.append(style_id)

    yield clip, style_ids

    client.delete_collection(_TEST_COLLECTION)


async def _wait_terminal(pid: str, timeout: float = 300.0):
    elapsed = 0.0
    while elapsed < timeout:
        events = await store.get_events(pid)
        if events and is_terminal_event(events[-1]):
            return events
        await asyncio.sleep(1.0)
        elapsed += 1.0
    raise TimeoutError(f"等待项目 {pid} 终止事件超时")


async def test_stage1_full_chain(seeded_styles, test_photo, lineart_workflow):
    clip, style_ids = seeded_styles
    pid = "itest_stage1"

    # 清掉可能的残留
    redis = await store.get_redis()
    for key in (f"sse:{pid}", f"sse_seq:{pid}", f"job:{pid}", f"exclude:{pid}"):
        await redis.delete(key)

    runner = GraphRunner(build_stage1_graph())
    task = await runner.start_stage1(
        project_id=pid,
        photo_path=str(test_photo),
        requirements="红色现代客厅",
        lineart_workflow=lineart_workflow,
        output_dir=outputs_dir() / "itest" / pid,
        comfy_runner=ComfyUIRunner.from_settings(),
        clip_service=clip,
    )
    await task
    events = await _wait_terminal(pid)

    types = [e["type"] for e in events]
    assert types[0] == "stage_change"
    assert "lineart_done" in types, f"事件流缺 lineart_done: {types}"
    assert "candidates" in types, f"事件流缺 candidates: {types}"
    assert types[-1] == "completed", f"终止事件异常: {types}"

    # 线稿产物真实落盘
    lineart = next(e for e in events if e["type"] == "lineart_done")["data"]
    assert lineart["lineart_url"].startswith("/api/images/outputs/")
    out_files = list((outputs_dir() / "itest" / pid).glob("*.png"))
    assert len(out_files) >= 4  # 线稿/结构线/深度图/分割图

    # 候选非空且在种子集内
    candidates = next(e for e in events if e["type"] == "candidates")["data"]["candidates"]
    assert candidates, "候选为空"
    assert all(c["id"] in style_ids for c in candidates)

    # ---- requery：exclude 累加 → 新候选与旧的不相交 ----
    old_ids = [c["id"] for c in candidates]
    await store.add_excludes(pid, old_ids)
    task = await runner.requery(project_id=pid, requirements="红色现代客厅", clip_service=clip)
    await task

    events = await store.get_events(pid)
    cand_events = [e for e in events if e["type"] == "candidates"]
    assert len(cand_events) == 2
    new_ids = [c["id"] for c in cand_events[-1]["data"]["candidates"]]
    assert not set(new_ids) & set(old_ids), f"requery 候选与已看重叠: {new_ids} vs {old_ids}"
