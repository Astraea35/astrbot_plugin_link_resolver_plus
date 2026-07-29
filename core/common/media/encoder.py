# core/common/media/encoder.py
import asyncio
import time
from pathlib import Path
from astrbot.api import logger
from .process import monitor_process_percentage


class MediaEncoder:
    """基于 FFmpeg 的异步媒体转码管道 (AV1/AVIF 压缩 & JPG 预览)"""

    def __init__(self, plugin_instance):
        self.plugin = plugin_instance

    async def compress_avif(self, input_path: Path, request_id: str, duration_sec: float | None = None) -> Path | None:
        """异步将图片压缩为 AVIF，严格完全对齐用户的 egFreeUI 预设命令参数"""
        ffmpeg_bin = getattr(self.plugin, "ffmpeg_bin_path", "ffmpeg")
        output_path = input_path.parent / f"{input_path.stem}_av1.avif"

        if output_path.exists():
            age = time.time() - (await asyncio.to_thread(lambda: output_path.stat().st_mtime))
            if age < 7 * 24 * 3600:
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
            str(output_path.resolve()),
        ]
        try:
            orig_size = (await asyncio.to_thread(lambda: input_path.stat().st_size)) / 1024
            logger.info("🗜️ [FFmpeg] 开始 AV1 压缩: %s (%.1fKB)", input_path.name, orig_size)
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            # 耗时打点监控
            await monitor_process_percentage(proc, "🗜️ FFmpeg AV1 压缩中", self.plugin, total_duration_sec=duration_sec)

            if proc.returncode == 0 and output_path.exists():
                new_size = (await asyncio.to_thread(lambda: output_path.stat().st_size)) / 1024
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
        preview_path = image_path.parent / f"{image_path.stem}_preview.jpg"

        # 若原图已经是 JPG 且体积 <= 2MB，直接复用
        if image_path.suffix.lower() in ('.jpg', '.jpeg'):
            try:
                if image_path.stat().st_size <= 2 * 1024 * 1024:
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
            str(preview_path.resolve()),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await monitor_process_percentage(proc, "🖼️ 生成 JPG 预览", self.plugin)
            if proc.returncode == 0 and preview_path.exists():
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