"""app/services/workflow_parse.py — ComfyUI 工作流 (Input)/(Output) 后缀解析 + 参数注入

移植自 Muse-Studio（muse_backend/app/comfyui_workflow.py 的 parse_dynamic_inputs /
parse_dynamic_outputs，与 muse-studio/app/api/generate/comfyui/route.ts 的 patchWorkflow），
并按本项目约定补充 CLIPTextEncode → text 的输入映射（开发计划 §3.3）。

约定：在 ComfyUI UI 里给需要外部注入的节点标题加后缀 " (Input)"，产出节点加
" (Output)"。解析与注入均基于 _meta.title / node_id，重新导出模板、增删节点都不用改代码。
"""

from __future__ import annotations

import copy
from typing import Any, Literal, TypedDict

from app.utils.logger import logger


ComfyInputKind = Literal["number", "text", "textarea", "image"]
ComfyOutputKind = Literal["image", "other"]


class ComfyDynamicInput(TypedDict):
    """工作流动态输入节点的归一化描述。"""

    node_id: str       # ComfyUI 图中的原始节点 id（如 "102"）
    key: str           # 标题去掉后缀后的基础名，如 "提示词"
    title: str         # 展示用标签
    kind: ComfyInputKind
    default_value: Any


class ComfyDynamicOutput(TypedDict):
    """工作流命名输出节点的归一化描述。"""

    node_id: str
    key: str
    title: str
    kind: ComfyOutputKind


INPUT_SUFFIX = " (Input)"
OUTPUT_SUFFIX = " (Output)"

# patch 时的字段路由（与 Muse patchWorkflow 一致）
_IMAGE_CLASSES = ("LoadImage", "LoadImageBase64", "ImageLoader", "ETN_LoadImageBase64")
_TEXT_CLASSES = ("CLIPTextEncode", "Note", "ShowText")


def _strip_suffix(title: str, suffix: str) -> str:
    if title.endswith(suffix):
        return title[: -len(suffix)].strip()
    return title


def parse_dynamic_inputs(workflow: dict[str, Any]) -> list[ComfyDynamicInput]:
    """
    提取全部动态输入：节点带 _meta.title 且以 ' (Input)' 结尾。

    class_type → kind：
      - PrimitiveInt / PrimitiveFloat  → 'number'
      - PrimitiveString                → 'text'
      - PrimitiveStringMultiline       → 'textarea'
      - CLIPTextEncode                 → 'text'（提示词直接写 inputs.text）
      - LoadImage / LoadImageBase64    → 'image'
    其余类型忽略。
    """
    inputs: list[ComfyDynamicInput] = []

    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        meta = node.get("_meta") or {}
        title = meta.get("title")
        if not isinstance(title, str) or not title.endswith(INPUT_SUFFIX):
            continue

        base = _strip_suffix(title, INPUT_SUFFIX)
        class_type = node.get("class_type")
        raw_inputs: dict[str, Any] = node.get("inputs") or {}

        kind: ComfyInputKind | None = None
        default: Any = None

        if class_type in ("PrimitiveInt", "PrimitiveFloat"):
            kind = "number"
            default = raw_inputs.get("value")
        elif class_type == "PrimitiveString":
            kind = "text"
            default = raw_inputs.get("value", "")
        elif class_type == "PrimitiveStringMultiline":
            kind = "textarea"
            default = raw_inputs.get("value", "")
        elif class_type == "CLIPTextEncode":
            kind = "text"
            default = raw_inputs.get("text", "")
        elif class_type in ("LoadImage", "LoadImageBase64"):
            kind = "image"
            default = None

        if kind is None:
            continue

        inputs.append(
            {
                "node_id": str(node_id),
                "key": base,
                "title": base,
                "kind": kind,
                "default_value": default,
            }
        )

    return inputs


def parse_dynamic_outputs(workflow: dict[str, Any]) -> list[ComfyDynamicOutput]:
    """
    提取全部命名输出：节点带 _meta.title 且以 ' (Output)' 结尾。

    class_type → kind：
      - PreviewImage / SaveImage / SaveImageExtended → 'image'
      - 其余                                          → 'other'
    """
    outputs: list[ComfyDynamicOutput] = []

    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        meta = node.get("_meta") or {}
        title = meta.get("title")
        if not isinstance(title, str) or not title.endswith(OUTPUT_SUFFIX):
            continue

        base = _strip_suffix(title, OUTPUT_SUFFIX)
        class_type = node.get("class_type")

        kind: ComfyOutputKind = "image" if class_type in (
            "PreviewImage",
            "SaveImage",
            "SaveImageExtended",
        ) else "other"

        outputs.append(
            {
                "node_id": str(node_id),
                "key": base,
                "title": base,
                "kind": kind,
            }
        )

    return outputs


def patch_workflow(
    base_json: dict[str, Any],
    input_values: dict[str, Any],
) -> dict[str, Any]:
    """
    按 node_id 把外部值写回工作流（移植 Muse 的 patchWorkflow）：

      - LoadImage 系      → inputs.image（文件名；本地路径由 ComfyUIRunner 先经
                            /upload/image 预上传并改写为服务端文件名）
      - CLIPTextEncode 系 → inputs.text
      - 其余              → inputs.value

    返回深拷贝，不修改入参；value 为 None 或 node_id 不存在时跳过。
    """
    patched = copy.deepcopy(base_json)

    for node_id, value in input_values.items():
        if value is None:
            continue
        node = patched.get(node_id)
        if not isinstance(node, dict):
            continue

        inputs = node.setdefault("inputs", {})
        if not isinstance(inputs, dict):
            continue
        class_type = node.get("class_type") or ""

        if class_type in _IMAGE_CLASSES:
            inputs["image"] = value
        elif class_type in _TEXT_CLASSES:
            inputs["text"] = value
        else:
            inputs["value"] = value

    return patched


def input_node_ids(workflow: dict[str, Any]) -> dict[str, str]:
    """便捷索引：{输入 key: node_id}，便于按业务名注入（如 '提示词' → '4'）。

    同名 (Input) 节点时后者生效并告警——模板里应避免重名标题。
    """
    result: dict[str, str] = {}
    for item in parse_dynamic_inputs(workflow):
        if item["key"] in result:
            logger.warning(
                "工作流存在重名 (Input) 节点 '%s'（node %s 与 %s），后者生效",
                item["key"], result[item["key"]], item["node_id"],
            )
        result[item["key"]] = item["node_id"]
    return result


__all__ = [
    "ComfyInputKind",
    "ComfyOutputKind",
    "ComfyDynamicInput",
    "ComfyDynamicOutput",
    "parse_dynamic_inputs",
    "parse_dynamic_outputs",
    "patch_workflow",
    "input_node_ids",
]
