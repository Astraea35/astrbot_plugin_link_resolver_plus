# core/common/media/classifier.py
from pathlib import Path
import cv2
import numpy as np
from astrbot.api import logger

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MODEL_PATH = PLUGIN_ROOT / "anime_classifier.onnx"


def cv2_imread_safe(image_path, flags=cv2.IMREAD_COLOR):
    """安全读取带中文/特殊字符路径的图片 (Windows 兼容)"""
    try:
        data = np.fromfile(str(Path(image_path).resolve()), dtype=np.uint8)
        return cv2.imdecode(data, flags)
    except Exception:
        return None


class AnimePhotoClassifier:
    """二次元/照片分类器 (ONNX 算法修正 + CV 特征融合双重校验)"""

    def __init__(self, model_path=DEFAULT_MODEL_PATH):
        self.session = None
        self.input_name = None
        self.model_path = Path(model_path)

        if not self.model_path.exists():
            logger.warning("⚠️ 未在插件根目录找到 ONNX 模型: %s，将使用 CV 规则分类", self.model_path)
            return

        try:
            import onnxruntime as ort
            providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
            self.session = ort.InferenceSession(str(self.model_path.resolve()), providers=providers)
            self.input_name = self.session.get_inputs()[0].name
            used_provider = self.session.get_providers()[0]
            logger.info("🧠 成功挂载 ONNX 图像分类模型: %s (Provider: %s)", self.model_path.name, used_provider)
        except Exception as e:
            logger.error("❌ 加载 ONNX 模型失败，回退至 CV 规则: %s", str(e))

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
        """预测图像是否为二次元/插画 (智能双重校验)"""
        img = cv2_imread_safe(image_path, cv2.IMREAD_COLOR)
        if img is None:
            return False

        # 1. 提取 CV 特征做兜底依据
        is_cv_anime, saturation, flatness, edge_ratio = self._extract_cv_features(img)

        prob_anime = 0.0
        onnx_decision = None

        if self.session and self.input_name:
            try:
                # 2. 🚀 关键修正：采用标准的 [0.0, 1.0] 归一化，去掉引发负数偏移的 ImageNet 均值/方差
                img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                img_resized = cv2.resize(img_rgb, (224, 224)).astype(np.float32) / 255.0

                # NCHW 维度变换
                img_tensor = np.transpose(img_resized, (2, 0, 1))[np.newaxis, ...]
                outputs = self.session.run(None, {self.input_name: img_tensor})

                out_arr = np.squeeze(outputs[0])

                if out_arr.size >= 2:
                    exp_arr = np.exp(out_arr - np.max(out_arr))
                    probs = exp_arr / np.sum(exp_arr)
                    # 🚀 尝试取两个概率的最大倾向值，并结合 Raw Logit 调试
                    prob_anime = float(max(probs[0], probs[1])) if (probs[0] > probs[1] or probs[1] > probs[0]) else float(probs[0])
                    onnx_decision = (probs[0] > 0.5) or (probs[1] > 0.5)
                elif out_arr.size == 1:
                    prob_anime = 1.0 / (1.0 + np.exp(-float(out_arr)))
                    onnx_decision = prob_anime > 0.5
            except Exception as e:
                logger.warning("⚠️ ONNX 推理过程异常: %s", str(e))

        # 3. 🚀 智能融合决断逻辑 (Double-Check Safety Net)
        # 如果 ONNX 推理成功且判定为二次元，直接认可
        if onnx_decision is True and prob_anime > 0.6:
            final_decision = True
            reason = "ONNX 高置信度"
        # 如果 ONNX 判定不明确/错判，但 CV 物理特征强烈指向动漫/插画（高饱和度/线稿/色块平坦）
        elif is_cv_anime:
            final_decision = True
            reason = "CV 视觉特征修正 (高饱和/平滑线稿)"
        else:
            final_decision = False
            reason = "判定为真实照片"

        logger.info(
            "🧠 [分类器] 最终判定: %s | 依据: %s | (CV饱和度: %.1f, 平坦度: %.1f, 边缘比: %.4f)",
            "【二次元】" if final_decision else "【照片】",
            reason,
            saturation,
            flatness,
            edge_ratio,
        )
        return final_decision


_classifier_instance = AnimePhotoClassifier()


def get_classifier():
    return _classifier_instance