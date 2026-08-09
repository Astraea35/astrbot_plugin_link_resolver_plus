# region 导入
"""抖音卡片渲染器

使用通用渲染器 + 抖音主题实现。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PIL import Image

from ..common.card_renderer import (
    CardData,
    UniversalCardRenderer,
    find_default_font,
    get_theme_for_platform,
)

# endregion


# region 渲染器
class DouyinCardRenderer:
    """抖音卡片渲染器

    特性:
    - 圆角卡片设计
    - 抖音红色主题
    - 点赞/评论统计徽章
    - 自动明暗主题切换（19:00-08:00 暗色）
    """

    def __init__(self, font_path: Path | None = None):
        """初始化渲染器

        Args:
            font_path: 自定义字体路径，None 则自动查找
        """
        self.font_path = font_path or find_default_font()

        if not self.font_path:
            from astrbot.api import logger

            logger.warning("⚠️ 抖音渲染器未找到中文字体，预览图可能出现乱码")

    def render(
        self,
        *,
        title: str | None,
        author: str | None,
        text: str | None = None,
        cover_path: Path | None = None,
        image_paths: Iterable[Path] | None = None,
        is_video: bool = True,
        likes: str | None = None,
        comments: str | None = None,
    ) -> Image.Image:
        """渲染抖音卡片

        Args:
            title: 视频/图集标题
            author: 作者名称
            text: 描述文字
            cover_path: 封面图路径
            image_paths: 图集图片路径列表
            is_video: 是否为视频（默认 True）
            likes: 点赞数（如 "12.3万"）
            comments: 评论数（如 "5678"）

        Returns:
            渲染完成的 PIL Image
        """
        # 获取当前时间对应的主题
        theme = get_theme_for_platform("douyin")

        # 创建通用渲染器
        renderer = UniversalCardRenderer(theme, self.font_path)

        # 构建统计徽章
        stats: dict[str, str] = {}
        if likes:
            stats["点赞"] = likes
        if comments:
            stats["评论"] = comments

        # 构建数据
        data = CardData(
            title=title,
            author=author,
            text=text,
            image_paths=list(image_paths) if image_paths else [],
            cover_path=cover_path,
            is_video=is_video,
            stats=stats if stats else None,
        )

        return renderer.render(data)


# endregion


# region 导出
__all__ = ["DouyinCardRenderer"]
# endregion
