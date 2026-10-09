"""tests/test_api_guards.py — API 层防护与统一响应信封（无需外部服务）"""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.v1.comfy import _resolve_output_dir
from app.main import app


class TestResolveOutputDir:
    def test_reject_parent_traversal(self):
        with pytest.raises(HTTPException) as exc:
            _resolve_output_dir("../../etc")
        assert exc.value.status_code == 400

    def test_reject_absolute_path(self):
        with pytest.raises(HTTPException) as exc:
            _resolve_output_dir("D:/evil")
        assert exc.value.status_code == 400

    def test_reject_drive_relative(self):
        with pytest.raises(HTTPException) as exc:
            _resolve_output_dir("C:evil")
        assert exc.value.status_code == 400

    def test_normal_subdir_ok(self):
        d = _resolve_output_dir("debug/test")
        assert d.name == "test"
        assert d.parent.name == "debug"

    def test_none_gives_default_debug_dir(self):
        d = _resolve_output_dir(None)
        assert d.parent.name == "debug"


class TestResponseEnvelope:
    def test_422_uses_unified_envelope(self):
        # input_values 传了错误类型 → RequestValidationError → 统一 {code, message, data}
        with TestClient(app) as client:
            resp = client.post("/api/comfy/run", json={"input_values": "not-a-dict"})
        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == 422
        assert "message" in body and "data" in body
