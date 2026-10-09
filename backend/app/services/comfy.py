"""app/services/comfy.py — ComfyUIRunner：提交 + HTTP 轮询 + 下载输出 + 媒体预上传

移植自 Muse-Studio muse_backend/app/comfyui_runner.py，按本项目裁剪（仅图片产物）：

- 完成判定只用 HTTP 轮询 GET /history/{prompt_id}，不用 WebSocket。
  （Muse 实测教训：自定义节点可能发出虚假的 execution_error WS 事件，WS 不可靠；
  见其上 _wait_for_completion 注释。WS 后续仅作可选的采样步数进度源。）
- 媒体输入先 POST /upload/image 预上传，再把节点里的路径改写为服务端文件名。
- 输出按 (Output) 后缀节点（workflow_parse.parse_dynamic_outputs）匹配下载，
  返回 {输出 key: 本地路径}；一个节点出多图时全部下载，多出的图记为 "key#2"… 并告警。
- 产物文件名统一加 key 前缀，避免不同节点/不同批次同名覆盖。
- 整体超时可配（COMFYUI_TIMEOUT，默认 300s），超时先 best-effort 调 /interrupt。
  进度百分比由调用方（LangGraph 节点）按节点粒度 emit，本类不逐采样步推送。
"""

from __future__ import annotations

import asyncio
import mimetypes
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx

from app.config import settings
from app.services.workflow_parse import ComfyDynamicOutput
from app.utils.logger import logger

OnProgress = Callable[[int, str], Awaitable[None]]

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif")


def _fs_safe(name: str) -> str:
    """文件名安全化：保留中英文、数字、点、横线、下划线，其余替换为 _。"""
    return re.sub(r"[^\w.\-]+", "_", name)


@dataclass
class ComfyRunResult:
    success: bool
    outputs: dict[str, Path] = field(default_factory=dict)  # (Output) key → 本地路径
    error: Optional[str] = None
    prompt_id: Optional[str] = None


