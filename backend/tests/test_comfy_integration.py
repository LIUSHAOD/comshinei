"""tests/test_comfy_integration.py — 对真 ComfyUI 跑通 lineart / generate 两条工作流

前置条件：
1. COMFYUI_HOST 指向真实 ComfyUI（默认 127.0.0.1:8188，.env 可改），M1 的模型与
   自定义节点已就绪（comfyui_controlnet_aux / BrushNet / ControlNet 等）；
2. tests/fixtures/ 下为 M1 导出的真实 API JSON（含 (Input)/(Output) 后缀）。

ComfyUI 不可达时自动 skip，不影响本地单测。

运行：uv run pytest -m integration -v
"""

import pytest

from app.services.comfy import ComfyUIRunner
from app.services.workflow_parse import (
    input_node_ids,
    parse_dynamic_outputs,
    patch_workflow,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def runner() -> ComfyUIRunner:
    return ComfyUIRunner.from_settings()


@pytest.fixture(scope="module")
async def comfy_available(runner) -> bool:
    if not await runner.ping():
        pytest.skip(f"ComfyUI 不可达: {runner.base_url}")
    return True


async def test_lineart_then_generate(
    comfy_available,
    runner,
    lineart_workflow,
    generate_workflow,
    test_photo,
    tmp_path,
):
    # ---- 工作流 1：lineart（实拍图 → 线稿 / 结构线 / 深度图 / 分割图）----
    lineart_inputs = input_node_ids(lineart_workflow)
    patched = patch_workflow(lineart_workflow, {lineart_inputs["实拍图"]: str(test_photo)})

    result = await runner.run(
        patched,
        output_dir=tmp_path / "lineart",
        outputs_spec=parse_dynamic_outputs(lineart_workflow),
    )
    assert result.success, result.error
    assert set(result.outputs) == {"线稿", "结构线", "深度图", "分割图"}
    for key, path in result.outputs.items():
        assert path.exists() and path.stat().st_size > 0, f"{key} 产物为空"

    # ---- 工作流 2：generate（线稿 + 深度图 + MLSD + 提示词 → 效果图）----
    gen_inputs = input_node_ids(generate_workflow)
    patched = patch_workflow(
        generate_workflow,
        {
            gen_inputs["线稿"]: str(result.outputs["线稿"]),
            gen_inputs["深度图"]: str(result.outputs["深度图"]),
            gen_inputs["SD15 图"]: str(result.outputs["结构线"]),  # MLSD 结构线图
            gen_inputs["提示词"]: "现代简约客厅，原木家具，暖色灯光，照片级真实感",
            gen_inputs["负面词"]: "低质量，变形，水印",
        },
    )

    gen_result = await runner.run(
        patched,
        output_dir=tmp_path / "generate",
        outputs_spec=parse_dynamic_outputs(generate_workflow),
    )
    assert gen_result.success, gen_result.error
    assert set(gen_result.outputs) == {"效果图"}
    image = gen_result.outputs["效果图"]
    assert image.exists() and image.stat().st_size > 0


async def test_interrupt_noop(comfy_available, runner):
    """空队列上调 /interrupt 应返回成功（取消接口的底层能力）。"""
    assert await runner.interrupt() is True
