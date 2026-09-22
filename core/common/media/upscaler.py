# core/common/media/upscaler.py
import asyncio
import re
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
from PIL import Image as PILImage

from astrbot.api import logger

from ..paths import get_persistent_animejanai_models_path
from .classifier import ClassificationResult, cv2_imread_safe, get_classifier
from .process import monitor_process_percentage
from .remote_client import RemoteWorkerClient


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
    "动漫高质量 (animejanai-v3.1-balanced)": "animejanai-v3.1-balanced",
    "动漫高质量锐利 (animejanai-v3.1-sharp)": "animejanai-v3.1-sharp",
    # Compatibility aliases emitted by pre-release configuration drafts.
    "自然照片 2x (liveaction-v1-span-2x)": "liveaction-v1-span-2x",
    "自然照片 4x (nomos8k-span-otf-medium)": "nomos8k-span-otf-medium",
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
    # AnimeJaNai uses an externally configured ONNX/TensorRT/DirectML runner.
    "animejanai-v3.1-balanced": UpscaleModel("animejanai", "anime", "digital-art-4x", 2),
    "animejanai-v3.1-sharp": UpscaleModel("animejanai", "anime", "digital-art-4x", 2),
}

AUTO_ANIME_MODEL = "animejanai-v3.1-balanced"
AUTO_PHOTO_MODEL = "nomos8k-span-otf-medium"
CACHE_MAX_AGE_SECONDS = 7 * 24 * 3600
ANIMEJANAI_TILE_SIZE = 768
ANIMEJANAI_TILE_OVERLAP = 64


