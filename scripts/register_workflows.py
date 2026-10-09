"""scripts/register_workflows.py — 把 M1 导出的 ComfyUI 工作流注册到后端

用法（后端已启动）：
    python scripts/register_workflows.py                          # 默认参数
    python scripts/register_workflows.py http://127.0.0.1:8000 lineart.json generate_liu_mlsd.json

默认 backend=http://127.0.0.1:8000，工作流文件取工作区根目录下的
lineart.json / generate_liu_mlsd.json。仅依赖标准库。
注册后打印解析出的 inputs/outputs——若预期输入没出现，说明节点标题缺 " (Input)" 后缀。
"""

import json
import sys
import urllib.request
from pathlib import Path


def register(base: str, workflow_key: str, name: str, path: Path) -> None:
    workflow = json.loads(path.read_text(encoding="utf-8"))
    payload = json.dumps(
        {"workflow_key": workflow_key, "name": name, "kind": "image", "json": workflow}
    ).encode()
    req = urllib.request.Request(
        f"{base}/api/workflows", data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        data = json.load(urllib.request.urlopen(req))["data"]
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        if e.code == 409:
            print(f"[{workflow_key}] 已存在，跳过（{body['message']}）")
            return
        raise
    inputs = [(i["key"], i["kind"]) for i in data["inputs"]]
    outputs = [(o["key"], o["kind"]) for o in data["outputs"]]
    print(f"[{workflow_key}] 注册成功")
    print(f"  inputs : {inputs}")
    print(f"  outputs: {outputs}")


def main() -> None:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    workspace_root = Path(__file__).resolve().parents[2]
    lineart = Path(sys.argv[2]) if len(sys.argv) > 2 else workspace_root / "lineart.json"
    generate = Path(sys.argv[3]) if len(sys.argv) > 3 else workspace_root / "generate_liu_mlsd.json"

    register(base, "lineart", "实拍图→线稿/结构线/深度图/分割图", lineart)
    register(base, "generate", "线稿+提示词→效果图", generate)


if __name__ == "__main__":
    main()
