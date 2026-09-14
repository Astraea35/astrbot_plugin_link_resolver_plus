# core/common/media/upscaler.py
import asyncio
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
from PIL import Image as PILImage

from astrbot.api import logger

from .classifier import cv2_imread_safe, get_classifier
from .process import monitor_process_percentage


@dataclass(frozen=True)
class UpscaleModel:
    """A model's execution backend and compatibility fallback."""

    backend: str
    category: str
    fallback: str | None = None
    native_scale: int = 4


# Keep the legacy Upscayl names intact so existing persisted settings remain valid.
UPSCAYL_MODEL_NAME_MAP = {
    "自动 (CV特征识别)": "auto",
    "数字艺术 (digital-art-4x)": "digital-art-4x",
    "二次元 (digital-art-4x)": "digital-art-4x",
    "高保真 (high-fidelity-4x)": "high-fidelity-4x",
    "Remacri (remacri-4x)": "remacri-4x",
    "超混合平衡 (ultramix-balanced-4x)": "ultramix-balanced-4x",
    "超锐化 (ultrasharp-4x)": "ultrasharp-4x",
    "轻量 (upscayl-lite-4x)": "upscayl-lite-4x",
    "标准 (upscayl-standard-4x)": "upscayl-standard-4x",
    "极速动漫 (realesr-animevideov3)": "realesr-animevideov3",
    "照片自然 2x (liveaction-v1-span-2x)": "liveaction-v1-span-2x",
    "照片自然 4x (nomos8k-span-otf-medium)": "nomos8k-span-otf-medium",
    "动漫自然 2x (hfa2k-span-2x)": "hfa2k-span-2x",
    "最高质量 (real-hat-gan-srx4)": "real-hat-gan-srx4",
    "最高质量锐利 (real-hat-gan-srx4-sharper)": "real-hat-gan-srx4-sharper",
    "动漫高质量 (animejanai-v3.1-balanced)": "animejanai-v3.1-balanced",
    "动漫高质量锐利 (animejanai-v3.1-sharp)": "animejanai-v3.1-sharp",
    # Compatibility aliases emitted by pre-release configuration drafts.
    "自然照片 2x (liveaction-v1-span-2x)": "liveaction-v1-span-2x",
    "自然照片 4x (nomos8k-span-otf-medium)": "nomos8k-span-otf-medium",
    "锐利质量 (real-hat-gan-srx4-sharper)": "real-hat-gan-srx4-sharper",
}


MODEL_REGISTRY: dict[str, UpscaleModel] = {
    "digital-art-4x": UpscaleModel("upscayl", "anime"),
    "high-fidelity-4x": UpscaleModel("upscayl", "photo", "remacri-4x"),
    "remacri-4x": UpscaleModel("upscayl", "photo"),
    "ultramix-balanced-4x": UpscaleModel("upscayl", "photo", "remacri-4x"),
    # Retained for manual selection, never selected by automatic routing.
    "ultrasharp-4x": UpscaleModel("upscayl", "photo", "remacri-4x"),
    "upscayl-lite-4x": UpscaleModel("upscayl", "photo", "remacri-4x"),
    "upscayl-standard-4x": UpscaleModel("upscayl", "photo", "remacri-4x"),
    # Real-ESRGAN Compact is compatible with the existing Upscayl NCNN runner.
    "realesr-animevideov3": UpscaleModel("upscayl", "anime", "digital-art-4x"),
    "liveaction-v1-span-2x": UpscaleModel("span", "photo", "remacri-4x", 2),
    "nomos8k-span-otf-medium": UpscaleModel("span", "photo", "remacri-4x", 4),
    "hfa2k-span-2x": UpscaleModel("span", "anime", "digital-art-4x", 2),
    "real-hat-gan-srx4": UpscaleModel("hat", "photo", "remacri-4x", 4),
    "real-hat-gan-srx4-sharper": UpscaleModel("hat", "photo", "remacri-4x", 4),
    # AnimeJaNai uses an externally configured ONNX/TensorRT/DirectML runner.
    "animejanai-v3.1-balanced": UpscaleModel("animejanai", "anime", "digital-art-4x", 2),
    "animejanai-v3.1-sharp": UpscaleModel("animejanai", "anime", "digital-art-4x", 2),
}

