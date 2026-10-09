"""tests/test_workflow_parse.py — (Input)/(Output) 后缀解析与 patch_workflow 注入单测（无外部依赖）

fixtures 为 M1 真实导出的 API JSON（generate 补挂了 SaveImage「效果图 (Output)」）。
"""

from app.services.workflow_parse import (
    input_node_ids,
    parse_dynamic_inputs,
    parse_dynamic_outputs,
    patch_workflow,
)


def _inputs_by_key(workflow: dict) -> dict[str, dict]:
    return {item["key"]: item for item in parse_dynamic_inputs(workflow)}


def _outputs_by_key(workflow: dict) -> dict[str, dict]:
    return {item["key"]: item for item in parse_dynamic_outputs(workflow)}


class TestParseLineart:
    def test_inputs(self, lineart_workflow):
        inputs = _inputs_by_key(lineart_workflow)
        assert set(inputs) == {"实拍图"}
        assert inputs["实拍图"]["kind"] == "image"
        assert inputs["实拍图"]["node_id"] == "1"

    def test_outputs(self, lineart_workflow):
        outputs = _outputs_by_key(lineart_workflow)
        assert set(outputs) == {"线稿", "结构线", "深度图", "分割图"}
        assert all(o["kind"] == "image" for o in outputs.values())


class TestParseGenerate:
    def test_inputs(self, generate_workflow):
        inputs = _inputs_by_key(generate_workflow)
        assert set(inputs) == {"提示词", "负面词", "线稿", "深度图", "SD15 图"}
        # CLIPTextEncode → text（提示词注入 inputs.text）
        assert inputs["提示词"]["kind"] == "text"
        assert inputs["提示词"]["node_id"] == "11"
        assert inputs["负面词"]["kind"] == "text"
        assert inputs["线稿"]["kind"] == "image"
        assert inputs["深度图"]["kind"] == "image"
        assert inputs["SD15 图"]["kind"] == "image"
        # CLIPTextEncode 默认值取自 inputs.text
        assert "interior design" in inputs["提示词"]["default_value"]

    def test_outputs(self, generate_workflow):
        outputs = _outputs_by_key(generate_workflow)
        assert set(outputs) == {"效果图"}
        assert outputs["效果图"]["kind"] == "image"
        assert outputs["效果图"]["node_id"] == "27"

    def test_input_node_ids(self, generate_workflow):
        ids = input_node_ids(generate_workflow)
        assert ids["提示词"] == "11"
        assert ids["SD15 图"] == "25"


class TestParseEdgeCases:
    def test_unsupported_input_class_ignored(self):
        wf = {
            "1": {
                "class_type": "SomeExoticNode",
                "inputs": {},
                "_meta": {"title": "怪节点 (Input)"},
            }
        }
        assert parse_dynamic_inputs(wf) == []

    def test_missing_meta_ignored(self):
        wf = {"1": {"class_type": "LoadImage", "inputs": {"image": "a.png"}}}
        assert parse_dynamic_inputs(wf) == []
        assert parse_dynamic_outputs(wf) == []

    def test_non_dict_node_ignored(self):
        wf = {"1": None, "2": "broken"}
        assert parse_dynamic_inputs(wf) == []
        assert parse_dynamic_outputs(wf) == []

    def test_primitive_number_input(self):
        """PrimitiveInt/Float → number（真实模板暂无，用内联样例覆盖）。"""
        wf = {
            "1": {
                "class_type": "PrimitiveInt",
                "inputs": {"value": 20},
                "_meta": {"title": "强度 (Input)"},
            },
            "2": {
                "class_type": "PrimitiveFloat",
                "inputs": {"value": 0.5},
                "_meta": {"title": "比率 (Input)"},
            },
        }
        inputs = _inputs_by_key(wf)
        assert inputs["强度"]["kind"] == "number"
        assert inputs["强度"]["default_value"] == 20
        assert inputs["比率"]["kind"] == "number"


class TestPatchWorkflow:
    def test_clip_text_encode_goes_to_text(self, generate_workflow):
        patched = patch_workflow(generate_workflow, {"11": "日式原木风，暖光"})
        assert patched["11"]["inputs"]["text"] == "日式原木风，暖光"

    def test_load_image_goes_to_image(self, generate_workflow):
        patched = patch_workflow(generate_workflow, {"13": "/data/outputs/proj_x/lineart.png"})
        assert patched["13"]["inputs"]["image"] == "/data/outputs/proj_x/lineart.png"

    def test_primitive_goes_to_value(self):
        wf = {
            "1": {
                "class_type": "PrimitiveInt",
                "inputs": {"value": 20},
                "_meta": {"title": "强度 (Input)"},
            }
        }
        patched = patch_workflow(wf, {"1": 8})
        assert patched["1"]["inputs"]["value"] == 8

    def test_unknown_node_and_none_skipped(self, generate_workflow):
        patched = patch_workflow(generate_workflow, {"999": "x", "11": None})
        assert "999" not in patched
        assert patched["11"]["inputs"]["text"] == generate_workflow["11"]["inputs"]["text"]

    def test_original_not_mutated(self, generate_workflow):
        original_text = generate_workflow["11"]["inputs"]["text"]
        patch_workflow(generate_workflow, {"11": "新提示词"})
        assert generate_workflow["11"]["inputs"]["text"] == original_text

    def test_full_injection_by_key(self, generate_workflow):
        """模拟业务侧用法：按 key 找 node_id 再注入。"""
        ids = input_node_ids(generate_workflow)
        patched = patch_workflow(
            generate_workflow,
            {
                ids["提示词"]: "北欧风客厅",
                ids["负面词"]: "低质量",
                ids["线稿"]: "outputs/proj_x/lineart.png",
            },
        )
        assert patched["11"]["inputs"]["text"] == "北欧风客厅"
        assert patched["12"]["inputs"]["text"] == "低质量"
        assert patched["13"]["inputs"]["image"] == "outputs/proj_x/lineart.png"


class TestDuplicateInputKeys:
    def test_duplicate_input_key_warns_and_last_wins(self, caplog):
        wf = {
            "1": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "a"},
                "_meta": {"title": "提示词 (Input)"},
            },
            "2": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "b"},
                "_meta": {"title": "提示词 (Input)"},
            },
        }
        with caplog.at_level("WARNING", logger="comshinei"):
            ids = input_node_ids(wf)
        assert ids["提示词"] == "2"
        assert any("重名" in r.message for r in caplog.records)
