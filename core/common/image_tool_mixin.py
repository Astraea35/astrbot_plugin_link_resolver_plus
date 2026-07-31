import hashlib
import time
from pathlib import Path

import httpx
import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter

from .media.upscaler import UPSCAYL_MODEL_NAME_MAP
from .paths import get_cache_path


COMMAND_MODEL_ALIASES = {
    "自动": "auto",
    "CV": "auto",
    "二次元": "digital-art-4x",
    "高保真": "high-fidelity-4x",
    "Remacri": "remacri-4x",
    "remacri": "remacri-4x",
    "超混合平衡": "ultramix-balanced-4x",
    "超锐化": "ultrasharp-4x",
    "轻量": "upscayl-lite-4x",
    "标准": "upscayl-standard-4x",
}


class ImageToolMixin:
    """Standalone image commands backed by the shared media pipeline."""

    def _image_tool_cache_dir(self) -> Path:
        path = get_cache_path() / "image_tool"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def _extract_image_url_for_tool(self, event: AstrMessageEvent) -> str | None:
        message = getattr(getattr(event, "message_obj", None), "message", [])
        for component in message:
            if isinstance(component, Comp.Reply):
                try:
                    bot = getattr(event, "bot", None)
                    call_action = getattr(bot, "call_action", None) or getattr(
                        getattr(bot, "api", None), "call_action", None
                    )
                    if call_action:
                        result = await call_action("get_msg", message_id=component.id)
                        payload = result.get("message", []) if isinstance(result, dict) else []
                        if isinstance(payload, list):
                            for item in payload:
                                data = item.get("data", {}) if isinstance(item, dict) else {}
                                if item.get("type") == "image" and data.get("url"):
                                    return data["url"]
                except Exception as exc:
                    logger.warning("Failed to extract image from reply: %s", exc)

        for component in message:
            if isinstance(component, Comp.Image) and getattr(component, "url", None):
                return component.url
        return None

    async def _download_tool_image(self, url: str) -> Path:
        max_bytes = int(self._get_config_value("image_tool_settings.max_image_bytes", 52428800))
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            response = await client.get(url)
            response.raise_for_status()
            content = response.content

        if len(content) > max_bytes:
            raise ValueError(f"图片大小超过限制（最大 {max_bytes // 1048576} MB）")

        digest = hashlib.md5(content).hexdigest()
        path = self._image_tool_cache_dir() / f"{digest}_raw.png"
        if not path.exists():
            path.write_bytes(content)
        return path

    @staticmethod
    def _command_argument(event: AstrMessageEvent) -> str:
        text = str(getattr(event, "message_str", "") or "").strip()
        for prefix in ("/升图", "升图"):
            if text.startswith(prefix):
                return text[len(prefix):].strip()
        if text in COMMAND_MODEL_ALIASES:
            return text
        return ""

    def _resolve_command_model(self, argument: str) -> str | None:
        """Return a model id; None means use the configured model."""
        if not argument:
            return None
        return COMMAND_MODEL_ALIASES.get(argument)

    async def _select_image_tool_model(self, input_path: Path, argument: str) -> str:
        selected = self._resolve_command_model(argument)
        if selected and selected != "auto":
            return selected

        configured = str(getattr(self, "image_tool_model_name", "auto"))
        configured_model = UPSCAYL_MODEL_NAME_MAP.get(configured)
        if configured_model is None:
            configured_model = next(
                (
                    model
                    for alias, model in COMMAND_MODEL_ALIASES.items()
                    if configured == alias or configured.startswith(f"{alias} ")
                ),
                configured,
            )
        if configured_model not in ("auto", "None", "") and "自动" not in configured:
            return configured_model

        self.current_task_info["stage"] = "CV 图片类型检测中"
        _, _, recommended_model = await self.upscaler.check_is_low_quality(
            input_path,
            threshold=getattr(self, "xhs_low_quality_threshold", 1080),
            model_setting="自动 (CV特征识别)",
        )
        return recommended_model

    async def _run_image_tool(self, event: AstrMessageEvent, command: str, upscale: bool):
        if not getattr(self, "image_tool_enabled", True):
            yield event.plain_result("独立图片工具已在插件配置中关闭。")
            return

        argument = self._command_argument(event) if upscale else ""
        if argument and argument not in COMMAND_MODEL_ALIASES:
            supported = "、".join(COMMAND_MODEL_ALIASES)
            yield event.plain_result(f"未知升图模型。可用参数：{supported}")
            return

        url = await self._extract_image_url_for_tool(event)
        if not url:
            yield event.plain_result(f"请引用一张图片后发送 /{command}。")
            return

        self.image_tool_waiting = getattr(self, "image_tool_waiting", 0) + 1
        acquired = False
        task_info = None
        try:
            input_path = await self._download_tool_image(url)
            async with self.heavy_task_lock:
                acquired = True
                self.image_tool_waiting = max(0, self.image_tool_waiting - 1)
                task_info = {
                    "title": input_path.name,
                    "user": str(event.get_sender_name() or event.get_sender_id()),
                    "current_img": 1,
                    "total_img": 1,
                    "stage": "准备图片处理",
                    "percent": "0.0%",
                    "start_time": time.time(),
                }
                self.current_task_info = task_info

                result_path = input_path
                if upscale:
                    model = await self._select_image_tool_model(input_path, argument)
                    task_info["stage"] = f"AI 升图处理中 ({model})"
                    result_path = await self.upscaler.upscale_image(
                        input_path,
                        f"image-tool-{input_path.stem}-{model}",
                        override_model=model,
                    )

                task_info["stage"] = "AVIF 转码处理中"
                avif_path = await self.encoder.compress_avif(
                    result_path, f"image-tool-{input_path.stem}"
                )
                if not avif_path:
                    yield event.plain_result("AVIF 转码失败，请检查 FFmpeg 配置。")
                    return

                task_info["stage"] = "发送文件"
                task_info["percent"] = "100.0%"
                if not await self._send_file_via_api(event, avif_path):
                    yield event.plain_result("文件发送失败，请检查当前协议适配器。")
        except Exception as exc:
            logger.error("Image tool command failed: %s", exc)
            yield event.plain_result(f"图片处理失败：{exc}")
        finally:
            if not acquired:
                self.image_tool_waiting = max(0, getattr(self, "image_tool_waiting", 1) - 1)
            if task_info is not None and getattr(self, "current_task_info", None) is task_info:
                self.current_task_info = None

    @filter.command("升图")
    async def cmd_image_tool_upscale(self, event: AstrMessageEvent):
        yield event.plain_result("已加入图片处理队列，正在执行 AI 升图和 AVIF 转码。")
        async for result in self._run_image_tool(event, "升图", upscale=True):
            yield result

    @filter.command("avif")
    async def cmd_image_tool_avif(self, event: AstrMessageEvent):
        yield event.plain_result("已加入图片处理队列，正在执行 AVIF 转码。")
        async for result in self._run_image_tool(event, "avif", upscale=False):
            yield result
