"""tests/test_cleanup.py — 清理服务单测（fakeredis + sqlite + 假 Qdrant，无外部依赖）"""

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.services.cleanup_service as cleanup_module
from app.main import app
from app.models.project import Project
from app.models.style_image import StyleImage
from app.runtime import store
from app.services.cleanup_service import CleanupService
from app.storage.paths import outputs_dir, project_upload_dir


def _seed_project(db_session, pid: str, days_old: int):
    row = db_session.merge(
        Project(
            id=pid,
            requirements="x",
            stage="done",
            created_at=datetime.now() - timedelta(days=days_old),
        )
    )
    db_session.commit()
    up = project_upload_dir(pid)
    up.mkdir(parents=True, exist_ok=True)
    (up / "photo.png").write_bytes(b"png")
    out = outputs_dir() / pid
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.png").write_bytes(b"png")
    return pid


class TestDeleteProject:
    async def test_full_teardown(self, db_session, fake_redis):
        pid = _seed_project(db_session, "clean_a", days_old=1)
        await store.push_event(pid, "stage_change", {})
        await store.set_job_fields(pid, {"stage": "done"})
        await store.add_excludes(pid, ["s1"])

        svc = CleanupService()
        assert await svc.delete_project(pid) is True

        assert db_session.get(Project, pid) is None
        # 不用 project_upload_dir()/outputs_dir() 断言（助手会顺手建目录），直接拼路径
        from app.storage.paths import uploads_dir

        assert not (uploads_dir() / "projects" / pid).exists()
        assert not (outputs_dir() / pid).exists()  # outputs_dir() 只建根，不建 /pid
        assert await store.get_events(pid) == []
        assert await store.get_job(pid) == {}
        assert await store.get_excludes(pid) == []

    async def test_not_found(self, fake_redis):
        assert await CleanupService().delete_project("nobody") is False


class TestTtl:
    async def test_only_expired_deleted(self, db_session, fake_redis):
        old = _seed_project(db_session, "clean_old", days_old=40)
        fresh = _seed_project(db_session, "clean_fresh", days_old=1)

        report = await CleanupService().run_ttl(ttl_days=30)
        assert report["projects_deleted"] == 1
        assert db_session.get(Project, old) is None
        assert db_session.get(Project, fresh) is not None
        assert (outputs_dir() / fresh).exists()


class TestQdrantReconcile:
    async def test_orphan_points_deleted(self, db_session, fake_redis, monkeypatch):
        # 行：style_keep；点：keep + orphan
        db_session.merge(StyleImage(id="style_keep", file_path="uploads/styles/x.png", source="upload"))
        db_session.commit()

        fake_qdrant = SimpleNamespace(
            collection_exists=lambda name: True,
            scroll=lambda **kw: (
                [
                    SimpleNamespace(id="p1", payload={"style_image_id": "style_keep"}),
                    SimpleNamespace(id="p2", payload={"style_image_id": "style_orphan"}),
                    SimpleNamespace(id="p3", payload={"style_image_id": "style_orphan"}),
                ],
                None,
            ),
            deleted=[],
            delete=None,
        )
        deleted_calls = []

        def _delete(collection_name, wait, points_selector):
            deleted_calls.append(sorted(points_selector.points))

        fake_qdrant.delete = _delete
        fake_clip = SimpleNamespace(_get_qdrant=lambda: fake_qdrant)

        async def _fake_get_clip():
            return fake_clip

        monkeypatch.setattr(cleanup_module, "get_clip_service", _fake_get_clip)

        report = await CleanupService().run_qdrant_reconcile()
        assert report["orphan_points_deleted"] == 2
        assert deleted_calls == [["p2", "p3"]]


class TestRunAllEndpoint:
    def test_manual_run(self, monkeypatch):
        class FakeSvc:
            async def run_all(self):
                return {"ttl_days": 30, "projects_deleted": 1, "orphan_points_deleted": 2, "last_run_at": "t"}

        import app.api.v1.cleanup as cleanup_api

        monkeypatch.setattr(cleanup_api, "get_cleanup_service", lambda: FakeSvc())
        with TestClient(app) as c:
            resp = c.post("/api/cleanup/run")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["projects_deleted"] == 1 and data["orphan_points_deleted"] == 2


class TestReconcileGraceWindow:
    async def test_young_points_skipped(self, db_session, fake_redis, monkeypatch):
        """对账豁免 10 分钟内写入的点（防与在途上传竞态误删，review #5）。"""
        import time

        young_ts = int(time.time()) - 60
        old_ts = int(time.time()) - 3600
        fake_qdrant = SimpleNamespace(
            collection_exists=lambda name: True,
            scroll=lambda **kw: (
                [
                    SimpleNamespace(id="p_young", payload={"style_image_id": "style_orphan_new", "created_ts": young_ts}),
                    SimpleNamespace(id="p_old", payload={"style_image_id": "style_orphan_old", "created_ts": old_ts}),
                    SimpleNamespace(id="p_nots", payload={"style_image_id": "style_orphan_legacy"}),
                ],
                None,
            ),
        )
        deleted_calls = []

        def _delete(collection_name, wait, points_selector):
            deleted_calls.append(sorted(points_selector.points))

        fake_qdrant.delete = _delete
        fake_clip = SimpleNamespace(_get_qdrant=lambda: fake_qdrant)

        async def _fake_get_clip():
            return fake_clip

        monkeypatch.setattr(cleanup_module, "get_clip_service", _fake_get_clip)
        report = await CleanupService().run_qdrant_reconcile()
        # 年轻点豁免；老点与无时间戳的遗留点照删
        assert report["orphan_points_deleted"] == 2
        assert deleted_calls == [["p_nots", "p_old"]]
