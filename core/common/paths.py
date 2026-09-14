# region 插件路径管理器（延迟初始化）
"""
使用 StarTools.get_data_dir() 获取数据存储目录。
在首次访问时延迟初始化，因为 StarTools 需要在 AstrBot 上下文初始化后才能使用。
"""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

PLUGIN_NAME = "astrbot_plugin_link_resolver_plus"

# 路径缓存
_data_dir: Path | None = None
_initialized: bool = False


def _get_data_dir() -> Path:
    """获取插件数据目录（延迟初始化）"""
    global _data_dir, _initialized
    if _data_dir is not None:
        return _data_dir

    try:
        from astrbot.api.star import StarTools

        _data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        _initialized = True
    except Exception:
        # 回退到插件目录（开发/测试环境）
        _data_dir = Path(__file__).resolve().parents[2] / "data"
        _data_dir.mkdir(parents=True, exist_ok=True)

    return _data_dir


def _ensure_dir(path: Path) -> Path:
    """确保目录存在"""
    path.mkdir(parents=True, exist_ok=True)
    return path


# region 路径获取函数
def get_cache_path() -> Path:
    """获取缓存根目录"""
    return _ensure_dir(_get_data_dir() / "cache")


def get_cookies_path() -> Path:
    """获取 Cookies 目录"""
    return _ensure_dir(_get_data_dir() / "cookies")


def get_fonts_path() -> Path:
    """获取插件字体目录"""
    return _ensure_dir(_get_data_dir() / "fonts")


def get_bili_cookies_file() -> Path:
    """获取 B站 Cookies 文件路径"""
    return get_cookies_path() / "bili_cookies.txt"


# Bilibili 路径
def get_bilibili_cache() -> Path:
    return _ensure_dir(get_cache_path() / "bilibili")


def get_bilibili_video_path() -> Path:
    return _ensure_dir(get_bilibili_cache() / "videos")


def get_bilibili_thumb_path() -> Path:
    return _ensure_dir(get_bilibili_cache() / "thumbnails")


def get_bilibili_card_path() -> Path:
    return _ensure_dir(get_bilibili_cache() / "cards")


# Douyin 路径
def get_douyin_cache() -> Path:
    return _ensure_dir(get_cache_path() / "douyin")


def get_douyin_video_path() -> Path:
    return _ensure_dir(get_douyin_cache() / "videos")


def get_douyin_image_path() -> Path:
    return _ensure_dir(get_douyin_cache() / "images")


def get_douyin_card_path() -> Path:
    return _ensure_dir(get_douyin_cache() / "cards")


# Xiaohongshu 路径
def get_xhs_cache() -> Path:
    return _ensure_dir(get_cache_path() / "xiaohongshu")


def get_xhs_video_path() -> Path:
    return _ensure_dir(get_xhs_cache() / "videos")


def get_xhs_image_path() -> Path:
    return _ensure_dir(get_xhs_cache() / "images")


def get_xhs_card_path() -> Path:
    return _ensure_dir(get_xhs_cache() / "cards")


# Weibo 路径
def get_weibo_cache() -> Path:
    return _ensure_dir(get_cache_path() / "weibo")


def get_weibo_video_path() -> Path:
    return _ensure_dir(get_weibo_cache() / "videos")


def get_weibo_image_path() -> Path:
    return _ensure_dir(get_weibo_cache() / "images")


# Twitter/X 路径
def get_twitter_cache() -> Path:
    return _ensure_dir(get_cache_path() / "twitter")


def get_twitter_video_path() -> Path:
    return _ensure_dir(get_twitter_cache() / "videos")


def get_twitter_image_path() -> Path:
    return _ensure_dir(get_twitter_cache() / "images")


# Extended platforms paths
def get_extended_media_path() -> Path:
    '''Get the cache root shared by generic extended-platform downloads.'''
    return _ensure_dir(get_cache_path() / 'extended_platforms')


# 内置资源路径
def get_plugin_root() -> Path:
    """获取插件根路径"""
    return Path(__file__).resolve().parents[2]


def get_default_upscayl_bin_path() -> Path:
    """获取默认内置 upscayl-bin.exe 路径"""
    return get_plugin_root() / "resources" / "bin" / "upscayl-bin.exe"


def get_default_upscayl_models_path() -> Path:
    """获取默认内置 models 目录"""
    return get_plugin_root() / "resources" / "models"


def get_default_span_bin_path() -> Path:
    """获取默认内置 SPAN NCNN 可执行文件路径"""
    return get_plugin_root() / "resources" / "bin" / "span-ncnn-vulkan.exe"


def get_default_span_models_path() -> Path:
    """获取默认内置 SPAN 模型目录"""
    return get_plugin_root() / "resources" / "span_models"


def get_default_animejanai_models_path() -> Path:
    """获取旧版随插件目录部署的 AnimeJaNai 模型目录。"""
    return get_plugin_root() / "resources" / "animejanai_models"


def get_persistent_ai_upscale_root() -> Path:
    """获取不会随插件或实例版本更新覆盖的升图资源根目录。"""
    for parent in get_plugin_root().parents:
        if parent.name == "core" and (parent / "astrbot").is_dir():
            return _ensure_dir(parent.parent / "ai_upscale")

    return _ensure_dir(_get_data_dir() / "ai_upscale")


def get_persistent_upscayl_bin_path() -> Path:
    return get_persistent_ai_upscale_root() / "bin" / "upscayl-bin.exe"


def get_persistent_upscayl_models_path() -> Path:
    return _ensure_dir(get_persistent_ai_upscale_root() / "models")


def get_persistent_span_bin_path() -> Path:
    return get_persistent_ai_upscale_root() / "bin" / "span-ncnn-vulkan.exe"


def get_persistent_span_models_path() -> Path:
    return _ensure_dir(get_persistent_ai_upscale_root() / "span_models")


def get_persistent_animejanai_models_path() -> Path:
    """获取不会随插件或实例版本更新覆盖的 AnimeJaNai 模型目录。"""
    for parent in get_plugin_root().parents:
        if parent.name == "core" and (parent / "astrbot").is_dir():
            return _ensure_dir(parent.parent / "models" / "animejanai")

    return get_plugin_data_animejanai_models_path()


def get_persistent_classifier_models_path() -> Path:
    """Get the instance-level classifier directory preserved across updates."""
    for parent in get_plugin_root().parents:
        if parent.name == "core" and (parent / "astrbot").is_dir():
            return _ensure_dir(parent.parent / "models" / "classifier")

    return _ensure_dir(_get_data_dir() / "models" / "classifier")


def get_plugin_data_animejanai_models_path() -> Path:
    """获取 v1.8.7 及以前使用的插件数据目录，用于一次性迁移。"""
    return _ensure_dir(_get_data_dir() / "models" / "animejanai")


def get_default_hat_models_path() -> Path:
    """获取默认 HAT 模型目录"""
    return get_plugin_root() / "resources" / "hat_models"


# endregion
# endregion
