"""tests/test_prompt_service.py — LLM 提示词服务单测（mock HTTP 层，无外部依赖）"""

import pytest

from app.services.prompt_service import PromptService, _extract_json


class TestExtractJson:
    def test_clean_json(self):
        out = _extract_json('{"prompt": "modern living room", "negative_prompt": "lowres"}')
        assert out["prompt"] == "modern living room"
        assert out["negative_prompt"] == "lowres"

    def test_fenced_json(self):
        out = _extract_json('```json\n{"prompt": "a"}\n```')
        assert out["prompt"] == "a"
        assert out["negative_prompt"]  # 缺省补默认负面词

    def test_json_embedded_in_text(self):
        out = _extract_json('好的，这是提示词：\n{"prompt": "b", "negative_prompt": "n"}\n希望满意')
        assert out["prompt"] == "b"
        assert out["negative_prompt"] == "n"

    def test_garbage_falls_back_to_raw_text(self):
        out = _extract_json("warm scandinavian living room, oak wood")
        assert out["prompt"] == "warm scandinavian living room, oak wood"
        assert "lowres" in out["negative_prompt"]


class FakePromptService(PromptService):
    def __init__(self, reply: str):
        self._reply = reply
        self.calls: list[list[dict]] = []

    async def _raw_chat(self, messages):
        self.calls.append(messages)
        return self._reply


class TestGenerateDesignPrompt:
    @pytest.fixture(autouse=True)
    def _no_langfuse(self, monkeypatch):
        """单测不走 Langfuse 观测路径（.env 里可能配了密钥）。"""
        from app.config import settings

        monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "")
        monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "")

    async def test_full_flow(self):
        svc = FakePromptService('{"prompt": "japandi living room, warm light", "negative_prompt": "blur"}')
        # 无 LANGFUSE 配置时直连 _raw_chat
        out = await svc.generate_design_prompt("日式原木风客厅，暖光", project_id="proj_x")
        assert out["prompt"] == "japandi living room, warm light"
        assert out["negative_prompt"] == "blur"
        # system prompt 传达了 SD 提示词工程要求
        assert "提示词" in svc.calls[0][0]["content"]
        assert "日式原木风" in svc.calls[0][1]["content"]

    async def test_no_api_key_raises(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "LLM_API_KEY", "")
        svc = PromptService()
        with pytest.raises(RuntimeError, match="LLM_API_KEY"):
            await svc.generate_design_prompt("x", project_id="p")