@dataclass
class ComfyUIRunner:
    base_url: str
    timeout: float = 300.0        # 整体执行超时（秒）
    poll_interval: float = 1.5    # /history 轮询间隔（秒）

    @classmethod
    def from_settings(cls) -> "ComfyUIRunner":
        return cls(base_url=settings.comfy_base_url, timeout=float(settings.COMFYUI_TIMEOUT))

    async def run(
        self,
        workflow: dict[str, Any],
        output_dir: Path,
        outputs_spec: Optional[list[ComfyDynamicOutput]] = None,
        on_progress: Optional[OnProgress] = None,
    ) -> ComfyRunResult:
        """
        提交注入好参数的工作流并等待完成，下载全部 (Output) 产物到 output_dir。

        outputs_spec 为 parse_dynamic_outputs 的结果；不传则下载历史中的全部图片，
        以 node_id 为 key。任何阶段失败都收敛为 ComfyRunResult(success=False, error=...)，
        不向外抛异常。
        """
        client_id = f"comshinei-{uuid.uuid4().hex[:12]}"

        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0)) as client:
            # 0) 媒体预上传：LoadImage 节点里的本地文件 → ComfyUI input 目录
            try:
                await self._prepare_media_inputs(client, workflow)
            except Exception as exc:  # noqa: BLE001
                return ComfyRunResult(False, error=str(exc))

            # 1) 提交 prompt
            try:
                submit_resp = await client.post(
                    f"{self.base_url}/prompt",
                    json={"client_id": client_id, "prompt": workflow},
                )
                submit_resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                return ComfyRunResult(False, error=f"ComfyUI 提交失败: {exc}")
            payload = submit_resp.json()
            prompt_id = payload.get("prompt_id")
            if not prompt_id:
                return ComfyRunResult(False, error="ComfyUI 未返回 prompt_id")

            logger.info("ComfyUI prompt submitted: %s", prompt_id)

            # 2) HTTP 轮询 /history 直到完成（含执行错误检测与整体超时）
            try:
                await self._poll_until_done(prompt_id, on_progress)
            except Exception as exc:  # noqa: BLE001
                return ComfyRunResult(False, error=f"ComfyUI 执行失败: {exc}", prompt_id=prompt_id)

            # 3) 按 (Output) 节点匹配输出并下载
            try:
                outputs = await self._download_outputs(client, prompt_id, outputs_spec, output_dir)
            except Exception as exc:  # noqa: BLE001
                return ComfyRunResult(False, error=f"ComfyUI 产物下载失败: {exc}", prompt_id=prompt_id)

        return ComfyRunResult(True, outputs=outputs, prompt_id=prompt_id)

    async def interrupt(self) -> bool:
        """中断 ComfyUI 当前队列（用于项目取消 / 超时清理）。"""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                resp = await client.post(f"{self.base_url}/interrupt")
                resp.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("ComfyUI /interrupt failed: %s", exc)
            return False

    async def ping(self) -> bool:
        """探活：GET /system_stats。"""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
                resp = await client.get(f"{self.base_url}/system_stats")
                return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    async def _poll_until_done(
        self,
        prompt_id: str,
        on_progress: Optional[OnProgress],
    ) -> None:
        """
        轮询 GET /history/{prompt_id}：出现历史记录即视为执行结束；
        历史记录 status.status_str == 'error' 抛执行错误；瞬时 5xx 重试不判死；
        超时先 best-effort /interrupt 再抛 TimeoutError。
        """
        start_ts = time.time()
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            while True:
                if time.time() - start_ts > self.timeout:
                    await self.interrupt()
                    raise TimeoutError(f"ComfyUI 执行超时（{self.timeout:.0f}s，prompt_id={prompt_id}）")

                try:
                    resp = await client.get(f"{self.base_url}/history/{prompt_id}")
                except httpx.HTTPError as exc:
                    # 网络瞬时抖动不判死，继续轮询直到超时
                    logger.warning("history 轮询网络错误，继续等待: %s", exc)
                    await asyncio.sleep(self.poll_interval)
                    continue

                if resp.status_code == 404:
                    await asyncio.sleep(self.poll_interval)
                    continue
                if resp.status_code >= 500:
                    logger.warning("history 轮询收到 %s，继续等待", resp.status_code)
                    await asyncio.sleep(self.poll_interval)
                    continue
                resp.raise_for_status()

                history = resp.json() or {}
                if not history:
                    await asyncio.sleep(self.poll_interval)
                    continue

                item = next(iter(history.values()), {}) or {}
                status = item.get("status") or {}
                if status.get("status_str") == "error":
                    messages = status.get("messages") or []
                    raise RuntimeError(f"ComfyUI 执行出错: {messages}")

                if on_progress:
                    await on_progress(100, "ComfyUI history ready.")
                return

    async def _download_outputs(
        self,
        client: httpx.AsyncClient,
        prompt_id: str,
        outputs_spec: Optional[list[ComfyDynamicOutput]],
        output_dir: Path,
    ) -> dict[str, Path]:
        """
        按 (Output) 节点从 /history 匹配输出记录，经 GET /view 下载到本地。

        - 落盘文件名加 key 前缀（{key}_{原文件名}），防不同节点/批次同名覆盖；
          ComfyUI 返回的 filename 只取 basename，防路径逃逸。
        - 一个节点出多图时全部下载：第一张记 key，其余记 "key#2"、"key#3"… 并告警。
        """
        resp = await client.get(f"{self.base_url}/history/{prompt_id}")
        resp.raise_for_status()
        hist = resp.json()
        if not isinstance(hist, dict) or not hist:
            raise RuntimeError("ComfyUI history 为空")

        # 结构：{ prompt_id: { 'outputs': { node_id: { 'images': [...] } } } }
        item = next(iter(hist.values()))
        node_outputs: dict[str, Any] = item.get("outputs") or {}

        if outputs_spec is not None:
            # (key, node_id, 严格)——声明了 (Output) 却拿不到产物属于异常
            wanted = [(s["key"], s["node_id"], True) for s in outputs_spec if s["kind"] == "image"]
            if not wanted:
                raise RuntimeError("工作流没有 image 类型的 (Output) 节点，无产物可下载")
        else:
            # 兜底模式：下载所有出了图的节点，无图节点跳过
            wanted = [
                (str(nid), str(nid), False)
                for nid, data in node_outputs.items()
                if isinstance(data, dict) and data.get("images")
            ]

        output_dir.mkdir(parents=True, exist_ok=True)
        results: dict[str, Path] = {}
        for key, node_id, strict in wanted:
            node_data = node_outputs.get(node_id)
            images = (
                [r for r in (node_data.get("images") or []) if isinstance(r, dict)]
                if isinstance(node_data, dict)
                else []
            )
            if not images:
                if strict:
                    raise RuntimeError(f"(Output) 节点 {node_id}（{key}）在 history 中无图片产物")
                continue

            if len(images) > 1:
                logger.warning("(Output) 节点 %s（%s）产出 %d 张图，全部下载", node_id, key, len(images))

            safe_key = _fs_safe(key)
            for idx, rec in enumerate(images):
                filename = rec.get("filename")
                if not filename:
                    raise RuntimeError(f"(Output) 节点 {node_id}（{key}）输出记录缺 filename")
                filename = Path(str(filename)).name

                params = {
                    "filename": rec.get("filename"),
                    "subfolder": rec.get("subfolder", ""),
                    "type": rec.get("type", "output"),
                }
                download_resp = await client.get(f"{self.base_url}/view", params=params)
                download_resp.raise_for_status()

                local_name = f"{safe_key}_{filename}" if idx == 0 else f"{safe_key}_{idx + 1}_{filename}"
                out_path = output_dir / local_name
                out_path.write_bytes(download_resp.content)

                result_key = key if idx == 0 else f"{key}#{idx + 1}"
                results[result_key] = out_path
                logger.info("ComfyUI output downloaded: %s -> %s", result_key, out_path)

        return results

    async def _prepare_media_inputs(self, client: httpx.AsyncClient, workflow: dict[str, Any]) -> None:
        """
        扫描 LoadImage 节点：inputs.image 为本地路径时，先 POST /upload/image
        上传到 ComfyUI input 目录，再把节点值改写为服务端文件名。

        远程文件名加 uuid 前缀，避免不同项目同名文件互相覆盖。
        引用了本地路径但文件不存在/格式不支持时立即抛错（快速失败，不让 ComfyUI 背锅）。
        """
        media_nodes: list[tuple[str, str]] = []
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            if node.get("class_type") != "LoadImage":
                continue
            inputs = node.get("inputs") or {}
            value = inputs.get("image")
            if isinstance(value, str):
                media_nodes.append((str(node_id), value))

        for node_id, value in media_nodes:
            # 裸文件名（无路径分隔符）视为 ComfyUI 已可见，跳过
            if "/" not in value and "\\" not in value:
                continue

            local_path = Path(value)
            if not local_path.is_absolute():
                local_path = settings.resolve_dir(settings.UPLOAD_DIR) / value
            if not local_path.exists():
                raise RuntimeError(f"媒体预上传失败：本地文件不存在: {local_path}")
            if local_path.suffix.lower() not in _IMAGE_EXTS:
                raise RuntimeError(f"媒体预上传失败：不支持的图片格式: {local_path}")

            remote_name = f"{uuid.uuid4().hex[:8]}_{local_path.name}"
            mime_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"

            try:
                with local_path.open("rb") as f:
                    files = {"image": (remote_name, f, mime_type)}
                    data = {"type": "input", "overwrite": "true"}
                    resp = await client.post(f"{self.base_url}/upload/image", files=files, data=data)
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"媒体预上传失败（{local_path}）: {exc}") from exc

            uploaded = resp.json()
            server_name = uploaded.get("name") or remote_name
            workflow[node_id]["inputs"]["image"] = server_name
            logger.info("ComfyUI media uploaded: %s -> %s", local_path, server_name)


__all__ = ["ComfyUIRunner", "ComfyRunResult", "OnProgress"]
