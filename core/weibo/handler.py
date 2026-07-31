"""微博消息处理器。

流程：
1. 从消息中提取微博链接并调用 extractor 解析。
2. 下载微博图片或视频，图片默认按原图优先 URL 下载。
3. 图文微博始终合并转发，视频微博按配置决定是否合并转发。
4. 合并转发中的视频节点会先注册为 callback file URL，兼容异机 NapCat。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Node, Nodes, Plain, Video

from ..common import SizeLimitExceeded, get_weibo_image_path, get_weibo_video_path
from ..common.media import (
    build_image_processing_annotation_text,
    format_image_processing_annotation,
)
from . import (
    WEIBO_DOWNLOAD_HEADERS,
    WeiboParseError,
    WeiboResult,
    extract_weibo_links,
)


class WeiboMixin:
    def _build_weibo_path(self, url: str, is_video: bool, request_id: str) -> Path:
        base_dir = get_weibo_video_path() if is_video else get_weibo_image_path()
        default_suffix = ".mp4" if is_video else ".jpg"
        suffix = (
            default_suffix
            if is_video
            else self._guess_media_suffix(url, default_suffix)
        )
        return base_dir / f"{self._hash_url(url)}_{request_id}{suffix}"

    async def _download_weibo_video(self, url: str, request_id: str) -> Path:
        max_bytes = (
            self.max_video_size_mb * 1024 * 1024 if self.max_video_size_mb > 0 else None
        )
        size_mb = await self._estimate_total_size_mb(
            url, None, headers=WEIBO_DOWNLOAD_HEADERS
        )
        logger.debug(
            "🐦 估算微博视频大小: %s MB",
            f"{size_mb:.2f}" if size_mb is not None else "未知",
        )
        if size_mb is not None and max_bytes and size_mb * 1024 * 1024 > max_bytes:
            raise SizeLimitExceeded("超过大小限制")

        output_path = self._build_weibo_path(url, is_video=True, request_id=request_id)
        await self._download_stream(
            url,
            output_path,
            cookies=None,
            max_bytes=max_bytes,
            headers=WEIBO_DOWNLOAD_HEADERS,
        )
        return output_path

    async def _download_weibo_image(self, url: str, request_id: str) -> Path:
        output_path = self._build_weibo_path(url, is_video=False, request_id=request_id)
        await self._download_stream(
            url,
            output_path,
            cookies=None,
            max_bytes=None,
            headers=WEIBO_DOWNLOAD_HEADERS,
        )
        return output_path

    def _build_weibo_summary(self, result: WeiboResult) -> str:
        lines: list[str] = []
        header = []
        if result.author:
            header.append(f"微博 @{result.author}")
        else:
            header.append("微博")
        if result.created_at:
            header.append(result.created_at)
        lines.append(" | ".join(header))

        if result.text:
            text = result.text.strip()
            if len(text) > 1200:
                text = text[:1197] + "..."
            lines.append(text)

        if result.source_url:
            lines.append(f"链接: {result.source_url}")
        return "\n".join(line for line in lines if line)

    async def _process_weibo(
        self, event: AstrMessageEvent, target_link: str, is_from_card: bool = False
    ) -> None:
        process_start = time.perf_counter()
        timing: dict[str, float] = {}

        self._refresh_config()
        if not self.weibo_enabled:
            return

        source_tag = "(来自卡片)" if is_from_card else ""
        request_id = uuid.uuid4().hex[:8]
        await self._send_reaction_emoji(event, source_tag)

        target_link = (target_link or "").strip()
        if not target_link:
            logger.warning("⚠️ 微博链接为空%s", source_tag)
            return
        logger.info("🐦 微博解析%s: %s", source_tag, target_link)

        parse_start = time.perf_counter()
        retry_count = getattr(self, "retry_count", 3)
        result: WeiboResult | None = None
        last_error = None

        for attempt in range(retry_count + 1):
            try:
                result = await asyncio.wait_for(
                    self.weibo_extractor.parse(target_link),
                    timeout=30.0,
                )
                break
            except asyncio.CancelledError:
                logger.info("♻️ 微博解析任务已中断%s", source_tag)
                return
            except WeiboParseError as exc:
                last_error = str(exc)
                if attempt < retry_count:
                    logger.warning(
                        "⚠️ 微博解析失败%s: %s，重试 %d/%d",
                        source_tag,
                        str(exc),
                        attempt + 1,
                        retry_count,
                    )
                    await asyncio.sleep(1.0)
                else:
                    logger.error(
                        "❌ 微博解析失败%s: %s (已重试%d次)",
                        source_tag,
                        str(exc),
                        retry_count,
                    )
            except Exception as exc:
                last_error = str(exc)
                if attempt < retry_count:
                    logger.warning(
                        "⚠️ 微博解析异常%s: %s，重试 %d/%d",
                        source_tag,
                        str(exc),
                        attempt + 1,
                        retry_count,
                    )
                    await asyncio.sleep(1.0)
                else:
                    logger.error(
                        "❌ 微博解析异常%s: %s (已重试%d次)",
                        source_tag,
                        str(exc),
                        retry_count,
                    )

        timing["parse"] = time.perf_counter() - parse_start
        if result is None:
            logger.error(
                "❌ 微博解析最终失败%s: %s, 解析耗时=%.2fs",
                source_tag,
                last_error,
                timing["parse"],
            )
            return

        logger.debug(
            "🐦 微博解析完成%s: 视频=%s, 图片=%d, 解析耗时=%.2fs",
            source_tag,
            "有" if result.video_url else "无",
            len(result.image_urls),
            timing["parse"],
        )

        if not result.video_url and not result.image_urls:
            logger.warning("⚠️ 微博未找到可下载媒体%s", source_tag)
            return

        summary_text = self._build_weibo_summary(result)
        media_components: list[object] = []
        media_paths: list[Path] = []
        avif_files_to_send: list[Path] = []
        failed_images = 0

        download_start = time.perf_counter()
        if result.video_url:
            try:
                video_path = await self._download_weibo_video(
                    result.video_url, request_id
                )
                media_paths.append(video_path)
                media_components.append(await self._video_component_from_path(video_path))
            except asyncio.CancelledError:
                raise
            except SizeLimitExceeded:
                logger.warning(
                    "⚠️ 微博视频超过大小限制%s (%dMB)",
                    source_tag,
                    self.max_video_size_mb,
                )
                return
            except Exception as exc:
                logger.error("❌ 微博视频下载失败%s: %s", source_tag, str(exc))
                return
        else:
            image_urls = result.image_urls[: self.weibo_max_media]
            # 1. 先进行图片下载
            for i, url in enumerate(image_urls):
                try:
                    image_path = await self._download_weibo_image(url, request_id)
                    media_paths.append(image_path)
                    media_components.append(
                        Image.fromFileSystem(str(image_path.resolve()))
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failed_images += 1
                    logger.warning(
                        "⚠️ 微博图片下载失败%s [%d/%d]: %s",
                        source_tag,
                        i + 1,
                        len(image_urls),
                        str(exc),
                    )

            # 提取已成功下载的图片路径
            image_paths = [p for p in media_paths if p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')]
            source_image_paths = list(image_paths)
            image_processing_metadata = [(False, "未检测", None, None) for _ in image_paths]

            # 2. 后处理：AI 升图
            if getattr(self, "weibo_enable_ai_upscale", True) and image_urls and image_paths:
                logger.info("🎨 微博图片 AI 升图检测开始...")
                new_image_paths = []
                image_processing_metadata = []
                for i, img_path in enumerate(image_paths):
                    upscaled, was_upscaled, image_type, target_model = await self._ai_upscale_platform_image_with_metadata(
                        img_path, request_id,
                        "weibo_enable_ai_upscale", "weibo_low_quality_threshold", "weibo_upscayl_model_name"
                    )
                    image_processing_metadata.append((was_upscaled, image_type, target_model, upscaled if was_upscaled else None))
                    if upscaled != img_path and upscaled.exists():
                        new_image_paths.append(upscaled)
                        for j, mp in enumerate(media_paths):
                            if str(mp) == str(img_path):
                                media_paths[j] = upscaled
                        for j, mc in enumerate(media_components):
                            # 兼容 AstrBot 的 mc.file 属性（优先），其次 mc.path
                            mc_file = str(getattr(mc, 'file', '') or getattr(mc, 'path', ''))
                            if isinstance(mc, Image) and mc_file == str(img_path):
                                media_components[j] = Image.fromFileSystem(str(upscaled.resolve()))
                    else:
                        new_image_paths.append(img_path)
                image_paths = new_image_paths

            # 3. 后处理：AVIF 压缩与预览
            if getattr(self, "enable_global_ffmpeg_compress", True) and image_paths:
                new_image_paths = []
                for i, img_path in enumerate(image_paths):
                    avif_path, preview_path = await self._convert_to_avif_with_preview(img_path, request_id)
                    if avif_path is not None and avif_path != img_path:
                        new_image_paths.append(avif_path)
                        if avif_path.exists() and avif_path.suffix.lower() == ".avif":
                            avif_files_to_send.append(avif_path)
                    else:
                        new_image_paths.append(img_path)
                    for j, mp in enumerate(media_paths):
                        if str(mp) == str(img_path):
                            media_paths[j] = new_image_paths[-1]
                    for j, mc in enumerate(media_components):
                        # 兼容 AstrBot 的 mc.file 属性（优先），其次 mc.path
                        mc_file = str(getattr(mc, 'file', '') or getattr(mc, 'path', ''))
                        if isinstance(mc, Image) and mc_file == str(img_path):
                            if preview_path and preview_path.exists():
                                media_components[j] = Image.fromFileSystem(str(preview_path.resolve()))
                            elif avif_path and avif_path.exists() and avif_path.suffix.lower() == '.avif':
                                media_components[j] = Image.fromFileSystem(str(avif_path.resolve()))
                image_paths = new_image_paths
            elif image_paths:
                logger.info("ℹ️ 微博全局 AVIF 压缩未启用，跳过 AVIF 文件生成与发送")

            annotation_text = build_image_processing_annotation_text([
                format_image_processing_annotation(i + 1, source_path, processed_path, *image_processing_metadata[i])
                for i, (source_path, processed_path) in enumerate(zip(source_image_paths, image_paths))
            ])
            if annotation_text:
                await event.send(MessageChain([Plain(annotation_text)]))

        timing["download"] = time.perf_counter() - download_start
        if not media_components:
            logger.warning(
                "⚠️ 微博媒体下载全部失败%s, 下载耗时=%.2fs",
                source_tag,
                timing["download"],
            )
            return

        is_image_post = bool(result.image_urls and not result.video_url)
        enable_merge_send = is_image_post or self.weibo_merge_send

        try:
            send_start = time.perf_counter()
            if enable_merge_send:
                nodes = Nodes([])
                sender_uin = self._get_merge_sender_uin(event)
                if summary_text:
                    nodes.nodes.append(
                        Node(uin=sender_uin, content=[Plain(summary_text)])
                    )
                for component in media_components:
                    merge_component = await self._prepare_component_for_merge_send(
                        component
                    )
                    nodes.nodes.append(Node(uin=sender_uin, content=[merge_component]))
                await event.send(MessageChain([nodes]))
            else:
                direct_component = await self._prepare_component_for_merge_send(
                    media_components[0]
                )
                await event.send(MessageChain([direct_component]))

            # 合并转发只包含可直接预览的图片；高质量 AVIF 通过文件接口单独发送。
            if avif_files_to_send:
                logger.info("📁 微博准备独立发送 %d 个 AVIF 文件", len(avif_files_to_send))
            for avif_file in avif_files_to_send:
                try:
                    if not await self._send_file_via_api(event, avif_file):
                        logger.warning("⚠️ 微博独立发送 AVIF 文件失败: %s", avif_file.name)
                except Exception as exc:
                    logger.warning("⚠️ 微博独立发送 AVIF 文件失败 (%s): %s", avif_file.name, str(exc))

            timing["send"] = time.perf_counter() - send_start

            total_elapsed = time.perf_counter() - process_start
            logger.info(
                "🐦 微博处理完成%s: 标题=%s, 媒体=%d, 失败=%d | 耗时: 解析=%.2fs, 下载=%.2fs, 发送=%.2fs, 总计=%.2fs",
                source_tag,
                (result.title or "未知标题")[:20],
                len(media_components),
                failed_images,
                timing.get("parse", 0),
                timing.get("download", 0),
                timing.get("send", 0),
                total_elapsed,
            )
        finally:
            if media_paths:
                await self.cleanup_files(media_paths, [])

    async def handle_weibo(self, event: AstrMessageEvent) -> None:
        if not self.weibo_enabled:
            return
        if self._is_self_message(event):
            return
        if await self._is_bot_muted(event):
            return
        event.should_call_llm(True)
        links = extract_weibo_links(event.message_str)
        logger.info("🐦 微博匹配链接: %s", links)
        if not links:
            return
        try:
            await self._process_weibo(event, links[0], is_from_card=False)
        except asyncio.CancelledError:
            logger.info("♻️ 微博解析任务已中断")
            return
