'''Platform matching rules for sites handled by generic download engines.'''

from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class ExtendedPlatform:
    '''A platform delegated to yt-dlp and, when needed, gallery-dl.'''

    key: str
    label: str
    domains: tuple[str, ...]
    gallery_dl_first: bool = False

    def matches(self, url: str) -> bool:
        host = (urlparse(url).hostname or '').lower().rstrip('.')
        return any(host == domain or host.endswith(f'.{domain}') for domain in self.domains)


EXTENDED_PLATFORMS: tuple[ExtendedPlatform, ...] = (
    ExtendedPlatform('kuaishou', '快手', ('kuaishou.com',)),
    ExtendedPlatform('wechat_channels', '视频号', ('channels.weixin.qq.com', 'finder.video.qq.com')),
    ExtendedPlatform('zhihu', '知乎', ('zhihu.com', 'zhimg.com')),
    ExtendedPlatform('xiaoheihe', '小黑盒', ('xiaoheihe.com',)),
    ExtendedPlatform('acfun', 'A站', ('acfun.cn',)),
    ExtendedPlatform('youtube', 'YouTube', ('youtube.com', 'youtu.be', 'youtube-nocookie.com')),
    ExtendedPlatform('tiktok', 'TikTok', ('tiktok.com',)),
    ExtendedPlatform('instagram', 'Instagram', ('instagram.com',), gallery_dl_first=True),
    ExtendedPlatform('pixiv', 'Pixiv', ('pixiv.net', 'pixiv.me'), gallery_dl_first=True),
    ExtendedPlatform('iwara', 'Iwara', ('iwara.tv',)),
    ExtendedPlatform('netease_music', '网易云', ('music.163.com', 'y.music.163.com', '163cn.tv')),
    ExtendedPlatform('nga', 'NGA', ('nga.178.com', 'bbs.nga.cn', 'nga.cn')),
)

EXTENDED_PLATFORM_LABELS = tuple(platform.label for platform in EXTENDED_PLATFORMS)

_DOMAIN_PATTERN = '|'.join(
    re.escape(domain)
    for platform in EXTENDED_PLATFORMS
    for domain in platform.domains
)
EXTENDED_MESSAGE_PATTERN = rf'(?i)https?://(?:[\w-]+\.)?(?:{_DOMAIN_PATTERN})(?:/[^\s<>\x22\x27]*)?'


def find_extended_platform(url: str) -> ExtendedPlatform | None:
    '''Return the extended-platform definition responsible for a URL.'''

    for platform in EXTENDED_PLATFORMS:
        if platform.matches(url):
            return platform
    return None


__all__ = [
    'EXTENDED_MESSAGE_PATTERN',
    'EXTENDED_PLATFORM_LABELS',
    'EXTENDED_PLATFORMS',
    'ExtendedPlatform',
    'find_extended_platform',
]