class UpscaylUpscaler:
    """Run registered NCNN upscalers with cache-safe automatic routing."""

    def __init__(self, plugin_instance):
        self.plugin = plugin_instance
        self.remote_client = getattr(plugin_instance, "remote_worker", None) or RemoteWorkerClient(plugin_instance)

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

    async def classify_image(
        self,
        image_path: Path,
        classification_hint: ClassificationResult | None = None,
    ) -> ClassificationResult:
        if classification_hint is not None:
            return classification_hint

        classifier = getattr(self.plugin, "image_classifier", None) or get_classifier()
        classify = getattr(classifier, "classify", None)
        if classify is not None:
            result = classify(image_path)
            if asyncio.iscoroutine(result):
                result = await result
            if isinstance(result, ClassificationResult):
                return result

        predict_is_anime = getattr(classifier, "predict_is_anime", None)
        is_anime = bool(
            await asyncio.to_thread(predict_is_anime, image_path)
            if predict_is_anime is not None
            else False
        )
        return ClassificationResult(
            "anime" if is_anime else "photo",
            0.5,
            "legacy_cv",
            {},
        )

    @staticmethod
    def _classification_route(
        result: ClassificationResult, auto_scale: int | None
    ) -> tuple[str, float, str | None]:
        source_label = "CLIP" if result.source == "clip" else "规则V2"
        confidence = round(result.confidence * 100)
        if result.kind == "anime":
            return f"二次元({source_label} {confidence}%)", 35.0, AUTO_ANIME_MODEL
        if result.kind == "photo":
            model = "liveaction-v1-span-2x" if auto_scale == 2 else AUTO_PHOTO_MODEL
            return f"照片({source_label} {confidence}%)", 80.0, model
        if result.kind == "text_ui":
            return f"文字/UI({source_label} {confidence}%)", 0.0, None
        return f"不确定({source_label} {confidence}%)", 55.0, "remacri-4x"

    @staticmethod
    def _safe_cache_component(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", value)

    def _cache_path(
        self,
        input_path: Path,
        model_name: str,
        scale: int,
        enable_taa: bool,
        *,
        passes: int = 1,
        target_long_edge: int | None = None,
    ) -> Path:
        spec = self._model_spec(model_name)
        model_key = self._safe_cache_component(model_name)
        if spec.backend == "animejanai" and target_long_edge is not None:
            cache_key = (
                f"{spec.backend}_{model_key}_native{scale}x_p{max(1, int(passes))}"
                f"_min{max(1, int(target_long_edge))}_taa{int(enable_taa)}"
            )
        else:
            cache_key = f"{spec.backend}_{model_key}_{scale}x_taa{int(enable_taa)}"
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

    def _animejanai_automatic_limits(self) -> tuple[int, int]:
        """Return the minimum automatic output edge and the native-pass safety limit."""
        target_long_edge = max(
            512,
            min(16384, int(getattr(self.plugin, "animejanai_auto_min_long_edge", 3840))),
        )
        max_passes = max(
            1,
            min(6, int(getattr(self.plugin, "animejanai_auto_max_passes", 6))),
        )
        return target_long_edge, max_passes

    def _animejanai_automatic_plan(self, input_path: Path) -> tuple[int, int]:
        """Return native 2x pass count and minimum output long edge for AnimeJaNai."""
        target_long_edge, max_passes = self._animejanai_automatic_limits()
        try:
            with PILImage.open(input_path) as image:
                current_long_edge = max(image.width, image.height)
        except Exception as exc:
            logger.warning("Unable to inspect AnimeJaNai input dimensions for %s: %s", input_path.name, exc)
            return 1, target_long_edge

        passes = 0
        projected_long_edge = current_long_edge
        while projected_long_edge < target_long_edge and passes < max_passes:
            projected_long_edge *= 2
            passes += 1

        if passes == max_passes and projected_long_edge < target_long_edge:
            logger.warning(
                "⚠️ AnimeJaNai 输入 %s 在安全上限 %d 轮后仅能达到 %dpx，低于目标 %dpx",
                input_path.name,
                max_passes,
                projected_long_edge,
                target_long_edge,
            )
        return passes, target_long_edge

    async def _run_animejanai_automatic_passes(
        self,
        input_path: Path,
        output_path: Path,
        model_name: str,
        enable_taa: bool,
        passes: int,
    ) -> bool:
        """Run AnimeJaNai's native 2x model repeatedly without post-resizing."""
        intermediate_paths: list[Path] = []
        current_path = input_path
        try:
            for pass_number in range(1, passes + 1):
                is_final_pass = pass_number == passes
                next_path = (
                    output_path
                    if is_final_pass
                    else output_path.with_name(f"{output_path.stem}_native_pass{pass_number}.png")
                )
                next_path.unlink(missing_ok=True)
                if not is_final_pass:
                    intermediate_paths.append(next_path)

                if not await self._run_model(
                    current_path,
                    next_path,
                    model_name,
                    2,
                    enable_taa,
                    f"🎨 AnimeJaNai 原生 2x ({pass_number}/{passes})",
                ):
                    return False
                current_path = next_path
            return True
        finally:
            for intermediate_path in intermediate_paths:
                intermediate_path.unlink(missing_ok=True)

    async def check_is_low_quality(
        self,
        image_path: Path,
        threshold: int = None,
        model_setting: str = "自动 (CV特征识别)",
        classification_hint: ClassificationResult | None = None,
    ) -> tuple[bool, str, str | None]:
        """Determine whether an image needs enhancement and route to an available model."""
        try:
            local_threshold = threshold or getattr(self.plugin, "low_quality_threshold", 2160)

            def _get_dims():
                with PILImage.open(image_path) as img:
                    return img.width, img.height

            width, height = await asyncio.to_thread(_get_dims)
            is_low_res = width < local_threshold or height < local_threshold

            is_auto = model_setting in ("自动 (CV特征识别)", "auto") or "Auto" in model_setting
            anime_needs_minimum_long_edge = False
            if is_auto:
                classification = await self.classify_image(image_path, classification_hint)
                auto_scale = self._select_automatic_scale_for_long_edge(max(width, height))
                (
                    img_type_label,
                    dynamic_blur_threshold,
                    recommended_model,
                ) = self._classification_route(classification, auto_scale)
                anime_target_long_edge, _ = self._animejanai_automatic_limits()
                anime_needs_minimum_long_edge = (
                    classification.kind == "anime"
                    and max(width, height) < anime_target_long_edge
                )
            else:
                recommended_model = self._resolve_model(model_setting)
                img_type_label = f"手动指定({recommended_model})"
                dynamic_blur_threshold = 35.0 if self._model_spec(recommended_model).category == "anime" else 80.0

            logger.info("📏 [尺寸检测] 当前图片尺寸: %dx%d | 设定判定阈值: %dpx", width, height, local_threshold)
            if is_auto and recommended_model is None:
                logger.info("📝 [%s] 检测为文字或界面截图，跳过 AI 升图", img_type_label)
                return False, img_type_label, None
            if anime_needs_minimum_long_edge:
                logger.info(
                    "🔳 [%s] 最长边 %dpx < AnimeJaNai 自动目标 %dpx，触发 AI 升图",
                    img_type_label,
                    max(width, height),
                    anime_target_long_edge,
                )
                return True, img_type_label, recommended_model
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
            configured_models_dir = str(getattr(self.plugin, "animejanai_models_path", "") or "")
            persistent_models_dir = get_persistent_animejanai_models_path()
            return (
                str(getattr(self.plugin, "animejanai_bin_path", "") or sys.executable),
                configured_models_dir
                if configured_models_dir and Path(configured_models_dir).is_dir()
                else str(persistent_models_dir),
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
        if spec.backend == "animejanai":
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

        if spec.backend == "animejanai":
            runner = Path(__file__).with_name("animejanai_runner.py")
            return [
                binary,
                str(runner),
                "--input",
                str(input_path.resolve()),
                "--output",
                str(output_path.resolve()),
                "--model",
                model_name,
                "--models",
                models_dir,
                "--tile-size",
                str(ANIMEJANAI_TILE_SIZE),
                "--tile-overlap",
                str(ANIMEJANAI_TILE_OVERLAP),
            ]

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
        if spec.backend == "animejanai":
            model_path = Path(models_dir) / f"{model_name}.onnx"
            if not model_path.is_file():
                logger.error("❌ [ANIMEJANAI 模型缺失] 请将 %s 放入 %s", model_path.name, model_path.parent)
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
        spec = self._model_spec(model_name)
        automatic_animejanai = automatic_scale and spec.backend == "animejanai"
        animejanai_passes = 1
        animejanai_target_long_edge: int | None = None

        if automatic_animejanai:
            animejanai_passes, animejanai_target_long_edge = self._animejanai_automatic_plan(input_path)
            if animejanai_passes == 0:
                logger.info(
                    "📐 AnimeJaNai 输入已达到自动最低长边 %dpx，跳过: %s",
                    animejanai_target_long_edge,
                    input_path.name,
                )
                return input_path
            selected_scale = spec.native_scale
            logger.info(
                "📐 AnimeJaNai 自动执行 %d 轮原生 %dx，最终保留实际输出尺寸（目标长边 >= %dpx）",
                animejanai_passes,
                spec.native_scale,
                animejanai_target_long_edge,
            )
        else:
            if automatic_scale:
                selected_scale = self._select_automatic_scale(input_path)
                if selected_scale is None:
                    logger.info("📐 图片已达到自动升图长边上限，跳过: %s", input_path.name)
                    return input_path
            else:
                selected_scale = max(1, int(scale))

            if spec.backend in {"span", "animejanai"} and selected_scale != spec.native_scale:
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
        out_path = self._cache_path(
            input_path,
            model_name,
            selected_scale,
            bool(enable_taa),
            passes=animejanai_passes,
            target_long_edge=animejanai_target_long_edge if automatic_animejanai else None,
        )
        if self._is_fresh_cache(out_path):
            logger.info("💾 [Cache Hit] 命中 7 天内的 AI 升图缓存: %s", out_path.name)
            return out_path

        # 远程算力节点处理分支（双机部署模式）
        if self.remote_client.is_enabled:
            remote_success = await self.remote_client.upscale_image(
                input_path=input_path,
                output_path=out_path,
                model_name=model_name,
                scale=selected_scale,
                enable_taa=bool(enable_taa),
                passes=animejanai_passes,
            )
            if remote_success:
                if automatic_scale and not automatic_animejanai:
                    await self._cap_automatic_output(out_path)
                return out_path

            if self.remote_client.fallback_policy == "raise_error":
                raise RuntimeError(f"远程算力节点升图失败: {model_name}")
            logger.warning("⚠️ 远程算力节点处理失败，根据策略降级使用原图: %s", input_path.name)
            return input_path

        pass1_path = out_path.with_name(f"{out_path.stem}_pass1.png")
        try:
            if automatic_animejanai:
                if await self._run_animejanai_automatic_passes(
                    input_path,
                    out_path,
                    model_name,
                    bool(enable_taa),
                    animejanai_passes,
                ):
                    return out_path
            elif use_double_pass:
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
                fallback_scale = selected_scale
                if automatic_animejanai:
                    fallback_scale = self._select_automatic_scale(input_path) or 4
                fallback_path = self._cache_path(input_path, fallback, fallback_scale, bool(enable_taa))
                if self._is_fresh_cache(fallback_path):
                    return fallback_path
                logger.warning("⚠️ 模型 %s 执行失败，回退到 %s", model_name, fallback)
                if await self._run_model(input_path, fallback_path, fallback, fallback_scale, bool(enable_taa), "🎨 AI 升图回退中"):
                    if automatic_scale and not automatic_animejanai:
                        await self._cap_automatic_output(fallback_path)
                    return fallback_path
            return input_path
        finally:
            pass1_path.unlink(missing_ok=True)
