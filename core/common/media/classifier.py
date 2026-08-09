# core/common/media/classifier.py
from pathlib import Path
import cv2
import numpy as np
from astrbot.api import logger


def cv2_imread_safe(image_path, flags=cv2.IMREAD_COLOR):
    """安全读取带中文/特殊字符路径的图片 (Windows 兼容)"""
    try:
        data = np.fromfile(str(Path(image_path).resolve()), dtype=np.uint8)
        return cv2.imdecode(data, flags)
    except Exception:
        return None


class AnimePhotoClassifier:
    """二次元/照片分类器 (纯 CV 物理特征检测)"""

    def __init__(self):
        pass

    def _extract_cv_features(self, img_bgr) -> tuple[bool, float, float, float]:
        """提取二次元/插画物理视觉特征 (饱和度、平坦度、边缘比)"""
        try:
            hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
            saturation_mean = float(np.mean(hsv[:, :, 1]))

            gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(gray, 50, 150)
            edge_ratio = float(np.count_nonzero(edges) / edges.size)
            non_edge_mask = (edges == 0)
            flatness_std = float(np.std(gray[non_edge_mask])) if np.any(non_edge_mask) else 50.0

            # 动漫插画显著特征：较高饱和度 OR 明显线稿边缘 OR 平坦填色
            is_cv_anime = (flatness_std < 65.0) or (edge_ratio > 0.02) or (saturation_mean > 65.0)
            return is_cv_anime, saturation_mean, flatness_std, edge_ratio
        except Exception:
            return False, 0.0, 0.0, 0.0

    def predict_is_anime(self, image_path) -> bool:
        """预测图像是否为二次元/插画 (纯 CV 特征判定)"""
        img = cv2_imread_safe(image_path, cv2.IMREAD_COLOR)
        if img is None:
            return False

        is_anime, saturation, flatness, edge_ratio = self._extract_cv_features(img)

        logger.info(
            "🧠 [CV分类器] 判定结果: %s | (饱和度: %.1f, 平坦度: %.1f, 边缘比: %.4f)",
            "【二次元】" if is_anime else "【照片】",
            saturation,
            flatness,
            edge_ratio,
        )
        return is_anime


_classifier_instance = AnimePhotoClassifier()


def get_classifier():
    return _classifier_instance