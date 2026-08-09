"""插件字体管理

1. 根据配置决定是否启用插件托管字体
2. 在插件数据目录中按需下载字体文件
3. 为渲染层提供的字体
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from astrbot.api import logger

from .paths import get_fonts_path


@dataclass(frozen=True, slots=True)
class ManagedFontAsset:
    name: str
    role: str
    download_urls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManagedFontPaths:
    primary: Path | None
    emoji: Path | None


@dataclass(frozen=True, slots=True)
class UserFontPaths:
    primary: Path | None
    emoji: Path | None


PRIMARY_FONT = ManagedFontAsset(
    name="NotoSansCJKsc-Regular.otf",
    role="中文主字体",
    download_urls=(
        "https://fastly.jsdelivr.net/gh/notofonts/noto-cjk@main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf",
        "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf",
    ),
)
EMOJI_FONT = ManagedFontAsset(
    name="OpenMoji-black-glyf.ttf",
    role="Emoji 字体",
    download_urls=(
        "https://fastly.jsdelivr.net/gh/hfg-gmuend/openmoji@master/font/OpenMoji-black-glyf/OpenMoji-black-glyf.ttf",
        "https://raw.githubusercontent.com/hfg-gmuend/openmoji/master/font/OpenMoji-black-glyf/OpenMoji-black-glyf.ttf",
    ),
)

_managed_fonts_enabled = False
_user_primary_font_path: Path | None = None
_user_emoji_font_path: Path | None = None


def set_managed_fonts_enabled(enabled: bool) -> None:
    """设置是否优先使用插件托管字体."""
    global _managed_fonts_enabled
    _managed_fonts_enabled = bool(enabled)


def managed_fonts_enabled() -> bool:
    """返回当前是否启用插件托管字体."""
    return _managed_fonts_enabled


def set_user_font_paths(
    primary: str | Path | None,
    emoji: str | Path | None,
) -> None:
    """设置用户自定义字体路径。"""
    global _user_primary_font_path, _user_emoji_font_path
    _user_primary_font_path = _normalize_font_path(primary)
    _user_emoji_font_path = _normalize_font_path(emoji)


def get_user_font_paths() -> UserFontPaths:
    """返回当前配置的用户自定义字体路径。"""
    return UserFontPaths(
        primary=(
            _user_primary_font_path
            if _user_primary_font_path and _user_primary_font_path.is_file()
            else None
        ),
        emoji=(
            _user_emoji_font_path
            if _user_emoji_font_path and _user_emoji_font_path.is_file()
            else None
        ),
    )


def get_managed_primary_font_file() -> Path:
    """返回插件托管中文字体路径."""
    return get_fonts_path() / PRIMARY_FONT.name


def get_managed_emoji_font_file() -> Path:
    """返回插件托管 Emoji 字体路径."""
    return get_fonts_path() / EMOJI_FONT.name


def get_managed_font_paths() -> ManagedFontPaths:
    """返回当前可用的托管字体路径."""
    primary = get_managed_primary_font_file()
    emoji = get_managed_emoji_font_file()
    return ManagedFontPaths(
        primary=primary if _is_valid_font_file(primary) else None,
        emoji=emoji if _is_valid_font_file(emoji) else None,
    )


def install_managed_fonts(timeout_sec: float = 90.0) -> ManagedFontPaths:
    """下载并安装插件托管字体.

    下载失败不会抛出到插件加载; 继续使用系统字体.
    """
    assets = (
        (PRIMARY_FONT, get_managed_primary_font_file()),
        (EMOJI_FONT, get_managed_emoji_font_file()),
    )
    for asset, target in assets:
        if _is_valid_font_file(target):
            logger.debug("🔤 插件字体已存在，跳过下载: %s", target)
            continue
        try:
            _download_font_asset(asset, target, timeout_sec)
            logger.info("🔤 已安装%s: %s", asset.role, target)
        except Exception as exc:
            target.unlink(missing_ok=True)
            logger.warning(
                "⚠️ %s下载失败: %s.可手动放置到 %s",
                asset.role,
                str(exc),
                target,
            )
    return get_managed_font_paths()


def _download_font_asset(
    asset: ManagedFontAsset, target: Path, timeout_sec: float
) -> None:
    """按顺序尝试多个源下载字体."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_suffix(target.suffix + ".download")
    temp_path.unlink(missing_ok=True)

    errors: list[str] = []
    for url in asset.download_urls:
        try:
            logger.info("🔤 正在安装%s: %s", asset.role, url)
            _download_to_path(url, temp_path, timeout_sec)
            if not _is_valid_font_file(temp_path):
                raise RuntimeError("下载结果为空文件")
            temp_path.replace(target)
            return
        except Exception as exc:
            temp_path.unlink(missing_ok=True)
            errors.append(f"{url} -> {exc}")

    raise RuntimeError("; ".join(errors))


def _download_to_path(url: str, target: Path, timeout_sec: float) -> None:
    """将远端文件下载到目标路径."""
    timeout = httpx.Timeout(timeout_sec, connect=min(timeout_sec, 20.0))
    with httpx.stream(
        "GET",
        url,
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": "astrbot-plugin-link-resolver/1.0"},
    ) as response:
        response.raise_for_status()
        with target.open("wb") as handle:
            for chunk in response.iter_bytes():
                if chunk:
                    handle.write(chunk)


def _is_valid_font_file(path: Path) -> bool:
    """判断字体文件是否生效."""
    return path.exists() and path.is_file() and path.stat().st_size > 0


def _normalize_font_path(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    return Path(raw).expanduser()


__all__ = [
    "EMOJI_FONT",
    "PRIMARY_FONT",
    "ManagedFontPaths",
    "UserFontPaths",
    "get_user_font_paths",
    "get_managed_emoji_font_file",
    "get_managed_font_paths",
    "get_managed_primary_font_file",
    "install_managed_fonts",
    "managed_fonts_enabled",
    "set_managed_fonts_enabled",
    "set_user_font_paths",
]
