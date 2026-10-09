"""app/runtime/nodes.py — 图节点：阶段 1 parse/lineart/retrieve/gate，阶段 2 prompt/generate/assemble

依赖注入约定：外部资源（ComfyUIRunner、ClipService、PromptService、工作流模板、产物目录）
经 LangGraph config["configurable"] 传入，节点本身无模块级依赖，便于测试 mock。
"""

import asyncio
import time
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.runtime import store
from app.runtime.state import (
    EVENT_CANDIDATES,
    EVENT_COMPLETED,
    EVENT_ERROR,
    EVENT_IMAGE_DONE,
    EVENT_LINEART_DONE,
    EVENT_PROGRESS,
    EVENT_STAGE_CHANGE,
    STAGE_DONE,
    STAGE_FAILED,
    STAGE_GENERATING,
    STAGE_LINEART,
    STAGE_PROMPTING,
    STAGE_SELECTING,
    DesignState,
)
from app.services.workflow_parse import input_node_ids, parse_dynamic_outputs, patch_workflow
from app.storage.paths import resolve_image_ref, to_image_ref
from app.utils.logger import logger


def _cfg(config: RunnableConfig, key: str, default: Any = None) -> Any:
    return (config.get("configurable") or {}).get(key, default)


async def parse_node(state: DesignState, config: RunnableConfig) -> dict[str, Any]:
    """校验输入，广播阶段 1 开始。"""
    photo = Path(state["photo_path"])
    if not photo.exists():
        await state["emit"](EVENT_ERROR, {"node": "parse", "message": f"实拍图不存在: {photo}"})
        return {"stage": STAGE_FAILED, "errors": [f"实拍图不存在: {photo}"]}

    await state["emit"](EVENT_STAGE_CHANGE, {"stage": STAGE_LINEART, "message": "线稿提取与风格检索并行开始"})
    return {"stage": STAGE_LINEART}


async def lineart_node(state: DesignState, config: RunnableConfig) -> dict[str, Any]:
    """调 ComfyUI lineart 工作流：实拍图 → 线稿/结构线/深度图/分割图。"""
    emit = state["emit"]
    runner = _cfg(config, "comfy_runner")
    workflow = _cfg(config, "lineart_workflow")
    output_dir = Path(_cfg(config, "output_dir"))

    await emit(EVENT_PROGRESS, {"node": "lineart", "percent": 0, "message": "线稿提取开始"})

    ids = input_node_ids(workflow)
    if "实拍图" not in ids:
        message = f"lineart 工作流缺少『实拍图 (Input)』节点，当前输入: {sorted(ids)}"
        await emit(EVENT_ERROR, {"node": "lineart", "message": message})
        return {"errors": [f"lineart: {message}"]}
    patched = patch_workflow(workflow, {ids["实拍图"]: state["photo_path"]})

    async def on_progress(percent: int, message: str) -> None:
        await emit(EVENT_PROGRESS, {"node": "lineart", "percent": percent, "message": message})

    result = await runner.run(
        patched,
        output_dir=output_dir,
        outputs_spec=parse_dynamic_outputs(workflow),
        on_progress=on_progress,
    )
    if not result.success:
        await emit(EVENT_ERROR, {"node": "lineart", "message": result.error})
        return {"errors": [f"lineart: {result.error}"]}

    urls = {key: f"/api/images/{to_image_ref(path)}" for key, path in result.outputs.items()}
    update = {
        "lineart_url": urls.get("线稿"),
        "mlsd_url": urls.get("结构线"),
        "depth_url": urls.get("深度图"),
        "seg_url": urls.get("分割图"),
    }
    await emit(EVENT_LINEART_DONE, update)
    return update


async def retrieve_node(state: DesignState, config: RunnableConfig) -> dict[str, Any]:
    """CLIP 文本编码需求 → Qdrant 检索相近风格图（must_not 排除已看）。"""
    emit = state["emit"]
    clip = _cfg(config, "clip_service")
    limit = _cfg(config, "candidate_limit")

    await emit(EVENT_PROGRESS, {"node": "retrieve", "percent": 0, "message": "风格检索开始"})

    exclude_ids = list(state.get("exclude_ids") or [])
    try:
        candidates = await clip.search_styles(
            state["requirements"], exclude_ids=exclude_ids, limit=limit
        )
    except Exception as exc:  # noqa: BLE001
        # 检索失败不阻断主线：线稿仍可出图，用户可稍后重查
        logger.exception("retrieve_node failed")
        await emit(EVENT_ERROR, {"node": "retrieve", "message": f"风格检索失败: {exc}"})
        return {"errors": [f"retrieve: {exc}"], "candidates": []}

    await emit(EVENT_CANDIDATES, {"candidates": candidates, "excluded": exclude_ids})
    return {"candidates": candidates}


async def gate_node(state: DesignState, config: RunnableConfig) -> dict[str, Any]:
    """阶段 1 收口：线稿必须成功；检索失败允许带 0 候选进入选图。"""
    emit = state["emit"]
    if not state.get("lineart_url"):
        errors = state.get("errors") or []
        await emit(EVENT_ERROR, {"node": "gate", "message": "线稿提取失败，无法进入选图", "fatal": True, "errors": errors})
        return {"stage": STAGE_FAILED}

    await emit(
        EVENT_STAGE_CHANGE,
        {"stage": STAGE_SELECTING, "message": "阶段 1 完成，等待用户选图"},
    )
    await emit(
        EVENT_COMPLETED,
        {
            "stage": STAGE_SELECTING,
            "lineart_url": state.get("lineart_url"),
            "mlsd_url": state.get("mlsd_url"),
            "depth_url": state.get("depth_url"),
            "seg_url": state.get("seg_url"),
            "candidates": state.get("candidates") or [],
            "errors": state.get("errors") or [],
        },
    )
    return {"stage": STAGE_SELECTING}


