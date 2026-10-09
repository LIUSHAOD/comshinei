"""tests/test_graph.py — 阶段 1 图单测（mock ComfyUI / CLIP，无外部依赖）"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime.graph import build_stage1_graph
from app.storage.paths import outputs_dir


class FakeComfyRunner:
    def __init__(self, success: bool = True):
        self.success = success

    async def run(self, workflow, output_dir, outputs_spec=None, on_progress=None):
        if not self.success:
            return SimpleNamespace(success=False, outputs={}, error="ComfyUI 炸了", prompt_id=None)
        outputs = {}
        for spec in outputs_spec or []:
            p = Path(output_dir) / f"{spec['key']}_fake.png"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\x89PNG\r\n\x1a\n")
            outputs[spec["key"]] = p
        if on_progress:
            await on_progress(100, "done")
        return SimpleNamespace(success=True, outputs=outputs, error=None, prompt_id="fake-pid")


class FakeClipService:
    def __init__(self, candidates=None, fail: bool = False):
        self._candidates = candidates
        self._fail = fail

    async def search_styles(self, text, exclude_ids=None, limit=None):
        if self._fail:
            raise RuntimeError("Qdrant 不可达")
        if self._candidates is not None:
            return self._candidates
        return [{"id": "style_a", "image_url": "/api/images/uploads/styles/a.png", "score": 0.9}]


def _make_state(test_photo, events):
    async def emit(event_type, data):
        events.append((event_type, data))

    return {
        "project_id": "proj_test",
        "photo_path": str(test_photo),
        "requirements": "北欧风客厅",
        "exclude_ids": [],
        "emit": emit,
    }


def _make_config(comfy, clip, lineart_workflow, tmp_path):
    return {
        "configurable": {
            "comfy_runner": comfy,
            "clip_service": clip,
            "lineart_workflow": lineart_workflow,
            "output_dir": str(outputs_dir() / "test_graph" / "unit"),
            "candidate_limit": 8,
        }
    }


async def test_happy_path(test_photo, lineart_workflow):
    events = []
    graph = build_stage1_graph()
    result = await graph.ainvoke(
        _make_state(test_photo, events),
        config=_make_config(FakeComfyRunner(), FakeClipService(), lineart_workflow, None),
    )

    assert result["stage"] == "selecting"
    assert result["lineart_url"].startswith("/api/images/outputs/")
    assert result["depth_url"] and result["mlsd_url"]
    assert result["candidates"][0]["id"] == "style_a"

    types = [t for t, _ in events]
    assert types[0] == "stage_change"          # parse 先行
    assert "lineart_done" in types
    assert "candidates" in types
    assert types[-2] == "stage_change"          # gate: selecting
    assert types[-1] == "completed"             # gate: 收口
    # gate 的 completed 携带恢复现场所需的全部产物
    completed = events[-1][1]
    assert completed["candidates"] and completed["lineart_url"]


async def test_lineart_failure_marks_failed(test_photo, lineart_workflow):
    events = []
    graph = build_stage1_graph()
    result = await graph.ainvoke(
        _make_state(test_photo, events),
        config=_make_config(FakeComfyRunner(success=False), FakeClipService(), lineart_workflow, None),
    )
    assert result["stage"] == "failed"
    assert result["errors"]
    assert any(t == "error" for t, _ in events)


async def test_retrieve_failure_nonfatal(test_photo, lineart_workflow):
    """检索失败不阻断主线：线稿产物照常，候选为空，仍可进入选图。"""
    events = []
    graph = build_stage1_graph()
    result = await graph.ainvoke(
        _make_state(test_photo, events),
        config=_make_config(
            FakeComfyRunner(), FakeClipService(fail=True), lineart_workflow, None
        ),
    )
    assert result["stage"] == "selecting"
    assert result["candidates"] == []
    assert result["lineart_url"]
    assert any(t == "error" and d.get("node") == "retrieve" for t, d in events)


async def test_missing_photo_fails_fast(lineart_workflow, tmp_path):
    events = []
    graph = build_stage1_graph()
    state = _make_state(tmp_path / "not_exists.png", events)
    result = await graph.ainvoke(
        state,
        config=_make_config(FakeComfyRunner(), FakeClipService(), lineart_workflow, None),
    )
    assert result["stage"] == "failed"
    assert "实拍图不存在" in result["errors"][0]
