# core/common/media/encoder.py
import asyncio
import hashlib
import tempfile
import time
from pathlib import Path
from astrbot.api import logger
from .metadata import ImageMetadataStore
from .process import monitor_process_percentage


class MediaEncoder:
    """基于 FFmpeg 的异步媒体转码管道 (AV1/AVIF 压缩 & JPG 预览)"""

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

    async def compress_avif(
        self,
        input_path: Path,
        request_id: str,
        duration_sec: float | None = None,
    ) -> Path | None:
        """异步将图片压缩为 AVIF，严格完全对齐用户的 egFreeUI 预设命令参数"""
        ffmpeg_bin = getattr(self.plugin, "ffmpeg_bin_path", "ffmpeg")
        output_path = self._build_output_path(input_path, "_av1.avif")
        metadata_enabled = getattr(self.plugin, "preserve_image_metadata", True)
        metadata = (
            await asyncio.to_thread(self.metadata.ensure, input_path)
            if metadata_enabled
            else None
        )
        processing = {
            "operation": "transcode",
            "format": "AVIF",
            "codec": "libaom-av1",
            "parameters": {
                "cpu_used": 1,
                "crf": 18,
                "still_picture": 1,
                "row_mt": 1,
            },
        }

        if output_path.exists():
            age = time.time() - (await asyncio.to_thread(lambda: output_path.stat().st_mtime))
            if age < 7 * 24 * 3600:
                if metadata_enabled and self.metadata.read(output_path) is None:
                    await asyncio.to_thread(self.metadata.finalize, input_path, output_path, processing)
                logger.info("⚡ [Cache Hit] 命中 7 天内的 AV1 压缩缓存: %s", output_path.name)
                return output_path

        # 🚀 100% 严格锁定你的预设命令:
        # ffmpeg -hide_banner -y -i <输入文件> -map 0:v:0? -c:v:0 libaom-av1 -cpu-used:v:0 1 -crf:v:0 18 -still-picture 1 -row-mt 1 <输出文件>
        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-i", str(input_path.resolve()),
            "-map", "0:v:0?",
            "-c:v:0", "libaom-av1",
            "-cpu-used:v:0", "1",
            "-crf:v:0", "18",
            "-still-picture", "1",
            "-row-mt", "1",
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
            logger.info("🗜️ [FFmpeg] 开始 AV1 压缩: %s (%.1fKB)", input_path.name, orig_size)
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            # 耗时打点监控
            await monitor_process_percentage(proc, "🗜️ FFmpeg AV1 压缩中", self.plugin, total_duration_sec=duration_sec)

            if proc.returncode == 0 and output_path.exists():
                new_size = (await asyncio.to_thread(lambda: output_path.stat().st_size)) / 1024
                if metadata_enabled:
                    await asyncio.to_thread(self.metadata.finalize, input_path, output_path, processing)
                logger.info("✅ [FFmpeg] AV1 完成: %.1fKB → %.1fKB (%s)", orig_size, new_size, output_path.name)
                return output_path
            else:
                await asyncio.to_thread(output_path.unlink, missing_ok=True)
                return None
        except Exception as e:
            logger.error("❌ FFmpeg 压缩异常: %s", str(e))
            await asyncio.to_thread(output_path.unlink, missing_ok=True)
            return None

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
        """组合管道: AVIF 高压文件 + JPG 预览图"""
        avif_path = await self.compress_avif(image_path, request_id)
        preview_path = await self.generate_jpg_preview(image_path, request_id)
        return avif_path, preview_path
