"""tests/test_stage2_integration.py — M5 验收：confirm 续跑出效果图，状态可恢复

链路：真 stage1（ComfyUI lineart + CLIP 检索）→ confirm（mock 掉 LLM 的 HTTP 调用，
其余全真：真阶段 2 图、真 ComfyUI BrushNet 生图）→ stage=done + 效果图落盘 +
恢复字段（result_path/prompt/negative_prompt）回读。

LLM 仅 mock 网络层（_raw_chat），提示词解析与注入全真。
运行：uv run pytest -m integration -v -k stage2
"""

import asyncio
import struct
import zlib

import pytest

from app.config import settings
from app.models.project import Project
from app.runtime import store
from app.runtime.graph import get_stage1_graph, get_stage2_graph
from app.runtime.runner import GraphRunner
from app.runtime.state import is_terminal_event
from app.services.comfy import ComfyUIRunner
from app.services.prompt_service import PromptService
from app.storage.paths import outputs_dir, style_images_dir

from test_stage1_integration import _service_available, _wait_terminal

pytestmark = pytest.mark.integration

_TEST_COLLECTION = "interior_styles_m5_test"


def _solid_png(path, rgb, size=224):
    row = b"\x00" + bytes(rgb) * size

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * size, 9))
        + chunk(b"IEND", b"")
    )


async def test_confirm_then_done_and_recover(
    monkeypatch, tmp_path, db_session, test_photo, lineart_workflow, generate_workflow
):
    ok, reason = await _service_available()
    if not ok:
        pytest.skip(reason)

    # ---- 种子风格图（3 张足够产生候选） ----
    monkeypatch.setattr(settings, "QDRANT_COLLECTION", _TEST_COLLECTION)
    from app.services.clip_service import ClipService

    clip = ClipService()
    client = clip._get_qdrant()
    if client.collection_exists(_TEST_COLLECTION):
        client.delete_collection(_TEST_COLLECTION)
    await clip.ensure_collection()
    for name, rgb in [("red", (180, 60, 60)), ("wood", (160, 120, 70)), ("blue", (60, 60, 180))]:
        target = style_images_dir() / f"m5test_{name}.png"
        _solid_png(target, rgb)
        vector = await clip.encode_image(target)
        await clip.upsert_style(f"style_m5_{name}", f"uploads/styles/{target.name}", vector)

    # ---- mock LLM 网络层（提示词确定，便于断言注入） ----
    async def fake_raw_chat(self, messages):
        return '{"prompt": "red modern living room, warm lighting, photorealistic", "negative_prompt": "lowres, watermark"}'

    monkeypatch.setattr(PromptService, "_raw_chat", fake_raw_chat)

    # ---- 真 stage1 ----
    pid = "itest_stage2"
    redis = await store.get_redis()
    for key in (f"sse:{pid}", f"sse_seq:{pid}", f"job:{pid}", f"exclude:{pid}"):
        await redis.delete(key)
    db_session.merge(Project(id=pid, requirements="红色现代客厅", stage="created", photo_path=str(test_photo)))
    db_session.commit()

    runner = GraphRunner(get_stage1_graph(), get_stage2_graph())
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
    assert events[-1]["type"] == "completed", [e["type"] for e in events]

    job = await store.get_job(pid)
    candidates = job.get("candidates") or []
    assert candidates, "阶段 1 无候选"
    selected = candidates[0]

    # ---- confirm：真阶段 2（mock LLM + 真 ComfyUI） ----
    task = await runner.start_stage2(
        project_id=pid,
        photo_path=str(test_photo),
        requirements="红色现代客厅",
        selected_ref=selected,
        lineart_url=job["lineart_url"],
        mlsd_url=job["mlsd_url"],
        depth_url=job["depth_url"],
        generate_workflow=generate_workflow,
        output_dir=outputs_dir() / "itest" / pid,
        comfy_runner=ComfyUIRunner.from_settings(),
        prompt_service=PromptService(),
    )
    await task
    events = await _wait_terminal(pid, timeout=420)

    types = [e["type"] for e in events]
    assert "image_done" in types, f"缺 image_done: {types}"
    assert events[-1]["type"] == "completed", f"终止异常: {types}"
    assert is_terminal_event(events[-1])

    # 效果图真实落盘且非空
    image_done = next(e for e in events if e["type"] == "image_done")["data"]
    assert image_done["result_url"].startswith("/api/images/outputs/")
    result_files = [p for p in (outputs_dir() / "itest" / pid).glob("效果图_*.png")]
    assert result_files and all(p.stat().st_size > 0 for p in result_files)

    # ---- 状态恢复字段：projects 行 ----
    db_session.expire_all()
    row = db_session.get(Project, pid)
    assert row.stage == "done"
    assert row.result_path.startswith("outputs/")
    assert row.prompt == "red modern living room, warm lighting, photorealistic"
    assert row.negative_prompt == "lowres, watermark"

    # ---- 清理 ----
    client.delete_collection(_TEST_COLLECTION)
    for p in style_images_dir().glob("m5test_*.png"):
        p.unlink(missing_ok=True)
