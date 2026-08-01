# core/common/media/upscaler.py
import asyncio
import time
from pathlib import Path
from PIL import Image as PILImage
import cv2

from astrbot.api import logger
from .classifier import cv2_imread_safe, get_classifier
from .process import monitor_process_percentage

UPSCAYL_MODEL_NAME_MAP = {
    "自动 (CV特征识别)": "auto",
    "数字艺术 (digital-art-4x)": "digital-art-4x",
    "高保真 (high-fidelity-4x)": "high-fidelity-4x",
    "Remacri (remacri-4x)": "remacri-4x",
    "超混合平衡 (ultramix-balanced-4x)": "ultramix-balanced-4x",
    "超锐化 (ultrasharp-4x)": "ultrasharp-4x",
    "轻量 (upscayl-lite-4x)": "upscayl-lite-4x",
    "标准 (upscayl-standard-4x)": "upscayl-standard-4x",
}


class UpscaylUpscaler:
    """异步调用 Upscayl 模型升图，集成双门槛检测与 7 天缓存"""

    def __init__(self, plugin_instance):
        self.plugin = plugin_instance

    async def check_is_low_quality(
        self,
        image_path: Path,
        threshold: int = None,
        model_setting: str = "自动 (CV特征识别)"
    ) -> tuple[bool, str, str]:
        """智能质量检测 (尺寸 + 模糊度)，支持自动匹配或强行指定模型

        Returns:
            (need_upscale, img_type_label, recommended_model)
        """
        try:
            local_threshold = threshold or getattr(self.plugin, "xhs_low_quality_threshold", 1080)

            # 1. 物理分辨率检测
            def _get_dims():
                with PILImage.open(image_path) as img:
                    return img.width, img.height

            width, height = await asyncio.to_thread(_get_dims)
            is_low_res = (width < local_threshold or height < local_threshold)

            # 2. 模型分配与判定参数准备
            if model_setting in ("自动 (CV特征识别)", "auto") or "Auto" in model_setting:
                classifier = get_classifier()
                is_anime = await asyncio.to_thread(classifier.predict_is_anime, image_path)
                if is_anime:
                    img_type_label = "二次元(CV)"
                    dynamic_blur_threshold = 35.0
                    recommended_model = "digital-art-4x"
                else:
                    img_type_label = "照片(CV)"
                    dynamic_blur_threshold = 80.0
                    recommended_model = "ultrasharp-4x"
            else:
                raw_model = UPSCAYL_MODEL_NAME_MAP.get(model_setting, model_setting)
                recommended_model = raw_model
                img_type_label = f"手动指定({raw_model})"
                if "digital-art" in raw_model:
                    dynamic_blur_threshold = 35.0
                else:
                    dynamic_blur_threshold = 80.0

            # 打印明确检测日志
            logger.info("📏 [尺寸检测] 当前图片尺寸: %dx%d | 设定判定阈值: %dpx", width, height, local_threshold)

            # 3. 尺寸硬性拦截：长或宽小于阈值时强制触发升图
            if is_low_res:
                logger.info("🔳 [%s] 尺寸 (%dx%d) < 阈值 (%dpx)，像素不达标，强制触发 AI 升图", img_type_label, width, height, local_threshold)
                return True, img_type_label, recommended_model

            # 4. 尺寸达标后，检测拉普拉斯 (Laplacian) 画面模糊度
            img_gray = cv2_imread_safe(image_path, cv2.IMREAD_GRAYSCALE)
            if img_gray is not None:
                blur_score = cv2.Laplacian(img_gray, cv2.CV_64F).var()
                if blur_score < dynamic_blur_threshold:
                    logger.info("🌫️ [%s] 尺寸合格但画面模糊 (得分 %.2f < 动态阈值 %.1f)，触发 AI 修复",
                                img_type_label, blur_score, dynamic_blur_threshold)
                    return True, img_type_label, recommended_model
                else:
                    logger.info("✨ [%s] 本身即为高清原图 (尺寸 %dx%d, 模糊得分 %.2f)，跳过 AI 升图",
                                img_type_label, width, height, blur_score)
                    return False, img_type_label, recommended_model

            return False, img_type_label, recommended_model
        except Exception as e:
            logger.warning("⚠️ 判定图片质量发生异常: %s", str(e))
            return False, "通用", "ultrasharp-4x"

    async def upscale_image(
        self,
        input_path: Path,
        request_id: str,
        override_model: str = None,
        *,
        scale: int | None = None,
        enable_taa: bool | None = None,
        double_pass: bool | None = None,
    ) -> Path:
        """异步执行 Upscayl 升图"""
        out_path = input_path.parent / f"{input_path.stem}_upscayl.png"
        if out_path.exists() and (time.time() - out_path.stat().st_mtime < 7 * 24 * 3600):
            logger.info("💾 [Cache Hit] 命中 7 天内的 AI 升图缓存: %s", out_path.name)
            return out_path

        upscayl_bin = getattr(self.plugin, "upscayl_bin_path", "C:/Program Files/Upscayl/resources/bin/upscayl-bin.exe")
        models_dir = getattr(self.plugin, "upscayl_models_path", "C:/Program Files/Upscayl/resources/models")
        scale = str(scale if scale is not None else getattr(self.plugin, "upscayl_scale", 2))
        enable_taa = enable_taa if enable_taa is not None else getattr(self.plugin, "upscayl_enable_taa", True)
        double_pass = double_pass if double_pass is not None else getattr(self.plugin, "upscayl_double_pass", True)

        # 核心拦截防御：先校验该路径在硬盘上是否存在！
        if not upscayl_bin or not Path(upscayl_bin).exists():
            logger.error("❌ [Upscayl 路径错误] 找不到可执行文件！程序当前调用的路径为: '%s'。请检查面板中的路径配置是否有效！", upscayl_bin)
            return input_path

        model_name = override_model or UPSCAYL_MODEL_NAME_MAP.get(
            getattr(self.plugin, "upscayl_model_name", "digital-art-4x"), "digital-art-4x"
        )

        pass1_path = input_path.parent / f"{input_path.stem}_up1.png"

        def _build_cmd(inp, outp):
            cmd = [upscayl_bin, "-i", str(inp.resolve()), "-o", str(outp.resolve()), "-n", model_name, "-s", scale]
            if enable_taa:
                cmd.append("-x")
            if models_dir and Path(models_dir).exists():
                cmd.extend(["-m", models_dir])
            return cmd

        try:
            if not double_pass:
                cmd = _build_cmd(input_path, out_path)
                proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await monitor_process_percentage(proc, "🎨 AI 升图中", self.plugin)
                return out_path if out_path.exists() else input_path
            else:
                cmd1 = _build_cmd(input_path, pass1_path)
                proc1 = await asyncio.create_subprocess_exec(*cmd1, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await monitor_process_percentage(proc1, "🎨 AI 升图 (第一阶段)", self.plugin)
                if not pass1_path.exists():
                    return input_path

                cmd2 = _build_cmd(pass1_path, out_path)
                proc2 = await asyncio.create_subprocess_exec(*cmd2, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await monitor_process_percentage(proc2, "🎨 AI 升图 (第二阶段)", self.plugin)
                pass1_path.unlink(missing_ok=True)
                return out_path if out_path.exists() else input_path
        except Exception as e:
            logger.error("❌ 调用 Upscayl 升图异常: %s", str(e))
            pass1_path.unlink(missing_ok=True)
            return input_path
