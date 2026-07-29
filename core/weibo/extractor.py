"""微博内容提取器。

流程：
1. 从文本中识别微博链接并规范化。
2. 需要时解析 t.cn 短链。
3. 优先使用用户提供的 Cookie，请求单条微博详情接口。
4. 若未提供用户 Cookie，则先生成访客 Cookie，再请求公开微博详情。
5. 将微博详情映射为插件内部统一结果，优先提取完整正文、原图和最高码率视频。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from astrbot.api import logger

WEIBO_REQUEST_TIMEOUT_SEC = 20.0
WEIBO_VISITOR_URL = "https://passport.weibo.com/visitor/genvisitor2"
WEIBO_VISITOR_FORM = {
    "cb": "visitor_gray_callback",
    "tid": "",
    "from": "weibo",
}
WEIBO_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    ),
    "Referer": "https://weibo.com/",
}
WEIBO_DOWNLOAD_HEADERS = {
    **WEIBO_BASE_HEADERS,
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}
WEIBO_API_HEADERS = {
    **WEIBO_BASE_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}

WEIBO_SHORT_LINK_PATTERN = r"(?:https?://)?t\.cn/[A-Za-z0-9]+/?"
_WEIBO_LONG_PATTERNS = [
    r"(?:https?://)?(?:www\.)?weibo\.com/(?P<uid>\d{10})/(?P<wid>[A-Za-z0-9]{9,16})(?:[/?#][^\s'\"<>]*)?",
    r"(?:https?://)?m\.weibo\.cn/(?P<kind>detail|status)/(?P<wid>[A-Za-z0-9]{9,16})(?:[/?#][^\s'\"<>]*)?",
    r"(?:https?://)?(?:www\.)?weibo\.cn/(?:detail/)?(?P<wid>[A-Za-z0-9]{9,16})(?:[/?#][^\s'\"<>]*)?",
]
_WEIBO_LONG_DETECT_PATTERNS = [
    r"(?:https?://)?(?:www\.)?weibo\.com/\d{10}/[A-Za-z0-9]{9,16}(?:[/?#][^\s'\"<>]*)?",
    r"(?:https?://)?m\.weibo\.cn/(?:detail|status)/[A-Za-z0-9]{9,16}(?:[/?#][^\s'\"<>]*)?",
    r"(?:https?://)?(?:www\.)?weibo\.cn/(?:detail/)?[A-Za-z0-9]{9,16}(?:[/?#][^\s'\"<>]*)?",
]
WEIBO_MESSAGE_PATTERN = (
    rf"(?s).*(?:{WEIBO_SHORT_LINK_PATTERN}|{'|'.join(_WEIBO_LONG_DETECT_PATTERNS)})"
)

_SHORT_RE = re.compile(WEIBO_SHORT_LINK_PATTERN, re.IGNORECASE)
_LONG_PATTERNS = [
    re.compile(pattern, re.IGNORECASE) for pattern in _WEIBO_LONG_PATTERNS
]
_TAG_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HTML_RE = re.compile(r"<[^>]+>")


@dataclass(slots=True)
class WeiboResult:
    title: str | None
    author: str | None
    text: str | None
    image_urls: list[str]
    video_url: str | None
    cover_url: str | None
    source_url: str
    weibo_id: str | None = None
    created_at: str | None = None


class WeiboParseError(RuntimeError):
    pass


class WeiboRetryableError(WeiboParseError):
    pass


class WeiboAuthError(WeiboParseError):
    pass


def extract_weibo_links(text: str) -> list[str]:
    links: list[str] = []
    if not text:
        return links

    for pattern in [WEIBO_SHORT_LINK_PATTERN, *_WEIBO_LONG_DETECT_PATTERNS]:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            url = _normalize_url(match.group(0))
            if url not in links:
                links.append(url)
    return links


def _normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip(")],.!?;")
    if not url:
        return url
    if url.startswith(("http://", "https://")):
        return url
    return f"https://{url}"


class WeiboExtractor:
    def __init__(
        self,
        timeout: float = WEIBO_REQUEST_TIMEOUT_SEC,
        *,
        download_original: bool = True,
    ):
        self.timeout = timeout
        self.download_original = download_original
        self.cookie = ""
        self._visitor_cookies: dict[str, str] | None = None

    def set_cookie(self, cookie: str | None) -> None:
        self.cookie = (cookie or "").strip()
        self._visitor_cookies = None

    def has_user_cookie(self) -> bool:
        return bool(self._parse_cookie_header(self.cookie))

    async def resolve_short_url(self, url: str) -> str:
        url = _normalize_url(url)
        async with httpx.AsyncClient(
            timeout=self.timeout,
            headers=WEIBO_BASE_HEADERS,
            follow_redirects=True,
        ) as client:
            try:
                response = await client.head(url)
                if response.status_code >= 400:
                    response = await client.get(url)
            except Exception:
                response = await client.get(url)
        return str(response.url)

    async def parse(self, text_or_url: str) -> WeiboResult:
        url = _normalize_url(text_or_url)
        if _SHORT_RE.search(url):
            url = await self.resolve_short_url(url)
            logger.debug("🔗 微博短链接解析完成: %s", url)

        weibo_id = self._extract_weibo_id(url)
        status = await self._fetch_status(weibo_id)
        result = self._build_result(status, url)
        if not result.video_url and not result.image_urls:
            raise WeiboParseError("微博中未找到可下载媒体")
        return result

    def _extract_weibo_id(self, url: str) -> str:
        for pattern in _LONG_PATTERNS:
            if match := pattern.search(url):
                wid = match.groupdict().get("wid")
                if wid:
                    return wid

        try:
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
        except Exception as exc:
            raise WeiboParseError(f"无法解析微博链接: {url}") from exc

        for key in ("id", "bid", "mid", "mblogid", "weibo_id", "status_id"):
            values = query.get(key) or []
            for value in values:
                value = (value or "").strip()
                if len(value) >= 9:
                    return value
        raise WeiboParseError(f"无法从微博链接提取微博 ID: {url}")

    async def _fetch_status(self, weibo_id: str) -> dict[str, Any]:
        errors: list[str] = []
        retry_attempts = 2

        for attempt in range(retry_attempts):
            try:
                cookies = await self._get_request_cookies(refresh_visitor=attempt > 0)
                return await self._fetch_status_json(weibo_id, cookies)
            except WeiboAuthError as exc:
                errors.append(str(exc))
                if self.has_user_cookie():
                    break
                self._visitor_cookies = None
            except WeiboRetryableError as exc:
                errors.append(str(exc))
                if attempt < retry_attempts - 1:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                break

        raise WeiboParseError("; ".join(errors) or "微博详情请求失败")

    async def _get_request_cookies(
        self, *, refresh_visitor: bool = False
    ) -> dict[str, str]:
        user_cookies = self._parse_cookie_header(self.cookie)
        if user_cookies:
            return user_cookies

        if self._visitor_cookies is None or refresh_visitor:
            self._visitor_cookies = await self._generate_visitor_cookies()
        return dict(self._visitor_cookies)

    async def _generate_visitor_cookies(self) -> dict[str, str]:
        headers = {
            "User-Agent": WEIBO_BASE_HEADERS["User-Agent"],
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, headers=headers
            ) as client:
                response = await client.post(WEIBO_VISITOR_URL, data=WEIBO_VISITOR_FORM)
                response.raise_for_status()
        except asyncio.TimeoutError as exc:
            raise WeiboRetryableError("生成微博访客 Cookie 超时") from exc
        except httpx.HTTPStatusError as exc:
            raise WeiboRetryableError(
                f"生成微博访客 Cookie 失败: {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise WeiboRetryableError(f"生成微博访客 Cookie 网络异常: {exc}") from exc

        cookies = self._parse_set_cookie_headers(response.headers)
        if not cookies.get("SUB"):
            raise WeiboAuthError("微博访客 Cookie 无效")
        return cookies

    async def _fetch_status_json(
        self, weibo_id: str, cookies: dict[str, str]
    ) -> dict[str, Any]:
        params = {"id": weibo_id}
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                headers=WEIBO_API_HEADERS,
                cookies=cookies,
                follow_redirects=True,
            ) as client:
                response = await client.get(
                    "https://weibo.com/ajax/statuses/show", params=params
                )
        except asyncio.TimeoutError as exc:
            raise WeiboRetryableError("微博详情请求超时") from exc
        except httpx.HTTPError as exc:
            raise WeiboRetryableError(f"微博详情网络异常: {exc}") from exc

        text = response.text or ""
        content_type = response.headers.get("Content-Type", "")
        if response.status_code == 404:
            raise WeiboParseError("微博不存在或已删除")
        if response.status_code in (401, 403):
            raise WeiboAuthError(f"微博详情鉴权失败: {response.status_code}")
        if response.status_code >= 500:
            raise WeiboRetryableError(f"微博详情临时失败: {response.status_code}")
        if "Sina Visitor System" in text:
            raise WeiboAuthError("微博要求访问凭证")

        try:
            data = response.json()
        except ValueError as exc:
            if "json" not in content_type.lower():
                raise WeiboAuthError("微博详情返回非 JSON 页面") from exc
            raise WeiboParseError("微博详情 JSON 解析失败") from exc

        if not isinstance(data, dict):
            raise WeiboParseError("微博详情响应结构异常")
        if data.get("ok") == 0:
            raise WeiboParseError(data.get("msg") or "微博详情接口返回失败")
        if not data.get("id") and not data.get("mblogid"):
            raise WeiboParseError("微博详情缺少关键字段")
        return data

    @staticmethod
    def _parse_cookie_header(raw: str | None) -> dict[str, str]:
        cookies: dict[str, str] = {}
        if not raw:
            return cookies
        for part in raw.split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key and value:
                cookies[key] = value
        return cookies

    @staticmethod
    def _parse_set_cookie_headers(headers: httpx.Headers) -> dict[str, str]:
        cookies: dict[str, str] = {}
        for item in headers.get_list("set-cookie"):
            segment = item.split(";", 1)[0].strip()
            if "=" not in segment:
                continue
            key, value = segment.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key and value:
                cookies[key] = value
        return cookies

    def _build_result(self, status: dict[str, Any], source_url: str) -> WeiboResult:
        display_status = status
        if not self._status_has_media(status) and isinstance(
            status.get("retweeted_status"), dict
        ):
            display_status = status["retweeted_status"]

        top_text = self._extract_text(status)
        display_text = self._extract_text(display_status)
        if display_status is status:
            final_text = display_text
        elif top_text and display_text and top_text != display_text:
            repost_author = self._extract_author(display_status)
            prefix = f"转发自 @{repost_author}:\n" if repost_author else "转发微博:\n"
            final_text = f"{top_text}\n\n{prefix}{display_text}"
        else:
            final_text = top_text or display_text

        image_urls = self._extract_image_urls(display_status)
        video_url, cover_url = self._extract_video(display_status)
        if not cover_url and image_urls:
            cover_url = image_urls[0]

        text_title = final_text.splitlines()[0].strip() if final_text else None
        if text_title and len(text_title) > 48:
            text_title = text_title[:45] + "..."

        return WeiboResult(
            title=text_title,
            author=self._extract_author(status) or self._extract_author(display_status),
            text=final_text,
            image_urls=image_urls,
            video_url=video_url,
            cover_url=cover_url,
            source_url=source_url,
            weibo_id=str(status.get("mblogid") or status.get("id") or "") or None,
            created_at=(
                status.get("created_at") or display_status.get("created_at") or None
            ),
        )

    @staticmethod
    def _status_has_media(status: dict[str, Any]) -> bool:
        if not isinstance(status, dict):
            return False
        if status.get("pic_infos") or status.get("pics"):
            return True
        page_info = status.get("page_info") or {}
        media_info = page_info.get("media_info") or {}
        return bool(page_info.get("type") == "video" or media_info)

    def _extract_image_urls(self, status: dict[str, Any]) -> list[str]:
        pic_infos = status.get("pic_infos") or {}
        pic_ids = status.get("pic_ids") or list(pic_infos.keys())
        urls: list[str] = []
        for pic_id in pic_ids:
            pic_info = pic_infos.get(pic_id) or {}
            url = self._pick_image_url(pic_info)
            if url and url not in urls:
                urls.append(url)

        if urls:
            return urls

        for item in status.get("pics") or []:
            if not isinstance(item, dict):
                continue
            url = self._pick_image_url(item)
            if url and url not in urls:
                urls.append(url)
        return urls

    def _pick_image_url(self, pic_info: dict[str, Any]) -> str | None:
        if not isinstance(pic_info, dict):
            return None

        if self.download_original:
            priorities = (
                "largest",
                "original",
                "mw2000",
                "large",
                "orj1080",
                "orj960",
                "bmiddle",
                "thumbnail",
                "url",
            )
        else:
            priorities = (
                "large",
                "mw2000",
                "orj960",
                "orj1080",
                "largest",
                "original",
                "bmiddle",
                "thumbnail",
                "url",
            )

        for key in priorities:
            value = pic_info.get(key)
            if isinstance(value, dict):
                url = value.get("url") or value.get("secure_url")
                if url:
                    return url
            elif isinstance(value, str) and value.startswith(("http://", "https://")):
                return value

        for value in pic_info.values():
            if isinstance(value, dict):
                url = value.get("url") or value.get("secure_url")
                if url:
                    return url
        return None

    def _extract_video(self, status: dict[str, Any]) -> tuple[str | None, str | None]:
        page_info = status.get("page_info") or {}
        media_info = page_info.get("media_info") or {}
        playback_list = media_info.get("playback_list") or []

        best_url = None
        best_bitrate = -1
        for item in playback_list:
            play_info = item.get("play_info") if isinstance(item, dict) else None
            if not isinstance(play_info, dict):
                continue
            url = play_info.get("url")
            bitrate = self._coerce_int(play_info.get("bitrate"))
            if url and bitrate >= best_bitrate:
                best_url = url
                best_bitrate = bitrate

        if not best_url:
            for key in (
                "stream_url_hd",
                "stream_url",
                "url",
                "mp4_720p_mp4",
                "mp4_hd_url",
                "mp4_sd_url",
            ):
                value = media_info.get(key) or page_info.get(key)
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    best_url = value
                    break
                if isinstance(value, dict):
                    url = value.get("url")
                    if isinstance(url, str) and url.startswith(("http://", "https://")):
                        best_url = url
                        break

        cover_url = None
        for candidate in (
            page_info.get("page_pic"),
            media_info.get("poster"),
            media_info.get("pic_info"),
            media_info.get("cover_img"),
        ):
            if isinstance(candidate, dict):
                url = candidate.get("url") or candidate.get("pic_url")
                if url:
                    cover_url = url
                    break
            elif isinstance(candidate, str) and candidate.startswith(
                ("http://", "https://")
            ):
                cover_url = candidate
                break

        return best_url, cover_url

    def _extract_text(self, status: dict[str, Any]) -> str | None:
        if not isinstance(status, dict):
            return None
        candidates = [
            status.get("longTextContent_raw"),
            status.get("longTextContent"),
            (status.get("longText") or {}).get("longTextContent_raw"),
            (status.get("longText") or {}).get("longTextContent"),
            status.get("text_raw"),
            status.get("text"),
        ]
        for candidate in candidates:
            cleaned = self._clean_text(candidate)
            if cleaned:
                return cleaned
        return None

    @staticmethod
    def _extract_author(status: dict[str, Any]) -> str | None:
        user = status.get("user") if isinstance(status, dict) else None
        if not isinstance(user, dict):
            return None
        return user.get("screen_name") or user.get("name")

    @staticmethod
    def _clean_text(value: Any) -> str | None:
        if not value or not isinstance(value, str):
            return None
        value = _TAG_RE.sub("\n", value)
        value = _HTML_RE.sub("", value)
        value = unescape(value)
        value = value.replace("\u200b", "")
        lines = [line.strip() for line in value.splitlines()]
        cleaned = "\n".join(line for line in lines if line)
        return cleaned or None

    @staticmethod
    def _coerce_int(value: Any) -> int:
        try:
            return int(value)
        except Exception:
            return 0
