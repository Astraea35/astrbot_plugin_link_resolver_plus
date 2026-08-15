# core/common/media/encoder.py
import asyncio
import hashlib
import math
import tempfile
import time
from pathlib import Path
from astrbot.api import logger
from .metadata import ImageMetadataStore
from .process import monitor_process_percentage


class MediaEncoder:
    """基于 FFmpeg 的异步图片转码管道（AVIF/JXL 压缩与 JPG 预览）。"""

    _MAX_OUTPUT_PATH_LENGTH = 240
    _METADATA_SIDECAR_LENGTH = len(".metadata.json")

    @classmethod
    def _build_output_path(cls, input_path: Path, suffix: str) -> Path:
        """Build an FFmpeg-safe output path for generated media."""
        input_path = Path(input_path)
        output_path = input_path.with_name(f"{input_path.stem}{suffix}")
        max_path_length = cls._MAX_OUTPUT_PATH_LENGTH - cls._METADATA_SIDECAR_LENGTH
        if len(str(output_path)) <= max_path_length:
            return output_path

        path_hash = hashlib.sha256(
            str(input_path.resolve()).encode("utf-8")
        ).hexdigest()[:16]
        max_name_length = max_path_length - len(str(input_path.parent)) - 1
        name_suffix = f"_{path_hash}{suffix}"
        max_stem_length = max_name_length - len(name_suffix)
        if max_stem_length > 0:
            return input_path.with_name(f"{input_path.stem[:max_stem_length]}{name_suffix}")

        return Path(tempfile.gettempdir()) / "astrbot-link-resolver-plus" / f"{path_hash}{suffix}"

    def __init__(self, plugin_instance):
        self.plugin = plugin_instance
        self.metadata = ImageMetadataStore()

    def _image_compression_options(self, input_path: Path) -> dict[str, object]:
        """Resolve the selected encoder and cache-safe output details."""
        selected_format = str(
            getattr(self.plugin, "image_compress_format", "AVIF")
        ).strip().upper()
        if selected_format.startswith("JXL"):
            is_png = self._is_png(input_path)
            distance = 0.0 if is_png else self._jxl_distance()
            distance_text = self._format_distance(distance)
            cache_tag = "lossless" if is_png else f"d{distance_text}"
            return {
                "format": "JXL",
                "codec": "libjxl",
                "suffix": f"_jxl_{cache_tag}.jxl",
                "label": "JXL",
                "parameters": {"effort": 9, "distance": distance},
                "codec_args": [
                    "-c:v:0", "libjxl",
                    "-effort:v:0", "9",
                    "-distance:v:0", distance_text,
                ],
            }

        return {
            "format": "AVIF",
            "codec": "libaom-av1",
            "suffix": "_av1.avif",
            "label": "AV1",
            "parameters": {
                "cpu_used": 1,
                "crf": 18,
                "still_picture": 1,
                "row_mt": 1,
            },
            "codec_args": [
                "-c:v:0", "libaom-av1",
                "-cpu-used:v:0", "1",
                "-crf:v:0", "18",
                "-b:v:0", "0",
                "-still-picture", "1",
                "-row-mt", "1",
            ],
        }

    def _jxl_distance(self) -> float:
        raw_distance = getattr(self.plugin, "jxl_distance", "1.0")
        try:
            distance = float(str(raw_distance).strip())
        except (TypeError, ValueError):
            return 1.0
        return distance if math.isfinite(distance) and distance >= 0 else 1.0

    @staticmethod
    def _format_distance(distance: float) -> str:
        if distance == 0:
            return "0"
        if distance.is_integer():
            return f"{distance:.1f}"
        return format(distance, ".12g")

    @staticmethod
    def _is_png(input_path: Path) -> bool:
        try:
            with input_path.open("rb") as image_file:
                return image_file.read(8) == b"\x89PNG\r\n\x1a\n"
        except OSError:
            return input_path.suffix.lower() == ".png"

    async def compress_image(
        self,
        input_path: Path,
        request_id: str,
        duration_sec: float | None = None,
    ) -> Path | None:
        """Asynchronously transcode an image to the configured AVIF or JXL format."""
        ffmpeg_bin = getattr(self.plugin, "ffmpeg_bin_path", "ffmpeg")
        options = self._image_compression_options(input_path)
        output_path = self._build_output_path(input_path, str(options["suffix"]))
        metadata_enabled = getattr(self.plugin, "preserve_image_metadata", True)
        metadata = (
            await asyncio.to_thread(self.metadata.ensure, input_path)
            if metadata_enabled
            else None
        )
        processing = {
            "operation": "transcode",
            "format": options["format"],
            "codec": options["codec"],
            "parameters": options["parameters"],
        }

        if output_path.exists():
            age = time.time() - (await asyncio.to_thread(lambda: output_path.stat().st_mtime))
            if age < 7 * 24 * 3600:
                if metadata_enabled and self.metadata.read(output_path) is None:
                    await asyncio.to_thread(self.metadata.finalize, input_path, output_path, processing)
                logger.info("⚡ [Cache Hit] 命中 7 天内的 %s 压缩缓存: %s", options["label"], output_path.name)
                return output_path

        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-i", str(input_path.resolve()),
            "-map", "0:v:0?",
            *options["codec_args"],
            *(
                self.metadata.ffmpeg_args(metadata, processing)
                if metadata_enabled and metadata is not None
                else []
            ),
            str(output_path.resolve()),
        ]
        try:
            await asyncio.to_thread(output_path.parent.mkdir, parents=True, exist_ok=True)
            orig_size = (await asyncio.to_thread(lambda: input_path.stat().st_size)) / 1024
            logger.info("🗜️ [FFmpeg] 开始 %s 压缩: %s (%.1fKB)", options["label"], input_path.name, orig_size)
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            # 耗时打点监控
            await monitor_process_percentage(
                proc,
                f"🗜️ FFmpeg {options['label']} 压缩中",
                self.plugin,
                total_duration_sec=duration_sec,
            )

            if proc.returncode == 0 and output_path.exists():
                new_size = (await asyncio.to_thread(lambda: output_path.stat().st_size)) / 1024
                if metadata_enabled:
                    await asyncio.to_thread(self.metadata.finalize, input_path, output_path, processing)
                logger.info("✅ [FFmpeg] %s 完成: %.1fKB → %.1fKB (%s)", options["label"], orig_size, new_size, output_path.name)
                return output_path
            else:
                await asyncio.to_thread(output_path.unlink, missing_ok=True)
                return None
        except Exception as e:
            logger.error("❌ FFmpeg 压缩异常: %s", str(e))
            await asyncio.to_thread(output_path.unlink, missing_ok=True)
            return None

    async def compress_avif(
        self,
        input_path: Path,
        request_id: str,
        duration_sec: float | None = None,
    ) -> Path | None:
        """Compatibility alias for callers that predate configurable image formats."""
        return await self.compress_image(input_path, request_id, duration_sec)

    async def generate_jpg_preview(self, image_path: Path, request_id: str) -> Path | None:
        """从图片生成轻量 JPG 预览图 (最长边 1920px)，防止合并转发直接发送 10MB+ 的 Upscayl PNG 原图"""
        ffmpeg_bin = getattr(self.plugin, "ffmpeg_bin_path", "ffmpeg")
        preview_path = self._build_output_path(image_path, "_preview.jpg")
        metadata_enabled = getattr(self.plugin, "preserve_image_metadata", True)
        metadata = (
            await asyncio.to_thread(self.metadata.ensure, image_path)
            if metadata_enabled
            else None
        )
        processing = {
            "operation": "preview",
            "format": "JPEG",
            "codec": "mjpeg",
            "parameters": {"max_width": 1920, "max_height": 1920, "quality": 4},
        }

        # 若原图已经是 JPG 且体积 <= 2MB，直接复用
        if image_path.suffix.lower() in ('.jpg', '.jpeg'):
            try:
                if image_path.stat().st_size <= 2 * 1024 * 1024:
                    if metadata_enabled:
                        await asyncio.to_thread(
                            self.metadata.finalize,
                            image_path,
                            image_path,
                            {**processing, "reused": True},
                        )
                    return image_path
            except Exception:
                pass

        # 压缩生成轻量化 JPG 预览图（保持在几百 KB ~ 1.5MB 之间）
        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-i", str(image_path.resolve()),
            "-vf", "scale=1920:1920:force_original_aspect_ratio=decrease",
            "-q:v", "4",
            "-frames:v", "1",
            *(
                self.metadata.ffmpeg_args(metadata, processing)
                if metadata_enabled and metadata is not None
                else []
            ),
            str(preview_path.resolve()),
        ]
        try:
            await asyncio.to_thread(preview_path.parent.mkdir, parents=True, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await monitor_process_percentage(proc, "🖼️ 生成 JPG 预览", self.plugin)
            if proc.returncode == 0 and preview_path.exists():
                if metadata_enabled:
                    await asyncio.to_thread(
                        self.metadata.finalize, image_path, preview_path, processing
                    )
                logger.info("🗼 JPG 预览生成完成: %s (%.1fKB)", preview_path.name, preview_path.stat().st_size / 1024)
                return preview_path
            logger.warning("⚠️ FFmpeg JPG 预览生成失败 (returncode=%s)，降级使用原图", proc.returncode)
            return image_path
        except Exception as e:
            logger.error("❌ JPG 预览生成异常: %s", str(e))
            return image_path

    async def convert_to_avif_with_preview(self, image_path: Path, request_id: str) -> tuple[Path | None, Path | None]:
        """Compatibility pipeline: configured image file plus JPG preview."""
        avif_path = await self.compress_image(image_path, request_id)
        preview_path = await self.generate_jpg_preview(image_path, request_id)
        return avif_path, preview_path
