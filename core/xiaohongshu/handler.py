# region 导入
import asyncio
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File, Image, Node, Nodes, Plain, Video

from ..common import (
    SizeLimitExceeded,
    get_xhs_card_path,
    get_xhs_image_path,
    get_xhs_video_path,
)
from ..common.media import (
    build_image_processing_annotation_text,
    format_image_processing_annotation,
)
from . import (
    XHS_HEADERS,
    XiaohongshuParseError,
    XiaohongshuResult,
    XiaohongshuRetryableError,
    extract_xhs_links,
)
from .extractor import _XHS_DOWNLOAD_UA

# endregion

# region 解析策略与中文模型映射常量
XHS_PARSE_TIMEOUT_SEC = 30.0
XHS_PARSE_RETRY_BASE_DELAY_SEC = 1.0
XHS_PARSE_RETRY_MAX_DELAY_SEC = 8.0

# endregion


class XiaohongshuMixin:
    # region 路径与候选构建
    def _build_xhs_path(self, url: str, is_video: bool, request_id: str) -> Path:
        suffix = ".mp4" if is_video else self._guess_media_suffix(url, ".jpg")
        base_dir = get_xhs_video_path() if is_video else get_xhs_image_path()
        return base_dir / f"{self._hash_url(url)}_{request_id}{suffix}"

    def _build_xhs_card_path(self, source_url: str, request_id: str) -> Path:
        return (
            get_xhs_card_path() / f"{self._hash_url(source_url)}_{request_id}_card.png"
        )

    @staticmethod
    def _force_https(url: str) -> str:
        try:
            parsed = urlparse(url)
        except Exception:
            return url
        if parsed.scheme in ("", "http"):
            return parsed._replace(scheme="https").geturl()
        return url

    # endregion

    # region 下载与渲染
    @staticmethod
    def _xhs_download_headers(referer: str | None) -> dict[str, str]:
        headers = dict(XHS_HEADERS)
        if referer:
            headers["Referer"] = referer
        headers["Origin"] = "https://www.xiaohongshu.com"
        headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
        headers["Accept-Language"] = "zh-CN,zh;q=0.9"
        return headers

    @staticmethod
    def _is_retryable_xhs_exception(exc: Exception) -> bool:
        if isinstance(exc, (asyncio.TimeoutError, XiaohongshuRetryableError)):
            return True
        text = str(exc).lower()
        retryable_patterns = (
            "timeout",
            "timed out",
            "connection",
            "reset",
            "refused",
            "temporary",
            "unavailable",
            "503",
            "502",
            "504",
            "429",
            "network",
        )
        return any(p in text for p in retryable_patterns)

    async def _download_xhs_video(
        self, url: str, request_id: str, referer: str | None = None
    ) -> Path:
        max_bytes = (
            self.max_video_size_mb * 1024 * 1024 if self.max_video_size_mb > 0 else None
        )
        size_mb = await self._estimate_total_size_mb(
            url, None, headers=self._xhs_download_headers(referer)
        )
        logger.debug(
            "📊 估算小红书视频大小: %s MB",
            f"{size_mb:.2f}" if size_mb is not None else "未知",
        )
        if size_mb is not None and max_bytes and size_mb * 1024 * 1024 > max_bytes:
            raise SizeLimitExceeded("超过大小限制")
        output_path = self._build_xhs_path(url, is_video=True, request_id=request_id)
        await self._download_stream(
            url,
            output_path,
            cookies=None,
            max_bytes=max_bytes,
            headers=self._xhs_download_headers(referer),
            retries=3,
        )
        return output_path

    async def _download_xhs_image(
        self,
        url: str,
        request_id: str,
        file_id: str | None = None,
        referer: str | None = None,
    ) -> Path:
        start_time = time.perf_counter()
        output_path = self._build_xhs_path(url, is_video=False, request_id=request_id)
        token = self._extract_image_token(url)
        logger.debug(
            "XHS 图片下载开始: url=%s, file_id=%s, token=%s", url[:80], file_id, token
        )

        if getattr(self, "xhs_download_original", True) and token:
            cdn_domains = [
                "sns-img-bd.xhscdn.com",
                "sns-img-qc.xhscdn.com",
                "sns-img-hw.xhscdn.com",
            ]
            cdn_candidates = [
                {
                    "url": f"https://{domain}/{token}",
                    "desc": f"CDN-{domain.split('-')[2].split('.')[0]}-auto",
                    "format": None,
                }
                for domain in cdn_domains
            ]
            ci_candidates = [
                {
                    "url": f"https://ci.xiaohongshu.com/{token}?imageView2/format/png",
                    "desc": "CI-PNG-原图",
                    "format": "png",
                },
            ]

            original_candidates = ci_candidates + cdn_candidates if getattr(self, "xhs_prefer_ci_png", False) else cdn_candidates + ci_candidates
            retry_count = max(0, int(getattr(self, "retry_count", 3)))

            for cand in original_candidates:
                cand_url = cand["url"]
                desc = cand["desc"]
                format_name = cand["format"]

                for attempt in range(retry_count + 1):
                    attempt_start = time.perf_counter()
                    try:
                        timeout = aiohttp.ClientTimeout(total=600, connect=60)
                        headers = {
                            "User-Agent": _XHS_DOWNLOAD_UA,
                            "Referer": "https://www.xiaohongshu.com/",
                        }

                        async with aiohttp.ClientSession(
                            headers=headers, timeout=timeout
                        ) as session:
                            async with session.get(cand_url) as resp:
                                attempt_elapsed = time.perf_counter() - attempt_start

                                if resp.status == 200:
                                    temp_output = output_path.with_suffix(".tmp")
                                    temp_path = temp_output.with_suffix(temp_output.suffix + ".part")
                                    content_len = 0
                                    f = None
                                    try:
                                        def _open_temp():
                                            temp_path.parent.mkdir(parents=True, exist_ok=True)
                                            return open(temp_path, "wb")

                                        f = await asyncio.to_thread(_open_temp)
                                        try:
                                            async for chunk in resp.content.iter_chunked(256 * 1024):
                                                if not chunk:
                                                    continue
                                                content_len += len(chunk)
                                                await asyncio.to_thread(f.write, chunk)
                                        finally:
                                            if f is not None:
                                                await asyncio.to_thread(f.close)

                                        if content_len >= 10 * 1024 and temp_path.exists():
                                            if format_name:
                                                actual_suffix = f".{format_name}"
                                            else:
                                                def _read_head():
                                                    with open(temp_path, "rb") as rf:
                                                        return rf.read(32)

                                                head = await asyncio.to_thread(_read_head)
                                                actual_suffix = self._detect_image_suffix(
                                                    head, resp.headers.get("Content-Type")
                                                )

                                            final_output = output_path.with_suffix(actual_suffix)
                                            final_part = final_output.with_suffix(final_output.suffix + ".part")

                                            def _move():
                                                if final_part.exists():
                                                    final_part.unlink()
                                                temp_path.replace(final_part)
                                                final_part.replace(final_output)

                                            await asyncio.to_thread(_move)
                                            total_elapsed = time.perf_counter() - start_time
                                            logger.debug(
                                                "XHS 原图下载成功 (%s): size=%.1fMB, 请求耗时=%.2fs, 总耗时=%.2fs",
                                                desc, content_len / 1024 / 1024, attempt_elapsed, total_elapsed
                                            )
                                            return final_output
                                    finally:
                                        if temp_path.exists() and content_len < 10 * 1024:
                                            try:
                                                temp_path.unlink()
                                            except Exception:
                                                pass
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        attempt_elapsed = time.perf_counter() - attempt_start
                        logger.debug("XHS 原图下载异常 (%s): %s, 耗时=%.2fs", desc, str(e)[:50], attempt_elapsed)

                    if attempt < retry_count:
                        await asyncio.sleep(0.5 * (2**attempt))

        # CDN 兜底策略
        base_headers = {"User-Agent": _XHS_DOWNLOAD_UA}
        candidates = [{"url": (url.replace("http://", "https://", 1) if url.startswith("http://") else url), "desc": "Raw"}]

        effective_id = file_id or token
        if effective_id:
            domains = ["sns-img-bd.xhscdn.com", "sns-img-qc.xhscdn.com", "sns-img-hw.xhscdn.com", "sns-webpic-qc.xhscdn.com"]
            for domain in domains:
                for path_prefix in ["", "spectrum/"]:
                    candidates.append({"url": f"https://{domain}/{path_prefix}{effective_id}", "desc": f"CDN-{domain.split('.')[0]}"})

        errors = []
        retry_count = max(0, int(getattr(self, "retry_count", 3)))
        for cand in candidates:
            cand_url = cand["url"]
            desc = cand["desc"]
            header_variants = [{**base_headers, "Referer": "https://www.xiaohongshu.com/"}, base_headers.copy()]

            for hv in header_variants:
                for attempt in range(retry_count + 1):
                    try:
                        timeout = aiohttp.ClientTimeout(total=300, connect=30)
                        async with aiohttp.ClientSession(headers=hv, timeout=timeout) as session:
                            async with session.get(cand_url) as resp:
                                if resp.status == 200:
                                    temp_path = output_path.with_suffix(output_path.suffix + ".part")
                                    content_len = 0
                                    f = None
                                    try:
                                        def _open_part():
                                            temp_path.parent.mkdir(parents=True, exist_ok=True)
                                            return open(temp_path, "wb")

                                        f = await asyncio.to_thread(_open_part)
                                        try:
                                            async for chunk in resp.content.iter_chunked(256 * 1024):
                                                if not chunk:
                                                    continue
                                                content_len += len(chunk)
                                                await asyncio.to_thread(f.write, chunk)
                                        finally:
                                            if f is not None:
                                                await asyncio.to_thread(f.close)

                                        if content_len >= 1024 and temp_path.exists():
                                            await asyncio.to_thread(temp_path.replace, output_path)
                                            return output_path
                                    except Exception:
                                        if temp_path.exists():
                                            await asyncio.to_thread(temp_path.unlink, missing_ok=True)
                                        raise
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        errors.append(f"{desc}: {str(e)[:20]}")

                    if attempt < retry_count:
                        await asyncio.sleep(0.5 * (2**attempt))

        total_elapsed = time.perf_counter() - start_time
        error_summary = " | ".join(errors[:5])
        logger.error("❌ XHS 图片下载全线失败: 总耗时=%.2fs, 错误=%s", total_elapsed, error_summary)
        raise RuntimeError(f"图片下载失败: {error_summary}")

    @staticmethod
    def _extract_image_token(url: str) -> str | None:
        if not url:
            return None
        try:
            parts = url.split("/")
            if len(parts) >= 6:
                token = "/".join(parts[5:]).split("!")[0].split("?")[0]
                if token and len(token) > 10:
                    return token
            last_part = url.split("/")[-1].split("!")[0].split("?")[0]
            if last_part and len(last_part) > 10:
                return last_part
        except Exception:
            pass
        return None

    @staticmethod
    def _detect_image_suffix(content: bytes, content_type: str | None) -> str:
        if content[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if content[:3] == b"\xff\xd8\xff":
            return ".jpeg"
        if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            return ".webp"
        if content[:4] == b"GIF8":
            return ".gif"
        if content[4:12] in (b"ftypheic", b"ftypmif1", b"ftypheix", b"ftyphevc"):
            return ".heic"
        if content[4:12] in (b"ftypavif", b"ftypavis"):
            return ".avif"

        if content_type:
            ct = content_type.lower()
            if "png" in ct:
                return ".png"
            if "jpeg" in ct or "jpg" in ct:
                return ".jpeg"
            if "webp" in ct:
                return ".webp"
            if "heic" in ct or "heif" in ct:
                return ".heic"
            if "gif" in ct:
                return ".gif"

        return ".jpeg"

    @staticmethod
    def _clean_xhs_summary_text(text: str) -> str:
        return re.sub(r"#([^#\n]+?)\[话题\]#", r"#\1#", text)

    @staticmethod
    def _clean_xhs_display_link(link: str) -> str:
        try:
            parsed = urlparse(link)
        except Exception:
            return link
        host = parsed.netloc.lower()
        if "xiaohongshu.com" not in host:
            return link
        return parsed._replace(query="", fragment="").geturl()

    def _build_xhs_summary(
        self,
        result: XiaohongshuResult,
        *,
        image_count: int,
        is_video: bool,
        display_link: str | None = None,
    ) -> str:
        author = result.author or "未知作者"
        title = result.title or "未知标题"
        lines = ["📕 小红书", f"作者：{author}", f"标题：{title}"]
        if result.text:
            lines.append(f"正文：{XiaohongshuMixin._clean_xhs_summary_text(result.text)}")
        lines.append(f"媒体：{'视频' if is_video else f'图片 {image_count} 张'}")
        summary_link = display_link or result.source_url
        if summary_link:
            lines.append(f"链接：{XiaohongshuMixin._clean_xhs_display_link(summary_link)}")
        return "\n".join(lines)

    async def _render_xhs_card(
        self,
        result: XiaohongshuResult,
        image_paths: list[Path],
        cover_path: Path | None,
        is_video: bool,
        request_id: str,
    ) -> Path | None:
        try:
            card_path = self._build_xhs_card_path(result.source_url, request_id)
            title = result.title or "小红书图集"
            author = result.author or "小红书用户"
            text = result.text or ""

            if not getattr(self, "xhs_renderer", None):
                from ..common.card_renderer import find_default_font
                from .render import XiaohongshuCardRenderer
                self.xhs_renderer = XiaohongshuCardRenderer(find_default_font())

            image = await asyncio.to_thread(
                self.xhs_renderer.render,
                title=title,
                author=author,
                text=text,
                image_paths=image_paths,
                cover_path=cover_path,
                is_video=is_video,
            )
            await asyncio.to_thread(image.save, card_path, format="PNG")
            logger.info("🖼️ 小红书渲染卡片生成成功: %s", card_path.name)
            return card_path
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("⚠️ 小红书卡片渲染失败: %s", str(exc))
            return None

    # region 后处理与解耦逻辑

    async def _convert_heic_to_png(self, image_path: Path) -> Path:
        """自动检测 HEIC 图像并调用 FFmpeg 转码为标准 PNG"""
        if image_path.suffix.lower() in ('.heic', '.heif'):
            ffmpeg_bin = getattr(self, "ffmpeg_bin_path", "ffmpeg")
            png_path = image_path.with_suffix('.png')
            metadata_enabled = getattr(self, "preserve_image_metadata", True)
            metadata = (
                await asyncio.to_thread(self.image_metadata_store.ensure, image_path)
                if metadata_enabled
                else None
            )
            processing = {
                "operation": "transcode",
                "format": "PNG",
                "codec": "png",
                "source_format": image_path.suffix.lower().lstrip("."),
            }
            cmd = [
                ffmpeg_bin, "-hide_banner", "-y",
                "-i", str(image_path.resolve()),
                *(
                    self.image_metadata_store.ffmpeg_args(metadata, processing)
                    if metadata_enabled and metadata is not None
                    else []
                ),
                str(png_path.resolve())
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                await proc.wait()
                if proc.returncode == 0 and png_path.exists():
                    if metadata_enabled:
                        await asyncio.to_thread(
                            self.image_metadata_store.finalize, image_path, png_path, processing
                        )
                    logger.info("🔄 [HEIC 转码] 自动将 HEIC 原图转化为 PNG: %s", png_path.name)
                    await asyncio.to_thread(image_path.unlink, missing_ok=True)
                    return png_path
            except Exception as e:
                logger.warning("⚠️ HEIC 自动转码 PNG 异常: %s", str(e))
        return image_path

    async def _post_process_xhs_image(
        self, image_path, request_id, index=0, total=0, metadata_context=None
    ):
        task_info = getattr(self, "current_task_info", None)
        if task_info is not None:
            task_info["current_img"] = index

        prepare_metadata = getattr(self, "_prepare_image_metadata", None)
        if prepare_metadata:
            await prepare_metadata(image_path, metadata_context)
        current_path = await self._convert_heic_to_png(image_path)

        preview_path = None
        was_upscaled = False
        upscaled_path = None
        img_type = "未检测"
        target_model = None

        if getattr(self, "xhs_enable_ai_upscale", True):
            threshold = getattr(self, "xhs_low_quality_threshold", 1080)
            model_setting = getattr(
                self, "xhs_upscayl_model_name", "自动 (CV特征识别)"
            )
            need_upscale, img_type, target_model = (
                await self.upscaler.check_is_low_quality(
                    current_path,
                    threshold=threshold,
                    model_setting=model_setting,
                )
            )
            if need_upscale:
                if task_info is not None:
                    task_info["stage"] = "🎨 AI 升图中"
                    task_info["percent"] = "0.0%"
                async with self.heavy_task_lock:
                    upscaled_path = await self.upscaler.upscale_image(
                        current_path,
                        request_id,
                        override_model=target_model,
                    )
                if upscaled_path != current_path:
                    propagate_metadata = getattr(self, "_propagate_image_metadata", None)
                    if propagate_metadata:
                        await propagate_metadata(
                            current_path,
                            upscaled_path,
                            {
                                "operation": "ai_upscale",
                                "model": target_model,
                                "scale": getattr(self, "upscayl_scale", None),
                                "double_pass": getattr(self, "upscayl_double_pass", None),
                                "taa": getattr(self, "upscayl_enable_taa", None),
                            },
                        )
                    current_path = upscaled_path
                    was_upscaled = True

        if getattr(self, "enable_global_ffmpeg_compress", True):
            if task_info is not None:
                task_info["stage"] = "🗜️ FFmpeg AV1 压缩中"
                task_info["percent"] = "0.0%"
            avif_path, jpg_preview = await self._convert_to_avif_with_preview(current_path, request_id)
            if avif_path is not None and avif_path != current_path:
                current_path = avif_path
            if jpg_preview is not None:
                preview_path = jpg_preview
        else:
            preview_path = current_path

        return current_path, preview_path, was_upscaled, img_type, target_model, upscaled_path

    # endregion

    # region 小红书处理
    async def _process_xhs(
        self, event: AstrMessageEvent, target_link: str, is_from_card: bool = False
    ):
        process_start = time.perf_counter()
        timing = {}

        self._refresh_config()
        if not self.xhs_enabled:
            return
        source_tag = "(来自卡片)" if is_from_card else ""
        request_id = uuid.uuid4().hex[:8]

        self.current_task_info = {
            "title": "", "user": "", "current_img": 0, "total_img": 0,
            "stage": "Downloading", "percent": "0.0%", "start_time": time.time()
        }

        await self._send_reaction_emoji(event, source_tag)
        target_link = (target_link or "").strip()

        if not target_link:
            logger.warning("⚠️ 小红书链接为空%s", source_tag)
            return
        logger.info("📕 小红书解析%s: %s", source_tag, target_link)

        parse_start = time.perf_counter()
        retry_count = max(0, int(getattr(self, "retry_count", 3)))
        result: XiaohongshuResult | None = None
        last_error: Exception | None = None

        for attempt in range(retry_count + 1):
            try:
                result = await asyncio.wait_for(
                    self.xhs_extractor.parse(target_link),
                    timeout=XHS_PARSE_TIMEOUT_SEC,
                )
                break
            except asyncio.CancelledError:
                logger.info("♻️ 小红书解析任务已中断%s", source_tag)
                return
            except XiaohongshuParseError as exc:
                last_error = exc
                if attempt < retry_count and self._is_retryable_xhs_exception(exc):
                    wait_time = min(
                        XHS_PARSE_RETRY_MAX_DELAY_SEC,
                        XHS_PARSE_RETRY_BASE_DELAY_SEC * (2**attempt),
                    )
                    logger.warning("⚠️ 小红书解析失败%s: %s，%.1fs后重试 (%d/%d)", source_tag, str(exc), wait_time, attempt + 1, retry_count)
                    await asyncio.sleep(wait_time)
                    continue
                logger.error("❌ 小红书解析失败%s: %s", source_tag, str(exc))
                return
            except Exception as exc:
                last_error = exc
                if attempt < retry_count and self._is_retryable_xhs_exception(exc):
                    wait_time = min(
                        XHS_PARSE_RETRY_MAX_DELAY_SEC,
                        XHS_PARSE_RETRY_BASE_DELAY_SEC * (2**attempt),
                    )
                    logger.warning("⚠️ 小红书解析异常%s: %s，%.1fs后重试 (%d/%d)", source_tag, str(exc), wait_time, attempt + 1, retry_count)
                    await asyncio.sleep(wait_time)
                    continue
                logger.error("❌ 小红书解析异常%s: %s", source_tag, str(exc))
                return

        if result is None:
            logger.error("❌ 小红书解析最终失败%s: %s, link=%s", source_tag, str(last_error) if last_error else "unknown", target_link)
            return

        timing["parse"] = time.perf_counter() - parse_start

        logger.debug(
            "📕 小红书解析完成%s: 视频=%s, 图片=%s, 解析耗时=%.2fs",
            source_tag,
            "有" if result.video_url else "无",
            len(result.image_urls),
            timing["parse"],
        )

        title = result.title or "未知标题"
        if not result.video_url and not result.image_urls:
            logger.warning("⚠️ 小红书未找到可下载的媒体%s: %s", source_tag, target_link)
            return

        media_components: list[object] = []
        media_paths: list[Path] = []
        image_paths: list[Path] = []
        cover_path: Path | None = None
        avif_files_to_send: list[Path] = []
        failed_images = 0

        download_start = time.perf_counter()

        if result.video_url:
            try:
                video_path = await self._download_xhs_video(
                    result.video_url, request_id, referer=result.source_url
                )
                media_paths.append(video_path)
                media_components.append(await self._video_component_from_path(video_path))
                cover_url = result.cover_url or (result.image_urls[0] if result.image_urls else None)
                if cover_url:
                    try:
                        cover_path = await self._download_xhs_image(
                            cover_url, request_id, referer=result.source_url
                        )
                        media_paths.append(cover_path)
                    except Exception as exc:
                        logger.warning("⚠️ 小红书封面下载失败%s: %s", source_tag, str(exc))
            except SizeLimitExceeded:
                logger.warning("⚠️ 小红书视频大小超过限制%s (%dMB)", source_tag, self.max_video_size_mb)
                return
            except Exception as exc:
                logger.error("❌ 小红书视频下载失败%s: %s", source_tag, str(exc))
                return
        elif result.image_urls:
            image_urls = result.image_urls[: self.xhs_max_media]
            file_ids = result.file_ids[: self.xhs_max_media] if result.file_ids else []
            if getattr(self, "xhs_concurrent_download", False):
                async def _download_one(i: int, url: str):
                    file_id = file_ids[i] if i < len(file_ids) else None
                    try:
                        path = await self._download_xhs_image(
                            url, request_id, file_id=file_id, referer=result.source_url
                        )
                        return (i, path, None)
                    except Exception as exc:
                        return (i, None, exc)

                dl_results = await asyncio.gather(*[_download_one(i, url) for i, url in enumerate(image_urls)])
                for i, path, exc in dl_results:
                    if path is not None:
                        image_paths.append(path)
                        media_paths.append(path)
                        media_components.append(Image.fromFileSystem(str(path.resolve())))
                    else:
                        failed_images += 1
                        logger.warning("⚠️ 小红书图片下载失败%s [%d/%d]: %s", source_tag, i + 1, len(image_urls), str(exc))
            else:
                for i, url in enumerate(image_urls):
                    try:
                        file_id = file_ids[i] if i < len(file_ids) else None
                        image_path = await self._download_xhs_image(
                            url, request_id, file_id=file_id, referer=result.source_url
                        )
                        image_paths.append(image_path)
                        media_paths.append(image_path)
                        media_components.append(Image.fromFileSystem(str(image_path.resolve())))
                    except Exception as exc:
                        failed_images += 1
                        logger.warning("⚠️ 小红书图片下载失败%s [%d/%d]: %s", source_tag, i + 1, len(image_urls), str(exc))

            # 🚀【新增】：下载并挂载 Live Photo 实况图 MP4 视频文件
            live_photo_urls = getattr(result, "live_photo_urls", [])
            if live_photo_urls:
                for i, live_url in enumerate(live_photo_urls):
                    if live_url:
                        try:
                            live_video_path = await self._download_xhs_video(
                                live_url, f"{request_id}_live_{i}", referer=result.source_url
                            )
                            media_paths.append(live_video_path)
                            media_components.append(
                                await self._video_component_from_path(live_video_path)
                            )
                            logger.info("🎬 成功下载第 %d 张图的 Live Photo 视频: %s", i + 1, live_video_path.name)
                        except Exception as exc:
                            logger.warning("⚠️ 第 %d 张图的 Live Photo 视频下载失败: %s", i + 1, str(exc))

        timing["download"] = time.perf_counter() - download_start

        if self.current_task_info is not None:
            self.current_task_info["title"] = result.title or "Unknown"
            self.current_task_info["user"] = result.author or "Unknown"
            self.current_task_info["total_img"] = len(image_paths)
            self.current_task_info["current_img"] = 0

        # 后处理（AI 升图与 AVIF 转码）
        if image_paths and not result.video_url:
            self.current_task_info["stage"] = "Post-processing"
            self.current_task_info["percent"] = "0.0%"
            upscale_annotations = []
            processed_results = []

            for i, img_path in enumerate(image_paths):
                proc_path, preview_path, was_upscaled, img_type, target_model, upscaled_path = await self._post_process_xhs_image(
                    img_path,
                    request_id,
                    index=i + 1,
                    total=len(image_paths),
                    metadata_context={
                        "platform": "xiaohongshu",
                        "url": result.source_url,
                        "author": result.author,
                        "title": result.title,
                        "image_index": i + 1,
                        "image_count": len(image_paths),
                        "original_image_url": result.image_urls[i] if i < len(result.image_urls) else None,
                        "live_photo_video_url": (
                            result.live_photo_urls[i]
                            if i < len(getattr(result, "live_photo_urls", []))
                            else None
                        ),
                        "live_photo": bool(
                            i < len(getattr(result, "live_photo_urls", []))
                            and result.live_photo_urls[i]
                        ),
                    },
                )
                upscale_annotations.append(format_image_processing_annotation(
                    i + 1, img_path, proc_path, was_upscaled, img_type, target_model, upscaled_path
                ))

                display_path = preview_path if (preview_path and preview_path.exists()) else proc_path
                processed_results.append((display_path, proc_path, was_upscaled))

            # 重构图片组件列表（保留下载的 Live Photo Video 组件）
            existing_videos = [c for c in media_components if isinstance(c, Video)]
            media_components.clear()

            for display_path, proc_path, was_upscaled in processed_results:
                media_components.append(Image.fromFileSystem(str(display_path.resolve())))
                if proc_path.suffix.lower() == '.avif':
                    avif_files_to_send.append(proc_path)

            # 重新追加 Live Photo 视频组件
            media_components.extend(existing_videos)

            image_paths = [r[0] for r in processed_results]

            if upscale_annotations:
                annotation_text = build_image_processing_annotation_text(upscale_annotations)
                try:
                    if annotation_text:
                        yield event.chain_result([Plain(annotation_text)])
                except Exception:
                    pass

        if not media_components:
            logger.debug("XHS 无媒体下载成功%s: url=%s", source_tag, result.source_url)
            return

        summary_enabled = bool(image_paths) or bool(result.video_url and self.xhs_merge_send)
        render_card = getattr(self, "xhs_render_card", False)
        is_video_post = bool(result.video_url and not image_paths)
        summary_text = None
        if summary_enabled and not render_card:
            summary_text = self._build_xhs_summary(
                result,
                image_count=len(image_paths),
                is_video=is_video_post,
                display_link=target_link,
            )

        render_start = time.perf_counter()
        card_path = None
        if summary_enabled and render_card:
            card_path = await self._render_xhs_card(
                result,
                image_paths=image_paths,
                cover_path=cover_path,
                is_video=is_video_post,
                request_id=request_id,
            )
        if card_path:
            media_paths.append(card_path)
            media_components.insert(0, Image.fromFileSystem(str(card_path.resolve())))
        timing["render"] = time.perf_counter() - render_start

        send_start = time.perf_counter()

        def _convert_to_file_if_needed(component):
            comp_path = getattr(component, "file", None) or getattr(component, "path", None)
            if isinstance(component, Image) and comp_path:
                try:
                    qq_image_size_limit_mb = getattr(self, "xhs_qq_image_size_limit_mb", 30)
                    if qq_image_size_limit_mb <= 0:
                        return component
                    file_size = Path(comp_path).stat().st_size
                    if file_size > qq_image_size_limit_mb * 1024 * 1024:
                        file_name = Path(comp_path).name
                        logger.info(
                            "XHS 图片 %.1fMB 超过 %dMB QQ限制，转为文件上传: %s",
                            file_size / 1024 / 1024,
                            qq_image_size_limit_mb,
                            file_name,
                        )
                        return File(file=str(comp_path), name=file_name)
                except Exception:
                    pass
            return component

        media_components = [_convert_to_file_if_needed(c) for c in media_components]

        total_size_bytes = await asyncio.to_thread(
            lambda: sum(p.stat().st_size for p in media_paths if p.exists())
        )
        total_size_mb = total_size_bytes / (1024 * 1024)

        threshold = getattr(self, "xhs_auto_unmerge_threshold_mb", 20)
        force_unmerge = False
        if threshold > 0 and total_size_mb > threshold:
            logger.info("XHS 媒体总大小 (%.2fMB) 超过阈值 (%dMB)，强制逐条发送", total_size_mb, threshold)
            force_unmerge = True

        is_image_post = bool(image_paths)

        if is_image_post:
            should_merge = not force_unmerge
        else:
            should_merge = self.xhs_merge_send

        # 1. 合并转发/逐条发送图片及 Live Photo MP4 视频
        if should_merge:
            nodes = Nodes([])
            sender_uin = self._get_merge_sender_uin(event)
            if summary_text:
                nodes.nodes.append(Node(uin=sender_uin, content=[Plain(summary_text)]))
            for component in media_components:
                merge_component = await self._prepare_component_for_merge_send(component)
                nodes.nodes.append(Node(uin=sender_uin, content=[merge_component]))
            yield event.chain_result([nodes])
        else:
            if is_image_post:
                if summary_text:
                    yield event.chain_result([Plain(summary_text)])
                for i, component in enumerate(media_components):
                    yield event.chain_result([component])
                    if i < len(media_components) - 1:
                        await asyncio.sleep(2.0)
            else:
                for component in media_components:
                    if isinstance(component, Video):
                        video_component = await self._prepare_component_for_merge_send(
                            component
                        )
                        yield event.chain_result([video_component])
                        break

        # 2. 独立后置发送高压 AVIF 原图文件
        if avif_files_to_send:
            for avif_file in avif_files_to_send:
                try:
                    await self._send_file_via_api(event, avif_file)
                except Exception as e:
                    logger.warning("⚠️ 独立发送 AVIF 文件失败 (%s): %s", avif_file.name, str(e))

        timing["send"] = time.perf_counter() - send_start

        total_elapsed = time.perf_counter() - process_start
        logger.info(
            "📕 XHS 处理完成%s: 标题=%s, 媒体=%d, 失败=%d | 耗时: 解析=%.2fs, 下载=%.2fs, 渲染=%.2fs, 发送=%.2fs, 总计=%.2fs",
            source_tag,
            title[:20],
            len(media_components),
            failed_images,
            timing.get("parse", 0),
            timing.get("download", 0),
            timing.get("render", 0),
            timing.get("send", 0),
            total_elapsed,
        )

        if media_paths:
            await self.cleanup_files(media_paths, [])

    # endregion

    # region 事件处理器
    async def handle_xhs(self, event: AstrMessageEvent):
        if not self.xhs_enabled:
            return
        if self._is_self_message(event):
            return
        if await self._is_bot_muted(event):
            return
        event.should_call_llm(True)
        links = extract_xhs_links(event.message_str)
        logger.info("📕 小红书匹配链接: %s", links)
        if not links:
            return
        try:
            async for result in self._process_xhs(event, links[0], is_from_card=False):
                yield result
        except asyncio.CancelledError:
            logger.info("♻️ 小红书解析任务已中断")
            return

    # endregion


# endregion
