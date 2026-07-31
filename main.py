# main.py
import asyncio
import re

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# 从各平台的子模块准确导入 Mixin 与正则
from .core.bilibili import BILI_MESSAGE_PATTERN
from .core.bilibili.handler import BilibiliMixin
from .core.common.config_mixin import ConfigMixin
from .core.common.base_mixin import BaseUtilsMixin
from .core.common.commands_mixin import CommandsMixin
from .core.common.font_manager import install_managed_fonts
from .core.common.media import UpscaylUpscaler, MediaEncoder
from .core.common.image_tool_mixin import ImageToolMixin
from .core.douyin import DOUYIN_MESSAGE_PATTERN, DouyinExtractor
from .core.douyin.handler import DouyinMixin
from .core.twitter import TWITTER_MESSAGE_PATTERN, TwitterExtractor
from .core.twitter.handler import TwitterMixin
from .core.weibo import WEIBO_MESSAGE_PATTERN, WeiboExtractor
from .core.weibo.handler import WeiboMixin
from .core.xiaohongshu import (
    XHS_MESSAGE_PATTERN,
    XiaohongshuCardRenderer,
    XiaohongshuExtractor,
)
from .core.xiaohongshu.handler import XiaohongshuMixin


@register(
    "astrbot_plugin_link_resolver_plus",
    "Astraea35",
    "解析 & 下载 & AI升图 & AVIF压缩 (B站/抖音/小红书/微博/X)",
    "1.0.10-mod",
)
class LinkResolverPlugin(
    ConfigMixin,
    BaseUtilsMixin,
    CommandsMixin,
    ImageToolMixin,
    BilibiliMixin,
    DouyinMixin,
    XiaohongshuMixin,
    WeiboMixin,
    TwitterMixin,
    Star,
):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or context.get_config()

        # 初始化任务进度与解耦的媒体管道
        self.current_task_info = None
        self.upscaler = UpscaylUpscaler(self)
        self.encoder = MediaEncoder(self)

        self._cancel_previous_parse_tasks()
        self._active_parse_tasks: set[asyncio.Task] = set()
        self.douyin_extractor = DouyinExtractor()
        self.weibo_extractor = WeiboExtractor()
        self.xhs_extractor = XiaohongshuExtractor()
        self.twitter_extractor = TwitterExtractor()
        self.font_auto_install_enabled = False
        self.custom_primary_font_path: str | None = None
        self.custom_emoji_font_path: str | None = None
        self.user_primary_font_ready = False
        self.user_emoji_font_ready = False
        self.managed_primary_font_ready = False
        self.managed_emoji_font_ready = False
        self.xhs_renderer: XiaohongshuCardRenderer | None = None
        self._refresh_config()

    async def initialize(self) -> None:
        if not self.font_auto_install_enabled:
            return
        managed_paths = await asyncio.to_thread(install_managed_fonts)
        self.managed_primary_font_ready = managed_paths.primary is not None
        self.managed_emoji_font_ready = managed_paths.emoji is not None

        # 启动后台 7 天缓存自动清理
        asyncio.create_task(self._auto_clean_expired_cache())

    # region 事件正则过滤器与路由
    @filter.regex(BILI_MESSAGE_PATTERN, priority=10)
    async def handle_bili_video(self, event: AstrMessageEvent):
        self._refresh_config()
        if self._has_json_component(event) or not self._is_message_allowed(event):
            return
        if not self.bili_enable_auto_download:
            logger.info("⏭️ B站自动下载已关闭，跳过处理")
            return
        notify_id = await self._send_notify(event, "⏳ 正在解析 B站 视频，请稍候...")
        try:
            self._register_parse_task("bili", event)
            await BilibiliMixin.handle_bili_video(self, event)
        finally:
            await self._recall_notify(event, notify_id)

    @filter.regex(DOUYIN_MESSAGE_PATTERN, priority=10)
    async def handle_douyin(self, event: AstrMessageEvent):
        self._refresh_config()
        if self._has_json_component(event) or not self._is_message_allowed(event):
            return
        notify_id = await self._send_notify(event, "⏳ 正在解析 抖音 媒体，请稍候...")
        try:
            self._register_parse_task("douyin", event)
            await DouyinMixin.handle_douyin(self, event)
        finally:
            await self._recall_notify(event, notify_id)

    @filter.regex(XHS_MESSAGE_PATTERN, priority=10)
    async def handle_xhs(self, event: AstrMessageEvent):
        self._refresh_config()
        if self._has_json_component(event) or not self._is_message_allowed(event):
            return
        notify_id = await self._send_notify(event, "⏳ 正在解析 小红书 内容，请稍候...")
        try:
            self._register_parse_task("xhs", event)
            async for result in XiaohongshuMixin.handle_xhs(self, event):
                yield result
        finally:
            await self._recall_notify(event, notify_id)

    @filter.regex(WEIBO_MESSAGE_PATTERN, priority=10)
    async def handle_weibo(self, event: AstrMessageEvent):
        self._refresh_config()
        if self._has_json_component(event) or not self._is_message_allowed(event):
            return
        self._register_parse_task("weibo", event)
        await WeiboMixin.handle_weibo(self, event)

    @filter.regex(TWITTER_MESSAGE_PATTERN, priority=10)
    async def handle_twitter(self, event: AstrMessageEvent):
        self._refresh_config()
        if self._has_json_component(event) or not self._is_message_allowed(event):
            return
        self._register_parse_task("twitter", event)
        await TwitterMixin.handle_twitter(self, event)

    @filter.regex(r".*")
    async def handle_json_card(self, event: AstrMessageEvent):
        # Ensure both group and private filters use the latest configuration.
        self._refresh_config()
        if self._is_self_message(event) or not self._is_message_allowed(event):
            return

        links: list[str] = []
        has_json_component = False
        if hasattr(event, "message_obj") and hasattr(event.message_obj, "message"):
            for component in event.message_obj.message:
                is_json_component = False
                comp_payload = component
                if isinstance(component, dict):
                    comp_type = component.get("type")
                    if comp_type == "reply":
                        continue
                    comp_payload = component.get("data") or component
                    is_json_component = bool(comp_type) and "json" in str(comp_type).lower()
                else:
                    if isinstance(component, Comp.Json):
                        is_json_component = True
                    comp_type = getattr(component, "type", None)
                    if not is_json_component and comp_type:
                        is_json_component = "json" in str(comp_type).lower()
                    if is_json_component and hasattr(component, "data"):
                        comp_payload = component.data
                if is_json_component:
                    has_json_component = True
                    links.extend(self.extract_links_from_json(comp_payload))

        if not has_json_component or await self._is_bot_muted(event) or not links:
            return

        unique_links = list(dict.fromkeys(links))
        bili_links = [link for link in unique_links if re.search(BILI_MESSAGE_PATTERN, link)]
        douyin_links = [link for link in unique_links if re.search(DOUYIN_MESSAGE_PATTERN, link)]
        xhs_links = [link for link in unique_links if re.search(XHS_MESSAGE_PATTERN, link)]
        weibo_links = [link for link in unique_links if re.search(WEIBO_MESSAGE_PATTERN, link)]
        twitter_links = [link for link in unique_links if re.search(TWITTER_MESSAGE_PATTERN, link)]

        if bili_links and self.bili_enabled and self.bili_enable_auto_download:
            notify_id = await self._send_notify(event, "⏳ 正在解析卡片中的 B站 链接，请稍候...")
            try:
                self._register_parse_task("json-bili", event)
                event.should_call_llm(True)
                ref = await self._resolve_video_ref_from_links(bili_links)
                if ref:
                    await self._process_bili_video(event, ref=ref, is_from_card=True)
                    return
            finally:
                await self._recall_notify(event, notify_id)

        if douyin_links and self.douyin_enabled:
            notify_id = await self._send_notify(event, "⏳ 正在解析卡片中的 抖音 链接，请稍候...")
            try:
                self._register_parse_task("json-douyin", event)
                event.should_call_llm(True)
                await self._process_douyin(event, douyin_links[0], is_from_card=True)
                return
            finally:
                await self._recall_notify(event, notify_id)

        if xhs_links and self.xhs_enabled:
            notify_id = await self._send_notify(event, "⏳ 正在解析卡片中的 小红书 链接，请稍候...")
            try:
                self._register_parse_task("json-xhs", event)
                event.should_call_llm(True)
                async for result in self._process_xhs(event, xhs_links[0], is_from_card=True):
                    await event.send(result)
                return
            finally:
                await self._recall_notify(event, notify_id)

        if weibo_links and self.weibo_enabled:
            notify_id = await self._send_notify(event, "⏳ 正在解析卡片中的 微博 链接，请稍候...")
            try:
                self._register_parse_task("json-weibo", event)
                event.should_call_llm(True)
                await self._process_weibo(event, weibo_links[0], is_from_card=True)
                return
            finally:
                await self._recall_notify(event, notify_id)

        if twitter_links and self.twitter_enabled:
            notify_id = await self._send_notify(event, "⏳ 正在解析卡片中的 X 链接，请稍候...")
            try:
                self._register_parse_task("json-twitter", event)
                event.should_call_llm(True)
                await self._process_twitter(event, twitter_links[0], is_from_card=True)
                return
            finally:
                await self._recall_notify(event, notify_id)

    @filter.command("升图")
    async def handle_image_tool_upscale(self, event: AstrMessageEvent):
        logger.info("[ImageTool] received /升图 command")
        async for result in self.cmd_image_tool_upscale(event):
            yield result

    @filter.command("avif")
    async def handle_image_tool_avif(self, event: AstrMessageEvent):
        logger.info("[ImageTool] received /avif command")
        async for result in self.cmd_image_tool_avif(event):
            yield result

    @filter.command("升图进度")
    async def handle_image_tool_progress(self, event: AstrMessageEvent):
        async for result in self._do_query_xhs_progress(event):
            yield result

    @filter.command("avif进度")
    async def handle_image_tool_avif_progress(self, event: AstrMessageEvent):
        async for result in self._do_query_xhs_progress(event):
            yield result
    # endregion


LinkResolver = LinkResolverPlugin
Main = LinkResolverPlugin
