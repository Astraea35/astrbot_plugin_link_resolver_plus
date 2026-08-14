"""小红书内容提取器 - 基于 astrbot_plugin_parser 参考实现重写"""

import asyncio
import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from astrbot.api import logger


# region 常量
XHS_REQUEST_TIMEOUT_SEC = 30.0
XHS_SHORT_LINK_PATTERN = r"(?:https?://)?(?:www\.)?xhslink\.(?:com|cn)/[A-Za-z0-9._?%&+=/#@-]+"
XHS_MESSAGE_PATTERN = (
    r"(?s).*(?:"
    + XHS_SHORT_LINK_PATTERN
    + r"|(?:https?://)?(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/[0-9a-zA-Z]+)"
)

# User-Agent
_XHS_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/55.0.2883.87 UBrowser/6.2.4098.3 Safari/537.36"
)
_XHS_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.6 Mobile/15E148 Safari/604.1 Edg/132.0.0.0"
)
_XHS_DOWNLOAD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# 通用 headers
_COMMON_HEADERS = {
    "User-Agent": _XHS_DESKTOP_UA,
}

# headers (用于短链接重定向)
XHS_HEADERS = {
    "User-Agent": _XHS_MOBILE_UA,
    "origin": "https://www.xiaohongshu.com",
    "x-requested-with": "XMLHttpRequest",
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
}

# explore 页面 headers
_EXPLORE_HEADERS = {
    "User-Agent": _XHS_DESKTOP_UA,
    "accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
}


_SHORT_RE = re.compile(XHS_SHORT_LINK_PATTERN, re.IGNORECASE)
_NOTE_ID_RE = re.compile(r"/(?:explore|discovery/item)/(?P<id>[0-9a-zA-Z]+)", re.IGNORECASE)
_LONG_RE = re.compile(
    r"(?:https?://)?(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/[0-9a-zA-Z]+[A-Za-z0-9._%?&+=/#@-]*",
    re.IGNORECASE
)
# endregion


def extract_xhs_links(text: str) -> list[str]:
    """从文本中提取所有小红书链接"""
    links = []
    # 短链接
    for match in _SHORT_RE.finditer(text):
        url = match.group(0)
        if not url.startswith("http"):
            url = "https://" + url
        links.append(url)
    # 长链接
    for match in _LONG_RE.finditer(text):
        url = match.group(0)
        if not url.startswith("http"):
            url = "https://" + url
        if url not in links:
            links.append(url)
    return links


# region 数据类
@dataclass(slots=True)
class XiaohongshuResult:
    title: str | None
    author: str | None
    text: str | None
    image_urls: list[str]
    file_ids: list[str]  # 用于尝试下载原图
    video_url: str | None
    cover_url: str | None
    source_url: str
    live_photo_urls: list[str | None] = field(default_factory=list)  # 与图片索引对齐的实况图 MP4 链接
    note_id: str | None = None
    image_url_candidates: list[list[str]] = field(default_factory=list)
    video_urls: list[str] = field(default_factory=list)
    live_photo_url_candidates: list[list[str]] = field(default_factory=list)


class XiaohongshuParseError(RuntimeError):
    pass


class XiaohongshuRetryableError(XiaohongshuParseError):
    """可重试的解析错误（网络抖动、临时服务异常等）"""

    pass
# endregion


