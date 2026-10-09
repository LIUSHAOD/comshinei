"""tests/test_runner.py — GraphRunner 全链路单测（fakeredis + sqlite + mock 服务）"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.project import Project
from app.runtime import store
from app.runtime.runner import GraphRunner
from app.runtime.graph import build_stage1_graph
from app.storage.paths import outputs_dir


class FakeComfyRunner:
    async def run(self, workflow, output_dir, outputs_spec=None, on_progress=None):
        outputs = {}
        for spec in outputs_spec or []:
            p = Path(output_dir) / f"{spec['key']}_fake.png"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\x89PNG\r\n\x1a\n")
            outputs[spec["key"]] = p
        return SimpleNamespace(success=True, outputs=outputs, error=None, prompt_id="fake")


class FakeClipService:
    async def search_styles(self, text, exclude_ids=None, limit=None):
        # 第二次调用（requery）时 exclude 已生效：换一批候选
        if exclude_ids:
            return [{"id": "style_new", "image_url": "/api/images/uploads/styles/new.png", "score": 0.8}]
        return [{"id": "style_a", "image_url": "/api/images/uploads/styles/a.png", "score": 0.9}]


@pytest.fixture
def runner() -> GraphRunner:
    return GraphRunner(build_stage1_graph())


async def test_stage1_events_and_persistence(
    runner, fake_redis, db_session, test_photo, lineart_workflow
):
    # 建项目行
    row = db_session.merge(Project(requirements="北欧风", stage="created"))
    db_session.commit()
    pid = row.id

    task = await runner.start_stage1(
        project_id=pid,
        photo_path=str(test_photo),
        requirements="北欧风客厅",
        lineart_workflow=lineart_workflow,
        output_dir=outputs_dir() / "test_runner" / pid,
        comfy_runner=FakeComfyRunner(),
        clip_service=FakeClipService(),
    )
    await task

    # Redis 事件流：回放完整，终止事件收尾
    events = await store.get_events(pid)
    types = [e["type"] for e in events]
    assert types[0] == "stage_change"
    assert "lineart_done" in types and "candidates" in types
    assert types[-1] == "completed"
    assert all(e["seq"] == i + 1 for i, e in enumerate(events))  # seq 连续

    # job hash：stage + 产物 + 候选
    job = await store.get_job(pid)
    assert job["stage"] == "selecting"
    assert job["lineart_url"].startswith("/api/images/outputs/")
    assert job["candidates"][0]["id"] == "style_a"

    # projects 行：stage + 产物引用（去掉 /api/images/ 前缀的相对串）
    db_session.expire_all()
    row = db_session.get(Project, pid)
    assert row.stage == "selecting"
    assert row.lineart_path.startswith("outputs/")
    assert row.error is None


async def test_requery_pushes_new_candidates(runner, fake_redis, db_session, test_photo, lineart_workflow):
    row = db_session.merge(Project(requirements="北欧风", stage="created"))
    db_session.commit()
    pid = row.id

    task = await runner.start_stage1(
        project_id=pid,
        photo_path=str(test_photo),
        requirements="北欧风客厅",
        lineart_workflow=lineart_workflow,
        output_dir=outputs_dir() / "test_runner" / pid,
        comfy_runner=FakeComfyRunner(),
        clip_service=FakeClipService(),
    )
    await task

    # 模拟 requery 接口语义：当前候选入 exclude → 重跑检索
    job = await store.get_job(pid)
    await store.add_excludes(pid, [c["id"] for c in job["candidates"]])
    task = await runner.requery(project_id=pid, requirements="北欧风客厅", clip_service=FakeClipService())
    await task

    events = await store.get_events(pid)
    candidate_events = [e for e in events if e["type"] == "candidates"]
    assert len(candidate_events) == 2
    # 第二轮候选是新一批，且事件里带 exclude 名单
    assert candidate_events[-1]["data"]["candidates"][0]["id"] == "style_new"
    assert candidate_events[-1]["data"]["excluded"] == ["style_a"]


async def test_stage2_failure_persists_failed_row(db_session, fake_redis, generate_workflow):
    """阶段 2 失败也必须回写 DB（曾因缩进错误只在 result_url 存在时回写，review 严重项）。"""
    from app.runtime.graph import build_stage2_graph
    from app.storage.paths import outputs_dir

    class FakeComfy:
        async def run(self, *a, **k):
            return SimpleNamespace(success=False, outputs={}, error="OOM", prompt_id=None)

    class FakePrompt:
        async def generate_design_prompt(self, requirements, *, project_id):
            return {"prompt": "p", "negative_prompt": "n"}

    row = db_session.merge(Project(requirements="x", stage="selecting", photo_path="x.png"))
    db_session.commit()
    pid = row.id

    root = outputs_dir() / "test_runner_s2"
    for name in ("lineart", "mlsd", "depth"):
        p = root / f"{name}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x89PNG\r\n\x1a\n")

    runner = GraphRunner(build_stage1_graph(), build_stage2_graph())
    task = await runner.start_stage2(
        project_id=pid,
        photo_path="x.png",
        requirements="x",
        selected_ref={"id": "style_a"},
        lineart_url="/api/images/outputs/test_runner_s2/lineart.png",
        mlsd_url="/api/images/outputs/test_runner_s2/mlsd.png",
        depth_url="/api/images/outputs/test_runner_s2/depth.png",
        generate_workflow=generate_workflow,
        output_dir=root,
        comfy_runner=FakeComfy(),
        prompt_service=FakePrompt(),
    )
    await task

    db_session.expire_all()
    row = db_session.get(Project, pid)
    assert row.stage == "failed"
    assert "OOM" in (row.error or "")
    assert row.prompt == "p"  # 已生成的提示词也应回写

    events = await store.get_events(pid)
    assert any(e["type"] == "error" and e["data"].get("fatal") for e in events)


async def test_double_start_rejected(db_session, fake_redis, test_photo, lineart_workflow):
    """TOCTOU 防重：同项目第二个 start 在锁内被 TaskAlreadyRunningError 拒绝（review #3）。"""
    import asyncio as _asyncio

    from app.runtime.runner import TaskAlreadyRunningError

    class SlowComfy:
        async def run(self, *a, **k):
            await _asyncio.sleep(30)
            return SimpleNamespace(success=True, outputs={}, error=None, prompt_id="x")

    class FakeClip:
        async def search_styles(self, *a, **k):
            return []

    row = db_session.merge(Project(requirements="x", stage="created"))
    db_session.commit()
    pid = row.id

    runner = GraphRunner(build_stage1_graph())
    kwargs = dict(
        project_id=pid,
        photo_path=str(test_photo),
        requirements="x",
        lineart_workflow=lineart_workflow,
        output_dir=outputs_dir() / "test_double" / pid,
        comfy_runner=SlowComfy(),
        clip_service=FakeClip(),
    )
    task1 = await runner.start_stage1(**kwargs)
    try:
        with pytest.raises(TaskAlreadyRunningError):
            await runner.start_stage1(**kwargs)
    finally:
        task1.cancel()
        with pytest.raises(_asyncio.CancelledError):
            await task1
