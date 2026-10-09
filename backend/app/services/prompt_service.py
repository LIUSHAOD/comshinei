"""app/services/prompt_service.py — LLM 生图提示词生成（OpenAI 兼容 + 可选 Langfuse）

计划 §5.3：每个 project 一个 trace（session_id=project_id）。实现说明：计划写的是
LangChain CallbackHandler 路线；本项目 LLM 调用点只有这一处，直接用 openai SDK
更轻，Langfuse 以 observe 装饰器接入（配置了密钥才启用，可观测性永远不阻断生图）。

v1 说明：deepseek-chat 等文本模型看不到选中图本身，提示词由用户需求驱动
（LLM 把中文需求扩写/翻译为 SD1.5 英文提示词）；选中图的风格注入留给 IPAdapter（预留）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.config import settings
from app.utils.logger import logger

_SYSTEM_PROMPT = """你是资深室内设计师，同时精通 Stable Diffusion 提示词工程。
根据用户的中文装修需求，为「实拍房照片 + 线稿 BrushNet 上色」流程生成英文生图提示词。
硬性要求：
- 只输出 JSON，格式 {"prompt": "...", "negative_prompt": "..."}，不要输出任何其他内容；
- prompt 用英文，覆盖：风格、空间、主要家具与材质、配色、光照、镜头视角、画质词
  （如 photorealistic, best quality, 8k）；长度 40-80 词；
- 不要改变房间结构（生图由线稿与深度图约束结构），提示词只描述风格与软装；
- negative_prompt 给通用质量负面词即可。"""

_DEFAULT_NEGATIVE = (
    "lowres, bad anatomy, watermark, text, signature, blurry, low quality, distorted, ugly"
)


def _extract_json(text: str) -> dict[str, str]:
    """从 LLM 输出提取 {"prompt", "negative_prompt"}；容忍代码围栏与夹带文字。"""
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict) and data.get("prompt"):
                return {
                    "prompt": str(data["prompt"]).strip(),
                    "negative_prompt": str(data.get("negative_prompt") or _DEFAULT_NEGATIVE).strip(),
                }
        except json.JSONDecodeError:
            pass
    # 兜底：全文当 prompt
    logger.warning("LLM 输出未含合法 JSON，全文作为 prompt 使用")
    return {"prompt": cleaned, "negative_prompt": _DEFAULT_NEGATIVE}


class PromptService:
    def __init__(self) -> None:
        self._client: Any = None

    def _get_client(self):
        if self._client is None:
            if not settings.LLM_API_KEY:
                raise RuntimeError("未配置 LLM_API_KEY（.env 中设置 OpenAI 兼容服务的密钥）")
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(base_url=settings.LLM_BASE_URL, api_key=settings.LLM_API_KEY)
        return self._client

    async def _chat(self, messages: list[dict[str, str]], *, project_id: str) -> str:
        """单次 LLM 调用（测试 mock 点；Langfuse 可选观测）。

        try 只包 Langfuse 的接入与装饰；LLM 调用本身的异常原样上抛——
        否则一次 LLM 故障会被兜底逻辑误当成 Langfuse 故障，静默重试一次（双倍成本）。
        """
        traced = None
        if settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY:
            try:
                from langfuse import get_client, observe

                @observe(name="generate_design_prompt")
                async def _traced() -> str:
                    try:
                        client = get_client()
                        # langfuse v3 各版本 API 不一：update_current_trace 或退化为 span metadata
                        if hasattr(client, "update_current_trace"):
                            client.update_current_trace(session_id=project_id)
                        else:
                            client.update_current_span(metadata={"session_id": project_id})
                    except Exception:  # noqa: BLE001
                        # 观测元数据失败不影响 LLM 调用（span 数据本地丢弃即可）
                        logger.warning("Langfuse trace 更新失败", exc_info=True)
                    return await self._raw_chat(messages)

                traced = _traced
            except Exception:  # noqa: BLE001
                logger.warning("Langfuse 接入失败，降级为无观测调用", exc_info=True)
                traced = None
        if traced is not None:
            return await traced()
        return await self._raw_chat(messages)

    async def _raw_chat(self, messages: list[dict[str, str]]) -> str:
        client = self._get_client()
        resp = await client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=400,
        )
        return resp.choices[0].message.content or ""

    async def generate_design_prompt(self, requirements: str, *, project_id: str) -> dict[str, str]:
        """需求 → {"prompt", "negative_prompt"}（英文 SD 提示词）。"""
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"用户的装修需求：{requirements}"},
        ]
        raw = await self._chat(messages, project_id=project_id)
        result = _extract_json(raw)
        logger.info("LLM 提示词生成: %s -> %s", project_id, result["prompt"][:80])
        return result


_prompt_service: PromptService | None = None


def get_prompt_service() -> PromptService:
    global _prompt_service
    if _prompt_service is None:
        _prompt_service = PromptService()
    return _prompt_service
