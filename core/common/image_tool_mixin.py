import hashlib
import re
import time
from pathlib import Path
from urllib.parse import unquote

import httpx
from PIL import Image as PILImage
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image, Reply

from .media.annotations import (
    build_image_processing_annotation_text,
    format_image_processing_annotation,
)
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

    @staticmethod
    def _extract_cq_image_urls(text: str) -> list[str]:
        if not text:
            return []
        urls: list[str] = []
        for match in re.finditer(r"\[CQ:image,([^\]]+)\]", text):
            params = match.group(1)
            url_match = re.search(r"(?:^|,)url=([^,]+)", params)
            if url_match:
                urls.append(unquote(url_match.group(1)))
            else:
                file_match = re.search(r"(?:^|,)file=([^,]+)", params)
                if file_match:
                    val = unquote(file_match.group(1))
                    if val.startswith(("http://", "https://", "file://")) or Path(val).is_file():
                        urls.append(val)
        return urls

    async def _extract_image_urls_for_tool(self, event: AstrMessageEvent) -> list[str]:
        message = getattr(getattr(event, "message_obj", None), "message", [])
        urls: list[str] = []

        for component in message:
            if isinstance(component, Reply) or type(component).__name__ == "Reply":
                reply_id = getattr(component, "id", None) or getattr(component, "message_id", None)
                if reply_id:
                    try:
                        bot = getattr(event, "bot", None)
                        call_action = getattr(bot, "call_action", None) or getattr(
                            getattr(bot, "api", None), "call_action", None
                        )
                        if call_action:
                            result = await call_action("get_msg", message_id=reply_id)
                            payload = []
                            raw_msg = ""
                            if isinstance(result, dict):
                                payload = result.get("message") or result.get("data", {}).get("message") or []
                                raw_msg = result.get("raw_message") or result.get("data", {}).get("raw_message") or ""
                            elif isinstance(result, list):
                                payload = result
                            else:
                                raw_msg = str(result)

                            if isinstance(payload, list):
                                for item in payload:
                                    image_url = self._image_url_from_component(item)
                                    if image_url:
                                        urls.append(image_url)
                            elif isinstance(payload, str):
                                urls.extend(self._extract_cq_image_urls(payload))

                            if not urls and raw_msg:
                                urls.extend(self._extract_cq_image_urls(raw_msg))
                    except Exception as exc:
                        logger.warning("Failed to extract image from reply: %s", exc)

        # If replied message contained images, prioritize them
        if urls:
            return list(dict.fromkeys(urls))

        # Check current message components
        for component in message:
            image_url = self._image_url_from_component(component)
            if image_url:
                urls.append(image_url)

        # Fallback to raw_message / message_str CQ codes
        if not urls:
            raw_message = str(
                getattr(getattr(event, "message_obj", None), "raw_message", "")
                or getattr(event, "message_str", "")
                or ""
            )
            if raw_message:
                urls.extend(self._extract_cq_image_urls(raw_message))

        return list(dict.fromkeys(urls))

    async def _extract_image_url_for_tool(self, event: AstrMessageEvent) -> str | None:
        """Backward-compatible helper returning the first extracted image URL."""
        urls = await self._extract_image_urls_for_tool(event)
        return urls[0] if urls else None

    @staticmethod
    def _image_url_from_component(component) -> str | None:
        if isinstance(component, dict):
            if str(component.get("type", "")).lower() != "image":
                return None
            data = component.get("data") or component
            if isinstance(data, dict):
                for key in ("url", "file", "path"):
                    value = data.get(key)
                    if value:
                        return str(value)
            return None

        if isinstance(component, Image) or type(component).__name__ == "Image":
            for key in ("url", "file", "path"):
                value = getattr(component, key, None)
                if value:
                    return str(value)
        return None

    async def _download_tool_image(self, url: str) -> Path:
        max_image_mb = self._get_config_value("image_tool_settings.max_image_mb", None)
        if max_image_mb is None:
            # Keep existing byte-based configurations working after the unit change.
            legacy_max_bytes = int(
                self._get_config_value("image_tool_settings.max_image_bytes", 52428800)
            )
            max_image_mb = legacy_max_bytes // 1048576 if legacy_max_bytes > 0 else 0
        max_image_mb = max(0, int(max_image_mb))
        max_bytes = max_image_mb * 1048576
        local_path = Path(url.removeprefix("file://"))
        if local_path.is_file():
            content = local_path.read_bytes()
        else:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                response = await client.get(url)
                response.raise_for_status()
                content = response.content

        if max_image_mb > 0 and len(content) > max_bytes:
            raise ValueError(f"图片大小超过限制（最大 {max_bytes // 1048576} MB）")

        digest = hashlib.md5(content).hexdigest()
        path = self._image_tool_cache_dir() / f"{digest}_raw.png"
        if not path.exists():
            path.write_bytes(content)
        return path

    @staticmethod
    def _command_argument(event: AstrMessageEvent) -> str:
        text = str(getattr(event, "message_str", "") or "").strip()
        text = re.sub(r"\[CQ:[^\]]+\]", "", text).strip()
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

    async def _select_image_tool_metadata(
        self, input_path: Path, argument: str
    ) -> tuple[str, str]:
        selected = self._resolve_command_model(argument)
        if selected and selected != "auto":
            return selected, f"手动指定({selected})"

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
            return configured_model, f"手动指定({configured_model})"

        self.current_task_info["stage"] = "CV 图片类型检测中"
        _, image_type, recommended_model = await self.upscaler.check_is_low_quality(
            input_path,
            threshold=getattr(self, "low_quality_threshold", 2160),
            model_setting="自动 (CV特征识别)",
        )
        return recommended_model, image_type

    async def _select_image_tool_model(self, input_path: Path, argument: str) -> str:
        """Return the selected model while keeping the legacy helper contract."""
        model, _ = await self._select_image_tool_metadata(input_path, argument)
        return model

    @staticmethod
    def _image_dimensions(input_path: Path) -> tuple[int, int]:
        with PILImage.open(input_path) as image:
            return image.width, image.height

    async def _run_image_tool(self, event: AstrMessageEvent, command: str, upscale: bool):
        if not getattr(self, "image_tool_enabled", True):
            yield event.plain_result("独立图片工具已在插件配置中关闭。")
            return

        argument = self._command_argument(event) if upscale else ""
        if argument and argument not in COMMAND_MODEL_ALIASES:
            supported = "、".join(COMMAND_MODEL_ALIASES)
            yield event.plain_result(f"未知升图模型。可用参数：{supported}")
            return

        urls = await self._extract_image_urls_for_tool(event)
        if not urls:
            yield event.plain_result(f"请发送图片或引用包含图片的消息后发送 /{command}。")
            return

        self.image_tool_waiting = getattr(self, "image_tool_waiting", 0) + 1
        acquired = False
        task_info = None
        try:
            total_images = len(urls)
            if getattr(self, "current_task_info", None) is None:
                task_info = {
                    "title": "图片工具",
                    "user": str(event.get_sender_name() or event.get_sender_id()),
                    "current_img": 0,
                    "total_img": total_images,
                    "stage": "下载图片",
                    "percent": "0.0%",
                    "start_time": time.time(),
                }
                self.current_task_info = task_info

            download_start = time.perf_counter()
            downloaded_images: list[tuple[str, Path]] = []
            for i, url in enumerate(urls, start=1):
                if task_info:
                    task_info["current_img"] = i
                    task_info["stage"] = f"下载图片 ({i}/{total_images})"
                logger.info("📥 图片工具下载开始 [%d/%d]", i, total_images)
                try:
                    input_path = await self._download_tool_image(url)
                    logger.info(
                        "📥 图片工具下载成功 [%d/%d]: %s, size=%.1fKB, 耗时=%.2fs",
                        i,
                        total_images,
                        input_path.name,
                        input_path.stat().st_size / 1024,
                        time.perf_counter() - download_start,
                    )
                    downloaded_images.append((url, input_path))
                except Exception as exc:
                    logger.warning("⚠️ 图片工具下载失败 [%d/%d]: %s", i, total_images, exc)

            if not downloaded_images:
                yield event.plain_result("图片下载失败，请检查图片链接或网络连接。")
                return

            for i, (url, input_path) in enumerate(downloaded_images, start=1):
                await self._prepare_image_metadata(
                    input_path,
                    {
                        "platform": "image_tool",
                        "url": url,
                        "image_index": i,
                        "image_count": len(downloaded_images),
                    },
                )

            async with self.media_whole_job:
                acquired = True
                self.image_tool_waiting = max(0, self.image_tool_waiting - 1)
                total_imgs = len(downloaded_images)
                if task_info is None or getattr(self, "current_task_info", None) is not task_info:
                    task_info = {
                        "title": downloaded_images[0][1].name,
                        "user": str(event.get_sender_name() or event.get_sender_id()),
                        "current_img": 0,
                        "total_img": total_imgs,
                        "stage": "准备图片处理",
                        "percent": "0.0%",
                        "start_time": time.time(),
                    }
                    self.current_task_info = task_info

                annotations: list[str] = []
                processing_timings: list[dict[str, float]] = []
                send_failed_count = 0

                for i, (url, input_path) in enumerate(downloaded_images, start=1):
                    task_info["title"] = input_path.name
                    task_info["current_img"] = i
                    task_info["total_img"] = total_imgs
                    task_info["stage"] = f"准备图片处理 ({i}/{total_imgs})"
                    task_info["percent"] = "0.0%"

                    result_path = input_path
                    was_upscaled = False
                    image_type = "未检测"
                    target_model = None
                    upscaled_path = None
                    if upscale:
                        width, height = self._image_dimensions(input_path)
                        resolution_limit = getattr(
                            self, "image_tool_upscayl_max_resolution", 3840
                        )
                        if resolution_limit > 0 and max(width, height) > resolution_limit:
                            image_type = f"超过 AI 升图上限 ({width}x{height})"
                            logger.info(
                                "Image tool skipped AI upscale for %dx%d image [%d/%d]; limit is %dpx",
                                width,
                                height,
                                i,
                                total_imgs,
                                resolution_limit,
                            )
                        else:
                            model, image_type = await self._select_image_tool_metadata(
                                input_path, argument
                            )
                            target_model = model

                    (
                        result_path,
                        _preview_path,
                        was_upscaled,
                        image_type,
                        target_model,
                        upscaled_path,
                        processing_timing,
                    ) = await self._process_image_file(
                        input_path,
                        f"image-tool-{input_path.stem}_{i}",
                        force_upscale_model=target_model,
                        force_upscale_type=image_type,
                        force_upscale_options=(
                            getattr(self, "image_tool_upscayl_scale", 2),
                            getattr(self, "image_tool_upscayl_enable_taa", True),
                            getattr(self, "image_tool_upscayl_double_pass", True),
                        ),
                        generate_preview=False,
                        manage_lock=bool(
                            getattr(self, 'allow_ai_upscale_ffmpeg_concurrent', False)
                        ),
                    )
                    compressed_path = result_path
                    if not compressed_path:
                        logger.error("图片转码失败 [%d/%d]: %s", i, total_imgs, input_path.name)
                        continue

                    task_info["stage"] = f"发送文件 ({i}/{total_imgs})"
                    task_info["percent"] = "100.0%"
                    if not await self._send_file_via_api(event, compressed_path):
                        send_failed_count += 1
                        logger.warning("文件发送失败 [%d/%d]: %s", i, total_imgs, compressed_path.name)

                    annotation = format_image_processing_annotation(
                        i,
                        input_path,
                        compressed_path,
                        was_upscaled,
                        image_type,
                        target_model,
                        upscaled_path,
                        processing_timing,
                    )
                    annotations.append(annotation)
                    processing_timings.append(processing_timing)

                if send_failed_count > 0 and send_failed_count == total_imgs:
                    yield event.plain_result("文件发送失败，请检查当前协议适配器。")
                    return

                annotation_text = build_image_processing_annotation_text(
                    annotations,
                    processing_timings,
                )
                if annotation_text:
                    yield event.plain_result(annotation_text)
        except Exception as exc:
            logger.error("Image tool command failed: %s", exc)
            yield event.plain_result(f"图片处理失败：{exc}")
        finally:
            if not acquired:
                self.image_tool_waiting = max(0, getattr(self, "image_tool_waiting", 1) - 1)
            if task_info is not None and getattr(self, "current_task_info", None) is task_info:
                self.current_task_info = None

    async def cmd_image_tool_upscale(self, event: AstrMessageEvent):
        yield event.plain_result("已加入图片处理队列，正在执行 AI 升图和图片转码。")
        async for result in self._run_image_tool(event, "升图", upscale=True):
            yield result

    async def cmd_image_tool_avif(self, event: AstrMessageEvent):
        yield event.plain_result("已加入图片处理队列，正在执行图片转码。")
        async for result in self._run_image_tool(event, "avif", upscale=False):
            yield result