# ==================== 阶段 2：prompt → generate → assemble ====================

# generate 节点全局锁：8G 显存并发不足，ComfyUI 串行队列 + 后端锁双保险（计划 §11）
_generate_lock = asyncio.Lock()


def resolve_generate_inputs(workflow: dict[str, Any]) -> dict[str, str]:
    """把业务角色映射到模板 (Input) 节点 id（按输入名模糊匹配，兼容不同模板命名）。"""
    ids = input_node_ids(workflow)

    def find(*candidates: str) -> str | None:
        for key in ids:
            if any(c in key for c in candidates):
                return key
        return None

    mapping = {
        "lineart": find("线稿"),
        "depth": find("深度"),
        "mlsd": find("MLSD", "SD15", "结构线"),
        "prompt": find("提示词"),
        "negative": find("负面"),
    }
    missing = [role for role, key in mapping.items() if not key]
    if missing:
        raise ValueError(f"generate 工作流缺少输入 {missing}，现有输入: {sorted(ids)}")
    return {role: ids[key] for role, key in mapping.items()}  # type: ignore[misc]


def _image_url_to_path(url: str) -> Path:
    """state 里的 /api/images/{ref} URL → 磁盘绝对路径（ComfyUI 预上传用）。"""
    ref = url.removeprefix("/api/images/")
    path = resolve_image_ref(ref)
    if path is None:
        raise RuntimeError(f"图片不存在: {url}")
    return path


async def prompt_node(state: DesignState, config: RunnableConfig) -> dict[str, Any]:
    """LLM 根据需求生成英文生图提示词（trace: session_id=project_id）。"""
    emit = state["emit"]
    prompt_service = _cfg(config, "prompt_service")

    await emit(EVENT_STAGE_CHANGE, {"stage": STAGE_PROMPTING, "message": "LLM 生成生图提示词"})
    started = time.time()
    try:
        result = await prompt_service.generate_design_prompt(
            state["requirements"], project_id=state["project_id"]
        )
    except Exception as exc:  # noqa: BLE001
        await emit(EVENT_ERROR, {"node": "prompt", "message": f"提示词生成失败: {exc}"})
        return {"errors": [f"prompt: {exc}"], "stage": STAGE_FAILED}

    elapsed = round(time.time() - started, 2)
    await emit(
        EVENT_PROGRESS,
        {"node": "prompt", "percent": 100, "message": f"提示词完成（{elapsed}s）", "prompt": result["prompt"]},
    )
    return {
        "stage": STAGE_PROMPTING,
        "prompt": result["prompt"],
        "negative_prompt": result["negative_prompt"],
    }


async def generate_node(state: DesignState, config: RunnableConfig) -> dict[str, Any]:
    """ComfyUI 条件生图：线稿 BrushNet + 深度/MLSD 双 ControlNet + LLM 提示词 → 效果图。"""
    emit = state["emit"]
    runner = _cfg(config, "comfy_runner")
    workflow = _cfg(config, "generate_workflow")
    output_dir = Path(_cfg(config, "output_dir"))

    await emit(EVENT_STAGE_CHANGE, {"stage": STAGE_GENERATING, "message": "条件生图开始"})

    try:
        roles = resolve_generate_inputs(workflow)
        patched = patch_workflow(
            workflow,
            {
                roles["lineart"]: str(_image_url_to_path(state["lineart_url"])),
                roles["depth"]: str(_image_url_to_path(state["depth_url"])),
                roles["mlsd"]: str(_image_url_to_path(state["mlsd_url"])),
                roles["prompt"]: state["prompt"],
                roles["negative"]: state["negative_prompt"],
            },
        )
    except (ValueError, RuntimeError) as exc:
        await emit(EVENT_ERROR, {"node": "generate", "message": str(exc)})
        return {"errors": [f"generate: {exc}"], "stage": STAGE_FAILED}

    async def on_progress(percent: int, message: str) -> None:
        await emit(EVENT_PROGRESS, {"node": "generate", "percent": percent, "message": message})

    started = time.time()
    async with _generate_lock:
        result = await runner.run(
            patched,
            output_dir=output_dir,
            outputs_spec=parse_dynamic_outputs(workflow),
            on_progress=on_progress,
        )
    if not result.success:
        await emit(EVENT_ERROR, {"node": "generate", "message": result.error})
        return {"errors": [f"generate: {result.error}"], "stage": STAGE_FAILED}

    elapsed = round(time.time() - started, 2)
    result_path = next(iter(result.outputs.values()))
    result_url = f"/api/images/{to_image_ref(result_path)}"
    await emit(EVENT_IMAGE_DONE, {"result_url": result_url, "elapsed_sec": elapsed})
    return {"result_url": result_url, "stage": STAGE_GENERATING}


async def assemble_node(state: DesignState, config: RunnableConfig) -> dict[str, Any]:
    """阶段 2 收口：stage=done，推送最终结果。"""
    emit = state["emit"]
    if not state.get("result_url"):
        errors = state.get("errors") or []
        await emit(
            EVENT_ERROR,
            {"node": "assemble", "message": "生图失败", "fatal": True, "errors": errors},
        )
        return {"stage": STAGE_FAILED}

    await emit(EVENT_STAGE_CHANGE, {"stage": STAGE_DONE, "message": "全部完成"})
    await emit(
        EVENT_COMPLETED,
        {
            "stage": STAGE_DONE,
            "result_url": state["result_url"],
            "prompt": state.get("prompt"),
            "negative_prompt": state.get("negative_prompt"),
        },
    )
    return {"stage": STAGE_DONE}
