'''AstrBot event handling for media sites outside the native five platforms.'''

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import uuid

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain

from ..common.paths import get_extended_media_path
from .downloader import ExtendedDownloadError, ExtendedMediaDownloader
from .specs import ExtendedPlatform, find_extended_platform


_IMAGE_SUFFIXES = {'.gif', '.jpeg', '.jpg', '.png', '.webp'}
_VIDEO_SUFFIXES = {'.3gp', '.avi', '.m4v', '.mkv', '.mov', '.mp4', '.webm'}


class ExtendedPlatformsMixin:
    '''Shared handler for the platforms backed by generic download engines.'''

    def _get_extended_downloader(self) -> ExtendedMediaDownloader:
        downloader = getattr(self, '_extended_media_downloader', None)
        if downloader is None:
            downloader = ExtendedMediaDownloader(get_extended_media_path())
            self._extended_media_downloader = downloader
        return downloader

    def _extract_extended_urls_from_event(self, event: AstrMessageEvent) -> list[str]:
        payloads: list[str] = []
        for source in (event, getattr(event, 'message_obj', None)):
            if source is None:
                continue
            for attr in ('message_str', 'message', 'raw_message'):
                value = getattr(source, attr, None)
                if isinstance(value, str):
                    payloads.append(value)
                elif isinstance(value, dict):
                    payloads.append(json.dumps(value, ensure_ascii=False))

        links: list[str] = []
        for payload in payloads:
            links.extend(self._extract_urls_from_text(payload))
        return list(dict.fromkeys(links))

    async def _process_extended_event(self, event: AstrMessageEvent) -> None:
        await self._process_extended_urls(
            event,
            self._extract_extended_urls_from_event(event),
            is_from_card=False,
        )

    async def _process_extended_urls(
        self,
        event: AstrMessageEvent,
        urls: list[str],
        *,
        is_from_card: bool,
    ) -> None:
        for url in urls:
            platform = find_extended_platform(url)
            if platform is None or not self._is_extended_platform_enabled(platform):
                continue
            await self._process_extended_url(event, platform, url, is_from_card)
            return

    def _is_extended_platform_enabled(self, platform: ExtendedPlatform) -> bool:
        return platform.label in getattr(self, 'enabled_platforms', set())

    async def _process_extended_url(
        self,
        event: AstrMessageEvent,
        platform: ExtendedPlatform,
        url: str,
        is_from_card: bool,
    ) -> None:
        source_tag = '（来自卡片）' if is_from_card else ''
        request_id = uuid.uuid4().hex[:10]
        notify_id = await self._send_notify(event, f'⏳ 正在解析 {platform.label} 媒体，请稍候...')
        self._register_parse_task(f'extended-{platform.key}', event)
        await self._send_reaction_emoji(event, source_tag)
        self.current_task_info = {
            'title': f'{platform.label} 媒体',
            'user': str(event.get_sender_id() or '未知用户'),
            'current_img': 0,
            'total_img': 0,
            'stage': '正在下载扩展平台媒体',
            'percent': 0,
        }
        try:
            result = await self._get_extended_downloader().download(
                url,
                platform,
                request_id,
                proxy_url=getattr(self, 'extended_proxy_url', ''),
                cookies_file=getattr(self, 'extended_cookies_file', ''),
                timeout_seconds=getattr(self, 'extended_download_timeout', 120),
                gallery_dl_fallback=getattr(self, 'extended_gallery_dl_fallback', True),
            )
            self.current_task_info.update(
                {
                    'title': result.title or f'{platform.label} 媒体',
                    'total_img': len(result.files),
                    'stage': f'已通过 {result.engine} 下载',
                    'percent': 100,
                }
            )
            await self._send_extended_result(event, platform, result.title, result.author, result.files)
        except asyncio.CancelledError:
            raise
        except (ExtendedDownloadError, asyncio.TimeoutError) as exc:
            logger.warning('⚠️ %s 解析失败%s: %s', platform.label, source_tag, exc)
            await self._send_extended_error(event, platform.label, str(exc))
        except Exception as exc:
            logger.exception('❌ %s 扩展解析异常%s', platform.label, source_tag)
            await self._send_extended_error(event, platform.label, str(exc))
        finally:
            await self._recall_notify(event, notify_id)

    async def _send_extended_result(
        self,
        event: AstrMessageEvent,
        platform: ExtendedPlatform,
        title: str,
        author: str,
        files: list[Path],
    ) -> None:
        media_files = self._select_sendable_files(files)
        if getattr(self, 'extended_send_summary', True):
            lines = [f'📎 {platform.label} 解析完成']
            if title:
                lines.append(f'标题：{title}')
            if author:
                lines.append(f'作者：{author}')
            lines.append(f'媒体：{len(media_files)} 个')
            await event.send(MessageChain([Comp.Plain('\n'.join(lines))]))

        if not media_files:
            await event.send(MessageChain([Comp.Plain('未获得可发送的媒体文件。')]))
            return

        components = []
        for path in media_files:
            suffix = path.suffix.lower()
            if suffix in _IMAGE_SUFFIXES:
                components.append(Comp.Image.fromFileSystem(str(path.resolve())))
            elif suffix in _VIDEO_SUFFIXES:
                components.append(await self._video_component_from_path(path))
            else:
                components.append(Comp.File.fromFileSystem(str(path.resolve())))
        await event.send(MessageChain(components))

    def _select_sendable_files(self, files: list[Path]) -> list[Path]:
        max_media = max(1, int(getattr(self, 'extended_max_media', 10)))
        max_video_size = max(0, int(getattr(self, 'max_video_size_mb', 0)))
        selected: list[Path] = []
        for path in files:
            if path.suffix.lower() in _VIDEO_SUFFIXES and max_video_size:
                if path.stat().st_size > max_video_size * 1024 * 1024:
                    logger.warning('⚠️ 扩展平台视频超过大小限制，跳过发送: %s', path.name)
                    continue
            selected.append(path)
            if len(selected) >= max_media:
                break
        return selected

    async def _send_extended_error(
        self,
        event: AstrMessageEvent,
        platform_label: str,
        detail: str,
    ) -> None:
        mode = getattr(self, 'error_notify_mode', '静默')
        if mode == '静默':
            return
        message = f'❌ {platform_label} 解析失败，请检查链接、Cookie 或代理设置。'
        if mode == '报错' and detail:
            message = f'{message}\n{detail[:300]}'
        await event.send(MessageChain([Comp.Plain(message)]))


__all__ = ['ExtendedPlatformsMixin']