AUTO_ANIME_MODEL = "realesr-animevideov3"
AUTO_PHOTO_MODEL = "nomos8k-span-otf-medium"
CACHE_MAX_AGE_SECONDS = 7 * 24 * 3600


class UpscaylUpscaler:
    """Run registered NCNN upscalers with cache-safe automatic routing."""

    def __init__(self, plugin_instance):
        self.plugin = plugin_instance

    @staticmethod
    def _resolve_model(model_setting: str | None) -> str:
        model = UPSCAYL_MODEL_NAME_MAP.get(model_setting or "", model_setting or "digital-art-4x")
        return str(model).strip() or "digital-art-4x"

    @staticmethod
    def _model_spec(model_name: str) -> UpscaleModel:
        # Unknown legacy/custom models retain the old Upscayl execution path.
        return MODEL_REGISTRY.get(model_name, UpscaleModel("upscayl", "photo", "remacri-4x"))

    def _select_automatic_scale_for_long_edge(self, long_edge: int) -> int | None:
        max_long_edge = max(1, int(getattr(self.plugin, "auto_upscale_max_long_edge", 3840)))
        if long_edge >= max_long_edge:
            return None
        return 4 if long_edge <= max_long_edge // 2 else 2

    def _select_automatic_scale(self, input_path: Path) -> int | None:
        """Choose an automatic scale without allowing output to exceed the limit."""
        try:
            with PILImage.open(input_path) as image:
                long_edge = max(image.width, image.height)
        except Exception as exc:
            logger.warning("Unable to inspect image dimensions for automatic scaling: %s", exc)
            return 2

        return self._select_automatic_scale_for_long_edge(long_edge)

    @staticmethod
    def _safe_cache_component(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", value)

    def _cache_path(self, input_path: Path, model_name: str, scale: int, enable_taa: bool) -> Path:
        spec = self._model_spec(model_name)
        cache_key = f"{spec.backend}_{self._safe_cache_component(model_name)}_{scale}x_taa{int(enable_taa)}"
        return input_path.parent / f"{input_path.stem}_upscaled_{cache_key}.png"

    @staticmethod
    def _is_fresh_cache(path: Path) -> bool:
        return path.exists() and time.time() - path.stat().st_mtime < CACHE_MAX_AGE_SECONDS

    async def _cap_automatic_output(self, output_path: Path) -> None:
        """Downsize automatic output after inference when it exceeds the configured cap."""
        max_long_edge = max(1, int(getattr(self.plugin, "auto_upscale_max_long_edge", 3840)))

        def _resize() -> None:
            with PILImage.open(output_path) as image:
                long_edge = max(image.width, image.height)
                if long_edge <= max_long_edge:
                    return
                ratio = max_long_edge / long_edge
                size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
                image.resize(size, PILImage.Resampling.LANCZOS).save(output_path, "PNG")

        try:
            await asyncio.to_thread(_resize)
        except Exception as exc:
            logger.warning("Unable to enforce automatic output size limit for %s: %s", output_path.name, exc)

    async def check_is_low_quality(
        self,
        image_path: Path,
        threshold: int = None,
        model_setting: str = "自动 (CV特征识别)",
    ) -> tuple[bool, str, str]:
        """Determine whether an image needs enhancement and route to an available model."""
        try:
            local_threshold = threshold or getattr(self.plugin, "low_quality_threshold", 2160)

            def _get_dims():
                with PILImage.open(image_path) as img:
                    return img.width, img.height

            width, height = await asyncio.to_thread(_get_dims)
            is_low_res = width < local_threshold or height < local_threshold

            is_auto = model_setting in ("自动 (CV特征识别)", "auto") or "Auto" in model_setting
            if is_auto:
                classifier = get_classifier()
                is_anime = await asyncio.to_thread(classifier.predict_is_anime, image_path)
                auto_scale = self._select_automatic_scale_for_long_edge(max(width, height))
                if is_anime:
                    img_type_label = "二次元(CV)"
                    dynamic_blur_threshold = 35.0
                    recommended_model = "hfa2k-span-2x" if auto_scale == 2 else AUTO_ANIME_MODEL
                else:
                    img_type_label = "照片(CV)"
                    dynamic_blur_threshold = 80.0
                    recommended_model = "liveaction-v1-span-2x" if auto_scale == 2 else AUTO_PHOTO_MODEL
            else:
                recommended_model = self._resolve_model(model_setting)
                img_type_label = f"手动指定({recommended_model})"
                dynamic_blur_threshold = 35.0 if self._model_spec(recommended_model).category == "anime" else 80.0

            logger.info("📏 [尺寸检测] 当前图片尺寸: %dx%d | 设定判定阈值: %dpx", width, height, local_threshold)
            if is_low_res:
                logger.info("🔳 [%s] 尺寸 (%dx%d) < 阈值 (%dpx)，触发 AI 升图", img_type_label, width, height, local_threshold)
                return True, img_type_label, recommended_model

            img_gray = cv2_imread_safe(image_path, cv2.IMREAD_GRAYSCALE)
            if img_gray is not None:
                blur_score = cv2.Laplacian(img_gray, cv2.CV_64F).var()
                if blur_score < dynamic_blur_threshold:
                    logger.info("🌫️ [%s] 尺寸合格但画面模糊 (得分 %.2f < %.1f)，触发 AI 修复", img_type_label, blur_score, dynamic_blur_threshold)
                    return True, img_type_label, recommended_model
                logger.info("✨ [%s] 图片清晰，跳过 AI 升图", img_type_label)
            return False, img_type_label, recommended_model
        except Exception as exc:
            logger.warning("⚠️ 判定图片质量发生异常: %s", exc)
            return False, "通用", "remacri-4x"

    def _backend_paths(self, backend: str) -> tuple[str, str]:
        if backend == "span":
            return (
                str(getattr(self.plugin, "span_bin_path", "") or ""),
                str(getattr(self.plugin, "span_models_path", "") or ""),
            )
        if backend == "animejanai":
            return (
                str(getattr(self.plugin, "animejanai_bin_path", "") or ""),
                str(getattr(self.plugin, "animejanai_models_path", "") or ""),
            )
        if backend == "hat":
            return (
                str(getattr(self.plugin, "hat_bin_path", "") or ""),
                str(getattr(self.plugin, "hat_models_path", "") or ""),
            )
        return (
            str(getattr(self.plugin, "upscayl_bin_path", "C:/Program Files/Upscayl/resources/bin/upscayl-bin.exe")),
            str(getattr(self.plugin, "upscayl_models_path", "C:/Program Files/Upscayl/resources/models")),
        )

    def _build_command(
        self,
        binary: str,
        models_dir: str,
        spec: UpscaleModel,
        input_path: Path,
        output_path: Path,
        model_name: str,
        scale: int,
        enable_taa: bool,
    ) -> list[str]:
        if spec.backend in {"animejanai", "hat"}:
            template = str(getattr(self.plugin, f"{spec.backend}_command_template", "") or "").strip()
            if template:
                values = {
                    "input": str(input_path.resolve()),
                    "output": str(output_path.resolve()),
                    "model": model_name,
                    "scale": str(scale),
                    "models": models_dir,
                }
                try:
                    return [part.format(**values).strip('"') for part in shlex.split(template, posix=False)]
                except (KeyError, ValueError) as exc:
                    raise ValueError(f"{spec.backend} command template is invalid") from exc

        cmd = [binary, "-i", str(input_path.resolve()), "-o", str(output_path.resolve()), "-n", model_name, "-s", str(scale)]
        if spec.backend == "upscayl" and enable_taa:
            cmd.append("-x")
        if models_dir and Path(models_dir).exists():
            cmd.extend(["-m", models_dir])
        return cmd

    async def _run_model(
        self,
        input_path: Path,
        output_path: Path,
        model_name: str,
        scale: int,
        enable_taa: bool,
        stage: str,
    ) -> bool:
        spec = self._model_spec(model_name)
        binary, models_dir = self._backend_paths(spec.backend)
        if not binary or not Path(binary).exists():
            logger.error("❌ [%s 路径错误] 找不到可执行文件: %s", spec.backend.upper(), binary or "未配置")
            return False

        try:
            cmd = self._build_command(binary, models_dir, spec, input_path, output_path, model_name, scale, enable_taa)
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await monitor_process_percentage(process, stage, self.plugin)
            return output_path.exists()
        except Exception as exc:
            logger.error("❌ 调用 %s 模型 %s 异常: %s", spec.backend.upper(), model_name, exc)
            return False

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
        """Execute one registered model, with a compatible model fallback on failure."""
        model_name = self._resolve_model(
            override_model or getattr(self.plugin, "upscayl_model_name", "digital-art-4x")
        )
        automatic_scale = scale is None
        if automatic_scale:
            selected_scale = self._select_automatic_scale(input_path)
            if selected_scale is None:
                logger.info("📐 图片已达到自动升图长边上限，跳过: %s", input_path.name)
                return input_path
        else:
            selected_scale = max(1, int(scale))

        spec = self._model_spec(model_name)
        if spec.backend in {"span", "animejanai", "hat"} and selected_scale != spec.native_scale:
            logger.info(
                "📐 %s 模型 %s 固定使用原生 %dx 倍率",
                spec.backend.upper(),
                model_name,
                spec.native_scale,
            )
            selected_scale = spec.native_scale

        enable_taa = enable_taa if enable_taa is not None else getattr(self.plugin, "upscayl_enable_taa", True)
        # Double processing magnifies artifacts and bypasses the automatic output cap.
        # Retain it only for explicitly requested legacy Upscayl manual operations.
        use_double_pass = bool(double_pass) and not automatic_scale and spec.backend == "upscayl"
        out_path = self._cache_path(input_path, model_name, selected_scale, bool(enable_taa))
        if self._is_fresh_cache(out_path):
            logger.info("💾 [Cache Hit] 命中 7 天内的 AI 升图缓存: %s", out_path.name)
            return out_path

        pass1_path = out_path.with_name(f"{out_path.stem}_pass1.png")
        try:
            if use_double_pass:
                first_pass_succeeded = await self._run_model(
                    input_path, pass1_path, model_name, selected_scale, bool(enable_taa), "🎨 AI 升图 (第一阶段)"
                )
                if first_pass_succeeded and await self._run_model(
                    pass1_path, out_path, model_name, selected_scale, bool(enable_taa), "🎨 AI 升图 (第二阶段)"
                ):
                    if automatic_scale:
                        await self._cap_automatic_output(out_path)
                    return out_path
            elif await self._run_model(input_path, out_path, model_name, selected_scale, bool(enable_taa), "🎨 AI 升图中"):
                if automatic_scale:
                    await self._cap_automatic_output(out_path)
                return out_path

            fallback = self._model_spec(model_name).fallback
            if fallback and fallback != model_name:
                fallback_path = self._cache_path(input_path, fallback, selected_scale, bool(enable_taa))
                if self._is_fresh_cache(fallback_path):
                    return fallback_path
                logger.warning("⚠️ 模型 %s 执行失败，回退到 %s", model_name, fallback)
                if await self._run_model(input_path, fallback_path, fallback, selected_scale, bool(enable_taa), "🎨 AI 升图回退中"):
                    if automatic_scale:
                        await self._cap_automatic_output(fallback_path)
                    return fallback_path
            return input_path
        finally:
            pass1_path.unlink(missing_ok=True)
