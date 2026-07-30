# core/common/config_mixin.py
import re
from pathlib import Path
from astrbot.api import logger
from .card_renderer import find_default_font, find_emoji_font
from .font_manager import (
    get_managed_font_paths,
    get_user_font_paths,
    set_managed_fonts_enabled,
    set_user_font_paths,
)
from .paths import (
    get_bili_cookies_file,
    get_default_upscayl_bin_path,
    get_default_upscayl_models_path,
)
from ..xiaohongshu.render import XiaohongshuCardRenderer

SUMMARY_MODE_TEXT = "文字摘要"
SUMMARY_MODE_CARD = "渲染卡片"
DEFAULT_UPSCAYL_BIN_PATH = "C:/Program Files/Upscayl/resources/bin/upscayl-bin.exe"
DEFAULT_UPSCAYL_MODELS_PATH = "C:/Program Files/Upscayl/resources/models"


class ConfigMixin:
    """配置读取与设置管理 Mixin"""

    def _get_config_value(self, key: str, default):
        keys = key.split(".")
        val = getattr(self, "config", {})
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
            if val is None:
                return default
        return val

    def _read_summary_mode(self, key: str) -> str:
        mode = str(self._get_config_value(key, SUMMARY_MODE_TEXT)).strip()
        if mode not in (SUMMARY_MODE_TEXT, SUMMARY_MODE_CARD):
            return SUMMARY_MODE_TEXT
        return mode

    def _configure_managed_fonts(self) -> None:
        """根据配置决定是否启用用户字体和自动安装插件字体。"""
        custom_primary_font = str(
            self._get_config_value("general_settings.custom_font_path", "")
        ).strip()
        custom_emoji_font = str(
            self._get_config_value("general_settings.custom_emoji_font_path", "")
        ).strip()
        self.custom_primary_font_path = custom_primary_font or None
        self.custom_emoji_font_path = custom_emoji_font or None
        set_user_font_paths(custom_primary_font, custom_emoji_font)

        self.font_auto_install_enabled = bool(
            self._get_config_value("general_settings.auto_install_fonts", False)
        )
        set_managed_fonts_enabled(self.font_auto_install_enabled)
        if not self.font_auto_install_enabled:
            self.managed_primary_font_ready = False
            self.managed_emoji_font_ready = False

    def _refresh_config(self) -> None:
        self._configure_managed_fonts()
        user_font_paths = get_user_font_paths()
        managed_font_paths = get_managed_font_paths()
        self.managed_primary_font_ready = managed_font_paths.primary is not None
        self.managed_emoji_font_ready = managed_font_paths.emoji is not None
        self.default_primary_font = find_default_font()
        self.default_emoji_font = find_emoji_font()
        self.user_primary_font_ready = bool(
            user_font_paths.primary
            and self.default_primary_font == user_font_paths.primary
        )
        self.user_emoji_font_ready = bool(
            user_font_paths.emoji and self.default_emoji_font == user_font_paths.emoji
        )

        # 平台启用列表
        enable_platforms = self._get_config_value(
            "enable_platforms", ["B站", "抖音", "小红书", "微博", "X"]
        )
        if not isinstance(enable_platforms, list):
            enable_platforms = ["B站", "抖音", "小红书", "微博", "X"]
        self.bili_enabled = "B站" in enable_platforms
        self.douyin_enabled = "抖音" in enable_platforms
        self.xhs_enabled = "小红书" in enable_platforms
        self.weibo_enabled = "微博" in enable_platforms
        self.twitter_enabled = "X" in enable_platforms

        # B站配置
        self.quality_label = str(
            self._get_config_value("bili_settings.video_quality", "1080P高帧率")
        )
        self.codecs_label = str(
            self._get_config_value("bili_settings.video_codecs", "AVC")
        )
        self.allow_hdr = bool(self._get_config_value("bili_settings.allow_hdr", False))
        self.allow_dolby = bool(
            self._get_config_value("bili_settings.allow_dolby", False)
        )
        self.bili_merge_send = bool(
            self._get_config_value("bili_settings.merge_send", False)
        )
        self.bili_summary_mode = self._read_summary_mode("bili_settings.summary_mode")
        self.bili_render_card = self.bili_summary_mode == SUMMARY_MODE_CARD
        self.enable_multi_page = bool(
            self._get_config_value("bili_settings.enable_multi_page", True)
        )
        self.multi_page_max = max(
            1, int(self._get_config_value("bili_settings.multi_page_max", 3))
        )
        self.bili_max_duration_seconds = max(
            0, int(self._get_config_value("bili_settings.max_duration_seconds", 300))
        )
        self.allow_quality_fallback = bool(
            self._get_config_value("bili_settings.allow_quality_fallback", True)
        )
        self.bili_enable_auto_download = bool(
            self._get_config_value("bili_settings.enable_auto_download", True)
        )

        # 读取 B站 Cookie 写入文件
        bili_cookies_str = str(
            self._get_config_value("bili_settings.cookies", "")
        ).strip()
        if bili_cookies_str:
            try:
                cookies_file = get_bili_cookies_file()
                cookies_file.parent.mkdir(parents=True, exist_ok=True)
                if "\n" not in bili_cookies_str and ".bilibili.com" in bili_cookies_str:
                    bili_cookies_str = re.sub(
                        r"\s+(\.(?:www\.)?bilibili\.com\s)",
                        r"\n\1",
                        bili_cookies_str,
                    )
                    bili_cookies_str = bili_cookies_str.replace("# ", "\n# ").strip()
                cookies_file.write_text(bili_cookies_str, encoding="utf-8")
                logger.info("🍪 B站 Cookie 已从配置写入文件")
            except Exception as exc:
                logger.warning("⚠️ 写入 B站 Cookie 文件失败: %s", str(exc))

        # 抖音配置
        self.douyin_max_media = max(
            1, int(self._get_config_value("douyin_settings.max_media", 99))
        )
        self.douyin_merge_send = bool(
            self._get_config_value("douyin_settings.merge_send", False)
        )
        self.douyin_summary_mode = self._read_summary_mode(
            "douyin_settings.summary_mode"
        )
        self.douyin_render_card = self.douyin_summary_mode == SUMMARY_MODE_CARD
        self.douyin_enable_ai_upscale = bool(self._get_config_value("douyin_settings.enable_ai_upscale", True))
        self.douyin_low_quality_threshold = max(100, int(self._get_config_value("douyin_settings.low_quality_threshold", 1080)))
        self.douyin_upscayl_model_name = str(self._get_config_value("douyin_settings.upscayl_model_name", "自动 (CV特征识别)")).strip()

        # 微博配置
        self.weibo_max_media = max(
            1, int(self._get_config_value("weibo_settings.max_media", 99))
        )
        self.weibo_merge_send = bool(
            self._get_config_value("weibo_settings.merge_send", False)
        )
        self.weibo_download_original = bool(
            self._get_config_value("weibo_settings.download_original", True)
        )
        weibo_cookies_str = str(
            self._get_config_value("weibo_settings.cookies", "")
        ).strip()
        if hasattr(self, "weibo_extractor"):
            self.weibo_extractor.set_cookie(weibo_cookies_str)
            self.weibo_extractor.download_original = self.weibo_download_original
            self.weibo_cookie_enabled = self.weibo_extractor.has_user_cookie()
        self.weibo_enable_ai_upscale = bool(self._get_config_value("weibo_settings.enable_ai_upscale", True))
        self.weibo_low_quality_threshold = max(100, int(self._get_config_value("weibo_settings.low_quality_threshold", 1080)))
        self.weibo_upscayl_model_name = str(self._get_config_value("weibo_settings.upscayl_model_name", "自动 (CV特征识别)")).strip()

        # X 配置
        self.twitter_max_media = max(
            1, int(self._get_config_value("twitter_settings.max_media", 99))
        )
        self.twitter_merge_send = bool(
            self._get_config_value("twitter_settings.merge_send", False)
        )
        self.twitter_enable_ai_upscale = bool(self._get_config_value("twitter_settings.enable_ai_upscale", True))
        self.twitter_low_quality_threshold = max(100, int(self._get_config_value("twitter_settings.low_quality_threshold", 1080)))
        self.twitter_upscayl_model_name = str(self._get_config_value("twitter_settings.upscayl_model_name", "自动 (CV特征识别)")).strip()

        # 小红书配置
        self.xhs_max_media = max(
            1, int(self._get_config_value("xhs_settings.max_media", 99))
        )
        self.xhs_merge_send = bool(
            self._get_config_value("xhs_settings.merge_send", False)
        )
        self.xhs_summary_mode = self._read_summary_mode("xhs_settings.summary_mode")
        self.xhs_render_card = self.xhs_summary_mode == SUMMARY_MODE_CARD
        self.xhs_download_original = bool(
            self._get_config_value("xhs_settings.download_original", True)
        )
        self.xhs_prefer_ci_png = bool(
            self._get_config_value("xhs_settings.prefer_ci_png", True)
        )
        self.xhs_auto_unmerge_threshold_mb = int(
            self._get_config_value("xhs_settings.auto_unmerge_threshold_mb", 50)
        )
        self.xhs_qq_image_size_limit_mb = max(
            0, int(self._get_config_value("xhs_settings.qq_image_size_limit_mb", 30))
        )
        self.xhs_concurrent_download = bool(
            self._get_config_value("xhs_settings.concurrent_download", True)
        )
        self.xhs_image_merge_send = bool(self._get_config_value("xhs_settings.image_merge_send", False))
        self.xhs_enable_ai_upscale = bool(self._get_config_value("xhs_settings.enable_ai_upscale", True))
        self.xhs_low_quality_threshold = max(100, int(self._get_config_value("xhs_settings.low_quality_threshold", 1080)))
        self.xhs_upscayl_model_name = str(self._get_config_value("xhs_settings.upscayl_model_name", "自动 (CV特征识别)")).strip()
        self.upscayl_model_name = self.xhs_upscayl_model_name
        self.upscayl_double_pass = bool(self._get_config_value("xhs_settings.upscayl_double_pass", True))
        self.upscayl_enable_taa = bool(self._get_config_value("xhs_settings.upscayl_enable_taa", True))
        self.upscayl_scale = max(1, int(self._get_config_value("xhs_settings.upscayl_scale", 2)))
        
        # Upscayl 资源路径统一由通用设置管理；无有效设置时回退到插件 resources 目录。
        user_bin = str(
            self._get_config_value(
                "general_settings.upscayl_bin_path", DEFAULT_UPSCAYL_BIN_PATH
            )
        ).strip().strip('"').strip("'")
        user_models = str(
            self._get_config_value(
                "general_settings.upscayl_models_path", DEFAULT_UPSCAYL_MODELS_PATH
            )
        ).strip().strip('"').strip("'")

        builtin_bin = get_default_upscayl_bin_path()
        builtin_models = get_default_upscayl_models_path()

        if user_bin and Path(user_bin).is_file():
            self.upscayl_bin_path = user_bin
        elif builtin_bin.is_file():
            self.upscayl_bin_path = str(builtin_bin.resolve())
        else:
            self.upscayl_bin_path = user_bin or DEFAULT_UPSCAYL_BIN_PATH

        if user_models and Path(user_models).is_dir():
            self.upscayl_models_path = user_models
        elif builtin_models.is_dir():
            self.upscayl_models_path = str(builtin_models.resolve())
        else:
            self.upscayl_models_path = user_models or DEFAULT_UPSCAYL_MODELS_PATH

        self.ffmpeg_bin_path = str(self._get_config_value("xhs_settings.ffmpeg_bin_path", "ffmpeg")).strip()

        # 通用设置
        self.enable_global_ffmpeg_compress = bool(self._get_config_value("general_settings.enable_ffmpeg_compress", True))
        self.progress_report_interval = max(1, min(100, int(self._get_config_value("general_settings.progress_report_interval", 1))))
        self.retry_count = max(
            0, int(self._get_config_value("general_settings.retry_count", 3))
        )
        self.reaction_emoji_enabled = bool(
            self._get_config_value("general_settings.reaction_emoji_enabled", True)
        )
        _raw_list = self._get_config_value(
            "general_settings.reaction_emoji_list", [127827]
        )
        _emoji_list: list[int] = []
        if isinstance(_raw_list, list):
            for item in _raw_list[:5]:
                coerced = self._coerce_positive_int(item, 0)
                if coerced > 0:
                    _emoji_list.append(coerced)
        self.reaction_emoji_list = _emoji_list
        _strategy = str(
            self._get_config_value("general_settings.reaction_emoji_strategy", "随机")
        ).strip()
        self.reaction_emoji_strategy = (
            _strategy if _strategy in ("随机", "顺序循环") else "随机"
        )
        self.reaction_emoji_type = "1"
        self.max_video_size_mb = int(
            self._get_config_value("general_settings.max_video_size_mb", 200)
        )
        self.napcat_media_share_path = str(
            self._get_config_value("general_settings.napcat_media_share_path", "")
        ).strip().strip('"').strip("'")
        self.napcat_media_container_path = str(
            self._get_config_value("general_settings.napcat_media_container_path", "")
        ).strip().strip('"').strip("'")
        self.merge_send_as_sender = bool(
            self._get_config_value("general_settings.merge_send_as_sender", False)
        )
        _mode = str(
            self._get_config_value("general_settings.error_notify_mode", "静默")
        ).strip()
        self.error_notify_mode = _mode if _mode in ("静默", "脱敏", "报错") else "静默"

        # 群过滤
        _gf_mode = str(self._get_config_value("group_filter.mode", "黑名单")).strip()
        self.group_filter_mode = (
            _gf_mode if _gf_mode in ("黑名单", "白名单") else "黑名单"
        )
        _gf_list = self._get_config_value("group_filter.group_list", [])
        self.group_filter_list = [
            str(item).strip() for item in _gf_list if str(item).strip()
        ] if isinstance(_gf_list, list) else []

        _pf_mode = str(self._get_config_value("private_filter.mode", "黑名单")).strip()
        self.private_filter_mode = (
            _pf_mode if _pf_mode in ("黑名单", "白名单") else "黑名单"
        )
        _pf_list = self._get_config_value("private_filter.user_list", [])
        self.private_filter_list = [
            str(item).strip() for item in _pf_list if str(item).strip()
        ] if isinstance(_pf_list, list) else []

        alias = self._normalize_quality_alias(self.quality_label)
        if alias == "HDR":
            self.allow_hdr = True
        if alias == "DOLBY":
            self.allow_dolby = True

        self.quality_enum_name, self.video_quality = self._resolve_quality(alias)
        self.codecs_enum_name, self.video_codecs = self._resolve_codecs(
            self.codecs_label
        )

        # 重新实例化小红书卡片渲染器
        self.xhs_renderer = XiaohongshuCardRenderer(self.default_primary_font)