class XiaohongshuExtractor:
    """小红书内容提取器"""

    def __init__(
        self,
        timeout: float = XHS_REQUEST_TIMEOUT_SEC,
    ):
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def parse(self, text_or_url: str) -> XiaohongshuResult:
        """解析小红书链接"""
        url = text_or_url.strip()
        if not url.startswith("http"):
            url = "https://" + url

        # 短链接需要先获取重定向目标
        if _SHORT_RE.search(url):
            url = await self._get_redirect_url(url)
            logger.debug("🔗 小红书短链接解析完成: %s", url)

        # 提取 note_id
        match = _NOTE_ID_RE.search(url)
        if not match:
            raise XiaohongshuParseError(f"无法从URL提取笔记ID: {url}")
        note_id = match.group("id")
        logger.debug("📝 小红书笔记ID: %s", note_id)

        # 构建 explore URL（比 discovery/item 更稳定）
        query_string = ""
        if "?" in url:
            query_string = "?" + url.split("?", 1)[1]
        explore_url = f"https://www.xiaohongshu.com/explore/{note_id}{query_string}"

        # 尝试 explore 页面解析
        try:
            return await self._parse_explore(explore_url, note_id)
        except XiaohongshuParseError:
            logger.debug("⚠️ 小红书 explore 解析失败, 尝试 discovery")

        # 回退到 discovery 解析
        if "/discovery/item/" in url:
            return await self._parse_discovery(url)

        raise XiaohongshuParseError("无法解析小红书内容")

    async def _get_redirect_url(self, url: str) -> str:
        """获取短链接重定向目标（单次重定向）"""
        try:
            async with aiohttp.ClientSession(timeout=self.timeout, cookies=None) as session:
                async with session.get(url, headers=XHS_HEADERS, allow_redirects=False) as resp:
                    if resp.status in (429,) or resp.status >= 500:
                        raise XiaohongshuRetryableError(f"短链接请求临时失败: {resp.status}")
                    if resp.status >= 400:
                        raise XiaohongshuParseError(f"短链接请求失败: {resp.status}")
                    location = resp.headers.get("Location", url)
                    return location
        except asyncio.TimeoutError as e:
            timeout_sec = getattr(self.timeout, "total", None)
            timeout_label = f"{timeout_sec:.0f}s" if isinstance(timeout_sec, (int, float)) else "unknown"
            raise XiaohongshuRetryableError(f"短链接跳转超时 ({timeout_label})") from e
        except aiohttp.ClientError as e:
            raise XiaohongshuRetryableError(f"短链接请求网络异常: {e}") from e

    async def _parse_explore(self, url: str, note_id: str) -> XiaohongshuResult:
        """解析 explore 页面"""
        try:
            async with aiohttp.ClientSession(timeout=self.timeout, cookies=None) as session:
                async with session.get(url, headers=_EXPLORE_HEADERS) as resp:
                    logger.debug("🔍 小红书 explore URL: %s, 状态: %s", resp.url, resp.status)
                    if resp.status in (429,) or resp.status >= 500:
                        raise XiaohongshuRetryableError(f"explore 页面临时失败: {resp.status}")
                    if resp.status != 200:
                        raise XiaohongshuParseError(f"页面请求失败: {resp.status}")
                    html = await resp.text()
        except asyncio.TimeoutError as e:
            timeout_sec = getattr(self.timeout, "total", None)
            timeout_label = f"{timeout_sec:.0f}s" if isinstance(timeout_sec, (int, float)) else "unknown"
            raise XiaohongshuRetryableError(f"explore 页面请求超时 ({timeout_label})") from e
        except aiohttp.ClientError as e:
            raise XiaohongshuRetryableError(f"explore 页面网络异常: {e}") from e

        json_obj = self._extract_initial_state(html)

        note_data = (
            json_obj.get("note", {})
            .get("noteDetailMap", {})
            .get(note_id, {})
            .get("note", {})
        )
        if not note_data:
            raise XiaohongshuParseError("无法找到笔记详情数据")

        return self._build_result_from_note(note_data, url, preload_data=None)

    async def _parse_discovery(self, url: str) -> XiaohongshuResult:
        """解析 discovery 页面"""
        try:
            async with aiohttp.ClientSession(timeout=self.timeout, cookies=None) as session:
                async with session.get(url, headers=XHS_HEADERS, allow_redirects=True) as resp:
                    logger.debug("🔍 小红书 discovery URL: %s, 状态: %s", resp.url, resp.status)
                    if resp.status in (429,) or resp.status >= 500:
                        raise XiaohongshuRetryableError(f"discovery 页面临时失败: {resp.status}")
                    if resp.status != 200:
                        raise XiaohongshuParseError(f"页面请求失败: {resp.status}")
                    html = await resp.text()
        except asyncio.TimeoutError as e:
            timeout_sec = getattr(self.timeout, "total", None)
            timeout_label = f"{timeout_sec:.0f}s" if isinstance(timeout_sec, (int, float)) else "unknown"
            raise XiaohongshuRetryableError(f"discovery 页面请求超时 ({timeout_label})") from e
        except aiohttp.ClientError as e:
            raise XiaohongshuRetryableError(f"discovery 页面网络异常: {e}") from e

        json_obj = self._extract_initial_state(html)

        note_data_wrapper = json_obj.get("noteData")
        if not note_data_wrapper:
            raise XiaohongshuParseError("无法找到 noteData")

        preload_data = note_data_wrapper.get("normalNotePreloadData", {})
        note_data = note_data_wrapper.get("data", {}).get("noteData", {})
        if not note_data:
            raise XiaohongshuParseError("无法找到 noteData.data.noteData")

        return self._build_result_from_note(note_data, url, preload_data=preload_data)

    def _extract_initial_state(self, html: str) -> dict[str, Any]:
        """从HTML中提取 __INITIAL_STATE__ JSON"""
        pattern = r"window\.__INITIAL_STATE__=(.*?)</script>"
        match = re.search(pattern, html)
        if not match:
            raise XiaohongshuParseError("小红书分享链接失效或内容已删除")

        json_str = match.group(1).replace("undefined", "null")
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            raise XiaohongshuParseError(f"JSON解析失败: {e}") from e

    def _build_result_from_note(
        self,
        note: dict[str, Any],
        source_url: str,
        preload_data: dict[str, Any] | None = None,
    ) -> XiaohongshuResult:
        """从笔记数据构建结果，优先从 preload_data 获取部分字段"""
        title = note.get("title") or note.get("shareTitle")
        desc = note.get("desc") or note.get("shareDesc")
        if preload_data:
            title = title or preload_data.get("title")
            desc = desc or preload_data.get("desc")

        # 作者
        user = note.get("user") or note.get("author") or {}
        author = user.get("nickname") or user.get("nickName") or user.get("name")
        if not author and preload_data:
            pre_user = preload_data.get("user") or {}
            author = pre_user.get("nickname") or pre_user.get("nickName") or pre_user.get("name")

        # 图片列表：各数组只在确认图片地址后追加，以保持 file_id、实况视频与图片索引一致。
        image_list = note.get("imageList") or []
        image_urls: list[str] = []
        image_url_candidates: list[list[str]] = []
        file_ids: list[str | None] = []
        live_photo_urls: list[str | None] = []
        live_photo_url_candidates: list[list[str]] = []
        for img in image_list:
            if not isinstance(img, dict):
                continue
            image_candidates = self._extract_image_urls(img)
            if not image_candidates:
                continue
            image_urls.append(image_candidates[0])
            image_url_candidates.append(image_candidates)
            file_ids.append(self._get_file_id_from_image(img))
            live_candidates = self._extract_live_photo_urls(img)
            live_photo_urls.append(live_candidates[0] if live_candidates else None)
            live_photo_url_candidates.append(live_candidates)

        # 视频
        video_urls = self._extract_video_urls(note)
        video_url = video_urls[0] if video_urls else None

        # 封面（视频的第一帧或第一张图片）
        cover_url = None
        if preload_data:
            preload_images = preload_data.get("imagesList") or []
            if preload_images and isinstance(preload_images[0], dict):
                cover_url = (
                    preload_images[0].get("urlSizeLarge")
                    or preload_images[0].get("url")
                )
        if not cover_url and image_urls:
            cover_url = image_urls[0]

        note_id = note.get("noteId") or note.get("id")
        return XiaohongshuResult(
            title=title if title else None,
            author=author if author else None,
            text=desc if desc else None,
            image_urls=image_urls,
            file_ids=file_ids,
            video_url=video_url,
            cover_url=cover_url,
            source_url=source_url,
            live_photo_urls=live_photo_urls,
            note_id=note_id,
            image_url_candidates=image_url_candidates,
            video_urls=video_urls,
            live_photo_url_candidates=live_photo_url_candidates,
        )

    def _extract_image_urls(self, img: dict[str, Any]) -> list[str]:
        candidates: list[str] = []
        for key in ("urlDefault", "url", "urlPre"):
            value = img.get(key)
            if not isinstance(value, str) or not value:
                continue
            url = html.unescape(value).split("!", 1)[0].strip()
            if url and url not in candidates:
                candidates.append(url)
        return candidates

    def _get_original_image_url(self, img: dict[str, Any]) -> str | None:
        """从图片对象获取最佳图片 URL。"""
        candidates = self._extract_image_urls(img)
        return candidates[0] if candidates else None
    def _get_file_id_from_image(self, img: dict[str, Any]) -> str | None:
        """从图片对象提取 fileId，用于构建原图 URL"""
        file_id = img.get("fileId") or img.get("file_id") or img.get("traceId")
        if not file_id:
            for key in ("urlDefault", "url", "urlPre"):
                url = img.get(key)
                if isinstance(url, str) and url:
                    extracted = self._extract_file_id_from_url(url)
                    if extracted:
                        file_id = extracted
                        break
        if file_id and "!" in file_id:
            file_id = file_id.split("!", 1)[0]
        return file_id

    def _extract_live_photo_url(self, img: dict[str, Any]) -> str | None:
        candidates = self._extract_live_photo_urls(img)
        return candidates[0] if candidates else None

    def _extract_live_photo_urls(self, img: dict[str, Any]) -> list[str]:
        """提取 Live Photo 的主地址及全部备用地址。"""
        candidates: list[str] = []
        for value in (img.get("livePhotoUrl"), img.get("live_photo_url")):
            url = self._normalize_media_url(value)
            if url and url not in candidates:
                candidates.append(url)
        for url in self._extract_stream_urls(img.get("stream")):
            if url not in candidates:
                candidates.append(url)
        return candidates
    @staticmethod
    def _extract_file_id_from_url(url: str) -> str | None:
        """从 URL 中提取 fileId"""
        if not url:
            return None
        if "spectrum/" in url:
            match = re.search(r"spectrum/([a-zA-Z0-9]+)", url)
            if match:
                return match.group(1)
        base_name = url.split("?")[0].split("!")[0]
        base_name = base_name.split("/")[-1]
        if len(base_name) > 20 and re.match(r"^[a-zA-Z0-9]+$", base_name):
            return base_name
        return None

    def _extract_video_url(self, note: dict[str, Any]) -> str | None:
        urls = self._extract_video_urls(note)
        return urls[0] if urls else None

    def _extract_video_urls(self, note: dict[str, Any]) -> list[str]:
        """按编码优先级提取视频主地址及备用地址。"""
        if note.get("type") != "video":
            return []
        video = note.get("video")
        if not isinstance(video, dict):
            return []
        media = video.get("media")
        if not isinstance(media, dict):
            return []
        return self._extract_stream_urls(media.get("stream"), prefer_h265=True)

    @staticmethod
    def _extract_stream_url(stream_item: dict[str, Any]) -> str | None:
        urls = XiaohongshuExtractor._extract_stream_urls(stream_item)
        return urls[0] if urls else None

    @staticmethod
    def _extract_stream_urls(
        stream: Any, *, prefer_h265: bool = False
    ) -> list[str]:
        if not isinstance(stream, dict):
            return []
        codecs = (
            ("h265", "h264", "av1", "h266")
            if prefer_h265
            else ("h264", "h265", "av1", "h266")
        )
        candidates: list[str] = []
        for codec in codecs:
            codec_streams = stream.get(codec)
            if not isinstance(codec_streams, list):
                continue
            for item in codec_streams:
                if not isinstance(item, dict):
                    continue
                values: list[object] = [
                    item.get("masterUrl"),
                    item.get("url"),
                    item.get("backupUrl"),
                ]
                backup_urls = item.get("backupUrls")
                if isinstance(backup_urls, list):
                    values.extend(backup_urls)
                elif isinstance(backup_urls, str):
                    values.append(backup_urls)
                for value in values:
                    url = XiaohongshuExtractor._normalize_media_url(value)
                    if url and url not in candidates:
                        candidates.append(url)
        return candidates
    @staticmethod
    def _normalize_media_url(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        media_url = html.unescape(value).strip()
        if media_url.startswith("//"):
            media_url = f"https:{media_url}"
        parsed = urlparse(media_url)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return media_url
        return None


# 导出
__all__ = [
    "XHS_HEADERS",
    "XHS_MESSAGE_PATTERN",
    "XHS_REQUEST_TIMEOUT_SEC",
    "XHS_SHORT_LINK_PATTERN",
    "XiaohongshuExtractor",
    "XiaohongshuParseError",
    "XiaohongshuRetryableError",
    "XiaohongshuResult",
    "extract_xhs_links",
]
