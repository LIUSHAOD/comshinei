"""tests/test_stage2.py — 阶段 2 图与 confirm/cancel 端点单测（全 mock，无外部依赖）"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.api.v1.projects as projects_module
from app.main import app
from app.models.project import Project
from app.runtime import store
from app.runtime.graph import build_stage2_graph
from app.runtime.nodes import resolve_generate_inputs
from app.storage.paths import outputs_dir


class FakeComfyRunner:
    def __init__(self, success: bool = True):
        self.success = success

    async def run(self, workflow, output_dir, outputs_spec=None, on_progress=None):
        if not self.success:
            return SimpleNamespace(success=False, outputs={}, error="OOM", prompt_id=None)
        outputs = {}
        for spec in outputs_spec or []:
            p = Path(output_dir) / f"{spec['key']}_result.png"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\x89PNG\r\n\x1a\n")
            outputs[spec["key"]] = p
        return SimpleNamespace(success=True, outputs=outputs, error=None, prompt_id="fake")


class FakePromptService:
    def __init__(self, fail: bool = False):
        self.fail = fail

    async def generate_design_prompt(self, requirements, *, project_id):
        if self.fail:
            raise RuntimeError("LLM 超时")
        return {"prompt": "modern living room, warm light", "negative_prompt": "lowres"}


def _stage2_state(events, outputs_root):
    async def emit(t, d):
        events.append((t, d))

    # 阶段 1 产物：写假文件并生成 URL（generate_node 会还原成磁盘路径）
    refs = {}
    for name in ("lineart", "mlsd", "depth"):
        p = outputs_root / f"{name}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x89PNG\r\n\x1a\n")
        refs[name] = f"/api/images/outputs/test_stage2_unit/{p.name}"

    return {
        "project_id": "proj_s2",
        "photo_path": "unused.png",
        "requirements": "北欧风",
        "selected_ref": {"id": "style_a", "image_url": "/api/images/uploads/styles/a.png"},
        "lineart_url": refs["lineart"],
        "mlsd_url": refs["mlsd"],
        "depth_url": refs["depth"],
        "stage": "selecting",
        "emit": emit,
    }


def _stage2_config(prompt_svc, comfy, generate_workflow, outputs_root):
    return {
        "configurable": {
            "prompt_service": prompt_svc,
            "comfy_runner": comfy,
            "generate_workflow": generate_workflow,
            "output_dir": str(outputs_root),
        }
    }


class TestResolveGenerateInputs:
    def test_fixture_keys(self, generate_workflow):
        roles = resolve_generate_inputs(generate_workflow)
        assert roles == {"lineart": "13", "depth": "18", "mlsd": "25", "prompt": "11", "negative": "12"}

    def test_mlsd_alias(self):
        wf = {
            "1": {"class_type": "LoadImage", "inputs": {}, "_meta": {"title": "MLSD 图 (Input)"}},
            "2": {"class_type": "LoadImage", "inputs": {}, "_meta": {"title": "线稿 (Input)"}},
            "3": {"class_type": "LoadImage", "inputs": {}, "_meta": {"title": "深度图 (Input)"}},
            "4": {"class_type": "CLIPTextEncode", "inputs": {}, "_meta": {"title": "提示词 (Input)"}},
            "5": {"class_type": "CLIPTextEncode", "inputs": {}, "_meta": {"title": "负面词 (Input)"}},
        }
        assert resolve_generate_inputs(wf)["mlsd"] == "1"

    def test_missing_input_raises(self):
        with pytest.raises(ValueError, match="缺少输入"):
            resolve_generate_inputs({"1": {"class_type": "LoadImage", "inputs": {}, "_meta": {"title": "线稿 (Input)"}}})


class TestStage2Graph:
    async def test_happy_path(self, generate_workflow):
        events = []
        outputs_root = outputs_dir() / "test_stage2_unit"
        graph = build_stage2_graph()
        result = await graph.ainvoke(
            _stage2_state(events, outputs_root),
            config=_stage2_config(FakePromptService(), FakeComfyRunner(), generate_workflow, outputs_root),
        )
        assert result["stage"] == "done"
        assert result["prompt"] == "modern living room, warm light"
        assert result["result_url"].startswith("/api/images/outputs/")

        types = [t for t, _ in events]
        assert types[0] == "stage_change"  # prompting
        assert "image_done" in types
        assert types[-1] == "completed"
        assert dict(events).get("completed", events[-1][1])["result_url"] == result["result_url"]

    async def test_prompt_failure(self, generate_workflow):
        events = []
        outputs_root = outputs_dir() / "test_stage2_unit"
        graph = build_stage2_graph()
        result = await graph.ainvoke(
            _stage2_state(events, outputs_root),
            config=_stage2_config(FakePromptService(fail=True), FakeComfyRunner(), generate_workflow, outputs_root),
        )
        assert result["stage"] == "failed"
        assert any(t == "error" and d.get("node") == "prompt" for t, d in events)

    async def test_generate_failure(self, generate_workflow):
        events = []
        outputs_root = outputs_dir() / "test_stage2_unit"
        graph = build_stage2_graph()
        result = await graph.ainvoke(
            _stage2_state(events, outputs_root),
            config=_stage2_config(FakePromptService(), FakeComfyRunner(success=False), generate_workflow, outputs_root),
        )
        assert result["stage"] == "failed"
        assert any(t == "error" and d.get("node") == "generate" for t, d in events)


class FakeInterruptRunner:
    def __init__(self):
        self.interrupted = False

    async def interrupt(self):
        self.interrupted = True
        return True


class TestConfirmCancelEndpoints:
    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setattr(projects_module, "ComfyUIRunner", SimpleNamespace(from_settings=lambda: FakeInterruptRunner()))
        with TestClient(app) as c:
            yield c

    def _seed_project(self, db_session, stage="selecting"):
        row = db_session.merge(
            Project(
                requirements="北欧风",
                stage=stage,
                photo_path=str(outputs_dir() / "x.png"),
                lineart_path="outputs/x/lineart.png",
                mlsd_path="outputs/x/mlsd.png",
                depth_path="outputs/x/depth.png",
            )
        )
        db_session.commit()
        return row.id

    async def _seed_job(self, pid):
        await store.set_job_fields(
            pid,
            {
                "stage": "selecting",
                "candidates": [{"id": "style_a", "image_url": "/api/images/uploads/styles/a.png", "score": 0.9}],
                "lineart_url": "/api/images/outputs/x/lineart.png",
                "mlsd_url": "/api/images/outputs/x/mlsd.png",
                "depth_url": "/api/images/outputs/x/depth.png",
            },
        )

    def test_confirm_starts_stage2(self, client, db_session, fake_redis, monkeypatch, generate_workflow):
        pid = self._seed_project(db_session)
        import anyio

        anyio.run(self._seed_job, pid)

        # generate 模板入库（清掉测试库里的历史同 key 行，防唯一键冲突）
        import json as jsonlib

        from app.models.comfy_workflow import ComfyWorkflow

        db_session.query(ComfyWorkflow).filter(ComfyWorkflow.workflow_key == "generate").delete()
        db_session.merge(
            ComfyWorkflow(workflow_key="generate", name="g", kind="image", json=jsonlib.dumps(generate_workflow, ensure_ascii=False))
        )
        db_session.commit()

        started = {}

        class FakeTask:
            def done(self):
                return False

        class FakeRunner:
            async def start_stage2(self, **kwargs):
                started.update(kwargs)
                return FakeTask()

        monkeypatch.setattr(projects_module, "get_runner", lambda: FakeRunner())
        monkeypatch.setattr(projects_module, "get_running_task", lambda pid: None)

        resp = client.post(f"/api/projects/{pid}/confirm", json={"style_image_id": "style_a"})
        assert resp.status_code == 200, resp.json()
        assert started["selected_ref"]["id"] == "style_a"
        assert started["project_id"] == pid

    def test_confirm_wrong_stage_409(self, client, db_session, fake_redis):
        pid = self._seed_project(db_session, stage="lineart")
        resp = client.post(f"/api/projects/{pid}/confirm", json={"style_image_id": "style_a"})
        assert resp.status_code == 409

    def test_confirm_unknown_candidate_400(self, client, db_session, fake_redis):
        import anyio

        pid = self._seed_project(db_session)
        anyio.run(self._seed_job, pid)
        resp = client.post(f"/api/projects/{pid}/confirm", json={"style_image_id": "nobody"})
        assert resp.status_code == 400

    def test_confirm_retry_from_failed_with_artifacts(self, client, db_session, fake_redis, monkeypatch, generate_workflow):
        """阶段 2 失败后（stage=failed、产物在）允许重试 confirm（review #1）。"""
        import anyio
        import json as jsonlib

        from app.models.comfy_workflow import ComfyWorkflow

        pid = self._seed_project(db_session, stage="failed")
        anyio.run(self._seed_job, pid)
        db_session.query(ComfyWorkflow).filter(ComfyWorkflow.workflow_key == "generate").delete()
        db_session.merge(
            ComfyWorkflow(workflow_key="generate", name="g", kind="image", json=jsonlib.dumps(generate_workflow, ensure_ascii=False))
        )
        db_session.commit()

        class FakeTask:
            def done(self):
                return False

        class FakeRunner:
            async def start_stage2(self, **kwargs):
                return FakeTask()

        monkeypatch.setattr(projects_module, "get_runner", lambda: FakeRunner())
        monkeypatch.setattr(projects_module, "get_running_task", lambda pid: None)
        resp = client.post(f"/api/projects/{pid}/confirm", json={"style_image_id": "style_a"})
        assert resp.status_code == 200, resp.json()

    def test_confirm_retry_from_failed_without_artifacts_409(self, client, db_session, fake_redis, monkeypatch):
        """阶段 1 失败（stage=failed、无线稿产物）confirm 仍被产物检查挡住。"""
        import anyio

        row = db_session.merge(Project(requirements="x", stage="failed", photo_path="x.png"))
        db_session.commit()

        async def _seed():
            await store.set_job_fields(row.id, {"stage": "failed", "candidates": [{"id": "style_a", "image_url": "/api/images/uploads/styles/a.png"}]})

        anyio.run(_seed)
        monkeypatch.setattr(projects_module, "get_running_task", lambda pid: None)
        resp = client.post(f"/api/projects/{row.id}/confirm", json={"style_image_id": "style_a"})
        assert resp.status_code == 409
        assert "产物不完整" in resp.json()["message"]

    def test_delete_running_project_cancels_first(self, client, db_session, fake_redis, monkeypatch):
        """DELETE 进行中项目：先 task.cancel + interrupt 再删（review #2）。"""
        pid = self._seed_project(db_session, stage="generating")
        calls = {"cancel": 0, "interrupt": 0}

        class FakeTask:
            def done(self):
                return False

            def cancel(self):
                calls["cancel"] += 1

        class FakeInterrupt:
            async def interrupt(self):
                calls["interrupt"] += 1
                return True

        monkeypatch.setattr(projects_module, "get_running_task", lambda pid: FakeTask())
        monkeypatch.setattr(projects_module, "ComfyUIRunner", SimpleNamespace(from_settings=lambda: FakeInterrupt()))
        resp = client.delete(f"/api/projects/{pid}")
        assert resp.status_code == 200
        assert calls == {"cancel": 1, "interrupt": 1}
        assert db_session.get(Project, pid) is None

    def test_cancel_rolls_back_to_selecting(self, client, db_session, fake_redis, monkeypatch):
        pid = self._seed_project(db_session, stage="generating")

        class FakeTask:
            def __init__(self):
                self.cancelled = False

            def done(self):
                return False

            def cancel(self):
                self.cancelled = True

        task = FakeTask()
        monkeypatch.setattr(projects_module, "get_running_task", lambda pid: task)
        resp = client.post(f"/api/projects/{pid}/cancel")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["stage"] == "selecting" and task.cancelled
        db_session.expire_all()
        assert db_session.get(Project, pid).stage == "selecting"

    def test_cancel_reads_job_stage_over_stale_db(self, client, db_session, fake_redis, monkeypatch):
        """阶段 2 进行中 DB 行的 stage 滞后为 selecting——cancel 必须读 job stage，
        否则会把进行中的生图误判成阶段 1 而打死成 failed（review 实证）。"""
        import anyio

        pid = self._seed_project(db_session, stage="selecting")  # DB 滞后

        async def _seed():
            await store.set_job_fields(pid, {"stage": "generating"})  # job 实时

        anyio.run(_seed)

        class FakeTask:
            def done(self):
                return False

            def cancel(self):
                pass

        monkeypatch.setattr(projects_module, "get_running_task", lambda pid: FakeTask())
        resp = client.post(f"/api/projects/{pid}/cancel")
        assert resp.status_code == 200
        assert resp.json()["data"]["stage"] == "selecting"  # 回退选图而非 failed
        db_session.expire_all()
        assert db_session.get(Project, pid).stage == "selecting"

    def test_requery_blocked_during_stage2(self, client, db_session, fake_redis, monkeypatch):
        """阶段 2 进行中（job stage=generating，DB 滞后 selecting）禁止 requery——
        否则清空事件列表会污染进行中阶段的 SSE。"""
        import anyio

        import app.api.v1.search as search_module

        pid = self._seed_project(db_session, stage="selecting")

        async def _seed():
            await store.set_job_fields(pid, {"stage": "generating"})

        anyio.run(_seed)

        class FakeTask:
            def done(self):
                return False

        monkeypatch.setattr(search_module, "get_running_task", lambda pid: FakeTask())
        resp = client.post(f"/api/projects/{pid}/requery")
        assert resp.status_code == 409
        assert "进行中的任务" in resp.json()["message"]

    def test_cancel_no_task_409(self, client, db_session, fake_redis, monkeypatch):
        pid = self._seed_project(db_session, stage="selecting")
        monkeypatch.setattr(projects_module, "get_running_task", lambda pid: None)
        assert client.post(f"/api/projects/{pid}/cancel").status_code == 409
