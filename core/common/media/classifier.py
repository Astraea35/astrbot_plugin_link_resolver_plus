import asyncio
import gc
import hashlib
import json
import math
import os
import time
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import cv2
import httpx
import numpy as np
from PIL import Image as PILImage
from astrbot.api import logger

from ..paths import get_persistent_classifier_models_path


CLASSIFIER_VERSION = "rule-v2+clip-v1"
RULE_VERSION = "rule-v2"
CLASSIFIER_RELEASE_BASE_URL = (
    "https://github.com/Astraea35/astrbot_plugin_link_resolver_plus/"
    "releases/download/classifier-v1"
)
MODEL_FILE_NAME = "clip_vision_fp16.onnx"
PROTOTYPES_FILE_NAME = "clip_prototypes.json"
LICENSE_FILE_NAME = "CLIP_LICENSE.txt"
CHECKSUMS_FILE_NAME = "SHA256SUMS.txt"
DOWNLOAD_ASSETS = (MODEL_FILE_NAME, PROTOTYPES_FILE_NAME, LICENSE_FILE_NAME)
CLASSIFIER_ASSET_SHA256 = {
    MODEL_FILE_NAME: "013f803271d6264f6f993ec3ca2597426810ed52b0a9855732239b02bda3dd3a",
    PROTOTYPES_FILE_NAME: "c6cad2d9c1dd36da471000d95c155fce535f62af2709a9d9860c6d8516a104fe",
    LICENSE_FILE_NAME: "f3623d45c2b6f37fc3d3e129d53275f8d65490915bec41b8cf7b66367d00d607",
}
CLASSIFIER_MANIFEST_SHA256 = (
    "ad2b93d4247dcdd0c5ab977ac97566ced13160b77eae16593197922e7063f108"
)
CLIP_MEAN = np.asarray([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
CLIP_STD = np.asarray([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
VALID_KINDS = {"anime", "photo", "text_ui", "uncertain"}
MAX_CLIP_SOURCE_PIXELS = 80_000_000


class ClassifierInputError(ValueError):
    pass


class ClassifierResourceError(RuntimeError):
    pass


def cv2_imread_safe(image_path, flags=cv2.IMREAD_COLOR):
    """Read paths containing non-ASCII characters on Windows."""
    try:
        data = np.fromfile(str(Path(image_path).resolve()), dtype=np.uint8)
        return cv2.imdecode(data, flags)
    except Exception:
        return None


def cv2_imread_for_analysis(image_path):
    """Decode large platform images close to the 512px analysis resolution."""
    path = Path(image_path).resolve()
    try:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", PILImage.DecompressionBombWarning)
                with PILImage.open(path) as image:
                    long_edge = max(image.size)
        except PILImage.DecompressionBombError:
            long_edge = 8192
        if long_edge > 4096:
            flags = cv2.IMREAD_REDUCED_COLOR_8
        elif long_edge > 2048:
            flags = cv2.IMREAD_REDUCED_COLOR_4
        elif long_edge > 1024:
            flags = cv2.IMREAD_REDUCED_COLOR_2
        else:
            flags = cv2.IMREAD_COLOR
        image = cv2_imread_safe(path, flags)
        return image if image is not None else cv2_imread_safe(path, cv2.IMREAD_COLOR)
    except Exception:
        return cv2_imread_safe(path, cv2.IMREAD_COLOR)


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _scaled(value: float, low: float, high: float) -> float:
    if high <= low:
        return 0.0
    return _clip01((value - low) / (high - low))


@dataclass(frozen=True)
class ClassificationResult:
    kind: str
    confidence: float
    source: str
    features: dict[str, float]
    version: str = CLASSIFIER_VERSION

    def __post_init__(self) -> None:
        if self.kind not in VALID_KINDS:
            raise ValueError(f"Unsupported classification kind: {self.kind}")


class RuleV2Classifier:
    """CPU-first classifier using color, texture, line, and UI structure cues."""

    max_edge = 512

    @staticmethod
    def _resize_for_analysis(image: np.ndarray) -> np.ndarray:
        height, width = image.shape[:2]
        long_edge = max(height, width)
        if long_edge <= RuleV2Classifier.max_edge:
            return image
        scale = RuleV2Classifier.max_edge / long_edge
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        return cv2.resize(image, size, interpolation=cv2.INTER_AREA)

    @staticmethod
    def _entropy(channel: np.ndarray) -> float:
        histogram = cv2.calcHist([channel], [0], None, [32], [0, 256]).reshape(-1)
        total = float(histogram.sum())
        if total <= 0:
            return 0.0
        probabilities = histogram[histogram > 0] / total
        return float(-(probabilities * np.log2(probabilities)).sum() / 5.0)

    @staticmethod
    def _text_and_rectangle_features(gray: np.ndarray) -> tuple[float, float]:
        height, width = gray.shape
        image_area = max(1, height * width)
        binary = cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV,
            21,
            9,
        )
        contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        text_boxes: list[tuple[int, int]] = []
        rectangle_area = 0.0
        for contour in contours:
            _, y, box_width, box_height = cv2.boundingRect(contour)
            area = float(cv2.contourArea(contour))
            box_area = max(1, box_width * box_height)
            if (
                2 <= box_width <= width * 0.28
                and 3 <= box_height <= max(12, height * 0.055)
                and 5 <= area <= image_area * 0.004
                and 0.08 <= area / box_area <= 0.95
                and 0.12 <= box_width / max(1, box_height) <= 16.0
            ):
                text_boxes.append((y + box_height // 2, box_height))

            if image_area * 0.004 <= area <= image_area * 0.45:
                perimeter = cv2.arcLength(contour, True)
                approximation = cv2.approxPolyDP(contour, 0.025 * perimeter, True)
                if len(approximation) == 4 and cv2.isContourConvex(approximation):
                    rectangle_area += min(area, image_area * 0.12)

        row_size = max(4, height // 50)
        row_counts: dict[int, int] = {}
        for center_y, _ in text_boxes:
            row = center_y // row_size
            row_counts[row] = row_counts.get(row, 0) + 1
        aligned_text = sum(count for count in row_counts.values() if count >= 3)
        alignment = aligned_text / max(1, len(text_boxes))
        units = image_area / 10000.0
        text_density = _clip01(aligned_text / max(8.0, units * 3.0)) * _scaled(
            alignment, 0.30, 0.85
        )
        rectangle_density = _clip01(rectangle_area / (image_area * 0.22))
        return text_density, rectangle_density

    def extract_features(self, image: np.ndarray) -> dict[str, float]:
        image = self._resize_for_analysis(image)
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        saturation = hsv[:, :, 1].astype(np.float32) / 255.0

        saturation_mean = float(saturation.mean())
        saturation_high_ratio = float(np.mean(saturation >= 0.50))
        low_saturation_ratio = float(np.mean(saturation <= 0.14))
        light_background_ratio = float(np.mean(gray >= 224))
        dark_background_ratio = float(np.mean(gray <= 30))

        gray_float = gray.astype(np.float32)
        local_mean = cv2.blur(gray_float, (9, 9))
        local_square_mean = cv2.blur(gray_float * gray_float, (9, 9))
        local_std = np.sqrt(np.maximum(local_square_mean - local_mean * local_mean, 0.0))
        flat_region_ratio = float(np.mean(local_std < 7.5))

        edges = cv2.Canny(gray, 60, 160)
        edge_ratio = float(np.count_nonzero(edges) / max(1, edges.size))
        sobel_x = cv2.Sobel(gray_float, cv2.CV_32F, 1, 0, ksize=3)
        sobel_y = cv2.Sobel(gray_float, cv2.CV_32F, 0, 1, ksize=3)
        edge_mask = edges > 0
        if np.any(edge_mask):
            angles = np.arctan2(np.abs(sobel_y[edge_mask]), np.abs(sobel_x[edge_mask]) + 1e-6)
            axis_aligned_ratio = float(
                np.mean((angles <= math.radians(16)) | (angles >= math.radians(74)))
            )
        else:
            axis_aligned_ratio = 0.0

        laplacian = cv2.Laplacian(gray_float, cv2.CV_32F)
        local_texture = float(np.mean(np.abs(laplacian)) / 255.0)
        color_entropy = float(
            np.mean([self._entropy(image[:, :, index]) for index in range(3)])
        )
        text_density, rectangle_density = self._text_and_rectangle_features(gray)

        background_uniformity = max(light_background_ratio, dark_background_ratio)
        ui_score = _clip01(
            0.22 * _scaled(low_saturation_ratio, 0.45, 0.92)
            + 0.18 * _scaled(axis_aligned_ratio, 0.46, 0.82)
            + 0.28 * text_density
            + 0.17 * rectangle_density
            + 0.15 * _scaled(background_uniformity, 0.25, 0.78)
        )

        texture_score = _scaled(local_texture, 0.025, 0.16)
        line_art_score = (
            _scaled(edge_ratio, 0.065, 0.17)
            * _scaled(flat_region_ratio, 0.20, 0.52)
            * (1.0 - _scaled(local_texture, 0.075, 0.18))
        ) ** (1.0 / 3.0)
        anime_score = _clip01(
            0.22 * _scaled(saturation_mean, 0.16, 0.55)
            + 0.10 * _scaled(saturation_high_ratio, 0.08, 0.52)
            + 0.24 * _scaled(flat_region_ratio, 0.18, 0.72)
            + 0.18 * _scaled(edge_ratio, 0.018, 0.105)
            + 0.16 * (1.0 - texture_score)
            + 0.10 * (1.0 - _scaled(color_entropy, 0.58, 0.94))
            + 0.18 * line_art_score
            - 0.28 * ui_score
        )

        return {
            "anime_score": anime_score,
            "ui_score": ui_score,
            "saturation_mean": saturation_mean,
            "saturation_high_ratio": saturation_high_ratio,
            "low_saturation_ratio": low_saturation_ratio,
            "color_entropy": color_entropy,
            "flat_region_ratio": flat_region_ratio,
            "edge_ratio": edge_ratio,
            "axis_aligned_ratio": axis_aligned_ratio,
            "local_texture": local_texture,
            "line_art_score": line_art_score,
            "text_density": text_density,
            "rectangle_density": rectangle_density,
            "background_uniformity": background_uniformity,
        }

    def classify_array(self, image: np.ndarray | None) -> ClassificationResult:
        if image is None or image.size == 0:
            return ClassificationResult(
                "uncertain", 0.0, "rule_v2", {"read_error": 1.0}, RULE_VERSION
            )
        try:
            features = self.extract_features(image)
            ui_score = features["ui_score"]
            if ui_score >= 0.72 and (
                features["text_density"] >= 0.26
                or features["rectangle_density"] >= 0.34
            ):
                return ClassificationResult(
                    "text_ui", ui_score, "rule_v2", features, RULE_VERSION
                )

            if (
                ui_score >= 0.40
                and features["background_uniformity"] >= 0.65
                and features["text_density"] >= 0.12
            ):
                return ClassificationResult(
                    "uncertain", ui_score, "rule_v2", features, RULE_VERSION
                )

            anime_score = features["anime_score"]
            confidence = _clip01(0.5 + abs(anime_score - 0.5))
            if anime_score <= 0.35:
                kind = "photo"
            elif anime_score >= 0.65:
                kind = "anime"
            else:
                kind = "uncertain"
            return ClassificationResult(kind, confidence, "rule_v2", features, RULE_VERSION)
        except Exception as exc:
            logger.warning("Rule V2 image classification failed: %s", exc)
            return ClassificationResult(
                "uncertain", 0.0, "rule_v2", {"rule_error": 1.0}, RULE_VERSION
            )

    def classify(self, image_path: Path) -> ClassificationResult:
        return self.classify_array(cv2_imread_for_analysis(image_path))

    def classify_many(self, image_paths: Iterable[Path]) -> list[ClassificationResult]:
        return [self.classify(Path(path)) for path in image_paths]


class HybridImageClassifier:
    """Rule V2 classifier with lazy serialized CLIP review and bounded lifetime."""

    def __init__(
        self,
        plugin_instance,
        *,
        model_dir: Path | None = None,
        asset_hashes: dict[str, str] | None = None,
        manifest_sha256: str | None = None,
    ) -> None:
        self.plugin = plugin_instance
        self.rule = RuleV2Classifier()
        self._model_dir_override = Path(model_dir) if model_dir else None
        self._session = None
        self._provider = None
        self._prototypes: dict[str, np.ndarray] | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._session_key: tuple[str, int, int, str, int, int, str] | None = None
        self._validated_resource_key: tuple[str, int, int, str, int, int] | None = None
        self._expected_hashes = dict(asset_hashes or CLASSIFIER_ASSET_SHA256)
        self._manifest_sha256 = manifest_sha256 or CLASSIFIER_MANIFEST_SHA256
        self._load_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()
        self._active_inference = 0
        self._inference_idle = asyncio.Event()
        self._inference_idle.set()
        self._idle_task: asyncio.Task | None = None
        self._download_task: asyncio.Task | None = None
        self._closing = False

    @property
    def model_dir(self) -> Path:
        if self._model_dir_override is not None:
            path = self._model_dir_override
        else:
            configured = str(getattr(self.plugin, "classifier_models_path", "") or "").strip()
            path = Path(configured) if configured else get_persistent_classifier_models_path()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def model_path(self) -> Path:
        return self.model_dir / MODEL_FILE_NAME

    @property
    def prototypes_path(self) -> Path:
        return self.model_dir / PROTOTYPES_FILE_NAME

    def resources_ready(self) -> bool:
        return self.model_path.is_file() and self.prototypes_path.is_file()

    def start_background_download(self) -> asyncio.Task | None:
        if self._closing or not bool(getattr(self.plugin, "classifier_auto_download", True)):
            return None
        if self._download_task and not self._download_task.done():
            return self._download_task
        self._download_task = asyncio.create_task(
            self._download_resources(), name="link-resolver-classifier-download"
        )
        return self._download_task

    @staticmethod
    def _parse_checksums(text: str) -> dict[str, str]:
        checksums: dict[str, str] = {}
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.replace(" *", "  ").split()
            if len(parts) >= 2 and len(parts[0]) == 64:
                checksums[Path(parts[-1]).name] = parts[0].lower()
        return checksums

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file_obj:
            for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    async def _download_file(
        self, client: httpx.AsyncClient, url: str, path: Path
    ) -> Path:
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.part"
        )
        try:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                with temporary.open("wb") as file_obj:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        file_obj.write(chunk)
            return temporary
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    @asynccontextmanager
    async def _directory_download_lock(self):
        lock_path = self.model_dir / ".classifier-download.lock"
        token = f"{os.getpid()}:{uuid.uuid4().hex}"
        deadline = asyncio.get_running_loop().time() + 600.0
        acquired = False
        while not acquired:
            if self._closing:
                raise asyncio.CancelledError
            try:
                descriptor = os.open(
                    str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
            except FileExistsError:
                try:
                    age = max(0.0, time.time() - lock_path.stat().st_mtime)
                    if age > 1800.0:
                        lock_path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("Timed out waiting for classifier download lock")
                await asyncio.sleep(0.25)
            else:
                with os.fdopen(descriptor, "w", encoding="ascii") as lock_file:
                    lock_file.write(token)
                acquired = True
        try:
            yield
        finally:
            try:
                if lock_path.read_text(encoding="ascii", errors="ignore") == token:
                    lock_path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass

    async def _asset_matches(self, path: Path, expected_hash: str) -> bool:
        if not path.is_file():
            return False
        actual_hash = await asyncio.to_thread(self._sha256, path)
        return actual_hash == expected_hash

    async def _download_resources(self) -> None:
        base_url = str(
            getattr(self.plugin, "classifier_release_base_url", CLASSIFIER_RELEASE_BASE_URL)
            or CLASSIFIER_RELEASE_BASE_URL
        ).rstrip("/")
        target_dir = self.model_dir
        checksum_path = target_dir / CHECKSUMS_FILE_NAME
        temporary_files: set[Path] = set()
        try:
            async with self._directory_download_lock():
                timeout = httpx.Timeout(30.0, read=300.0)
                async with httpx.AsyncClient(
                    follow_redirects=True, timeout=timeout
                ) as client:
                    checksum_temporary = await self._download_file(
                        client, f"{base_url}/{CHECKSUMS_FILE_NAME}", checksum_path
                    )
                    temporary_files.add(checksum_temporary)
                    manifest_hash = await asyncio.to_thread(
                        self._sha256, checksum_temporary
                    )
                    if manifest_hash != self._manifest_sha256:
                        raise ValueError("Classifier checksum manifest is not trusted")
                    checksums = self._parse_checksums(
                        checksum_temporary.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    )
                    if any(
                        checksums.get(name) != self._expected_hashes.get(name)
                        for name in DOWNLOAD_ASSETS
                    ):
                        raise ValueError("Classifier checksum manifest is inconsistent")
                    os.replace(checksum_temporary, checksum_path)
                    temporary_files.discard(checksum_temporary)

                    for name in DOWNLOAD_ASSETS:
                        destination = target_dir / name
                        expected_hash = self._expected_hashes[name]
                        if await self._asset_matches(destination, expected_hash):
                            continue
                        temporary = await self._download_file(
                            client, f"{base_url}/{name}", destination
                        )
                        temporary_files.add(temporary)
                        actual_hash = await asyncio.to_thread(self._sha256, temporary)
                        if actual_hash != expected_hash:
                            raise ValueError(
                                f"Classifier asset checksum mismatch: {name}"
                            )
                        os.replace(temporary, destination)
                        temporary_files.discard(temporary)
                self._validated_resource_key = self._resource_key()
            logger.info("Classifier resources are ready: %s", target_dir)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Classifier resource download failed; Rule V2 remains active: %s", exc)
        finally:
            for partial in temporary_files:
                partial.unlink(missing_ok=True)

    @staticmethod
    def _load_prototypes(path: Path) -> dict[str, np.ndarray]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw = payload.get("prototypes", {})
        prototypes: dict[str, np.ndarray] = {}
        for kind in ("anime", "photo", "text_ui"):
            vector = np.asarray(raw.get(kind), dtype=np.float32)
            if vector.shape != (512,):
                raise ValueError(f"Invalid CLIP prototype for {kind}")
            norm = float(np.linalg.norm(vector))
            if norm <= 1e-12:
                raise ValueError(f"Empty CLIP prototype for {kind}")
            prototypes[kind] = vector / norm
        return prototypes

    @staticmethod
    def _create_session(model_path: Path, provider: str):
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.enable_mem_pattern = False
        options.intra_op_num_threads = 1
        if provider == "directml" and "DmlExecutionProvider" in ort.get_available_providers():
            providers = ["DmlExecutionProvider", "CPUExecutionProvider"]
        else:
            providers = ["CPUExecutionProvider"]
        return ort.InferenceSession(
            str(model_path), sess_options=options, providers=providers
        ), providers[0]

    @staticmethod
    def _normalize_provider(provider: str | None) -> str:
        selected = str(provider or "DirectML").lower()
        return "directml" if selected.startswith("direct") else "cpu"

    def _resource_key(self) -> tuple[str, int, int, str, int, int]:
        model_path = self.model_path.resolve()
        prototypes_path = self.prototypes_path.resolve()
        model_stat = model_path.stat()
        prototypes_stat = prototypes_path.stat()
        return (
            str(model_path),
            model_stat.st_mtime_ns,
            model_stat.st_size,
            str(prototypes_path),
            prototypes_stat.st_mtime_ns,
            prototypes_stat.st_size,
        )

    async def _validate_runtime_resources(self) -> tuple[str, int, int, str, int, int]:
        try:
            resource_key = self._resource_key()
        except OSError as exc:
            raise ClassifierResourceError("CLIP runtime resources are missing") from exc
        if resource_key == self._validated_resource_key:
            return resource_key
        for name in (MODEL_FILE_NAME, PROTOTYPES_FILE_NAME):
            path = self.model_dir / name
            expected_hash = self._expected_hashes[name]
            if not await self._asset_matches(path, expected_hash):
                path.unlink(missing_ok=True)
                self._validated_resource_key = None
                raise ClassifierResourceError(f"CLIP runtime resource is invalid: {name}")
        self._validated_resource_key = self._resource_key()
        return self._validated_resource_key

    async def _clear_runtime_locked(self) -> None:
        session = self._session
        executor = self._executor
        self._session = None
        self._executor = None
        self._prototypes = None
        self._provider = None
        self._session_key = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        if session is not None or executor is not None:
            session = None
            await asyncio.to_thread(gc.collect)

    async def _ensure_session(self, provider: str | None = None):
        selected_provider = self._normalize_provider(
            provider
            or str(getattr(self.plugin, "classifier_provider", "DirectML") or "DirectML")
        )
        resource_key = await self._validate_runtime_resources()
        desired_key = (*resource_key, selected_provider)
        if self._session is not None and self._session_key == desired_key:
            return self._session
        async with self._load_lock:
            resource_key = await self._validate_runtime_resources()
            desired_key = (*resource_key, selected_provider)
            if self._session is not None and self._session_key == desired_key:
                return self._session
            if self._closing or not self.resources_ready():
                return None
            if self._session is not None or self._executor is not None:
                await self._clear_runtime_locked()
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="clip-classifier"
            )
            loop = asyncio.get_running_loop()
            try:
                try:
                    self._prototypes = await asyncio.to_thread(
                        self._load_prototypes, self.prototypes_path
                    )
                except Exception as exc:
                    self.prototypes_path.unlink(missing_ok=True)
                    raise ClassifierResourceError(
                        "CLIP prototype resource is invalid"
                    ) from exc
                try:
                    self._session, self._provider = await loop.run_in_executor(
                        self._executor,
                        self._create_session,
                        self.model_path,
                        selected_provider,
                    )
                    self._session_key = desired_key
                except Exception:
                    raise
                logger.info("CLIP classifier loaded with %s", self._provider)
            except Exception:
                executor = self._executor
                self._executor = None
                self._session = None
                self._prototypes = None
                self._session_key = None
                if executor is not None:
                    executor.shutdown(wait=False, cancel_futures=True)
                raise
            return self._session

    @staticmethod
    def _preprocess_image(path: Path) -> np.ndarray:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", PILImage.DecompressionBombWarning)
                with PILImage.open(path) as image:
                    if image.width * image.height > MAX_CLIP_SOURCE_PIXELS:
                        raise ClassifierInputError(
                            f"Image is too large for CLIP review: {image.width}x{image.height}"
                        )
                    image = image.convert("RGB")
                    width, height = image.size
                    scale = 224.0 / min(width, height)
                    resized = image.resize(
                        (max(224, round(width * scale)), max(224, round(height * scale))),
                        PILImage.Resampling.BICUBIC,
                    )
                    left = max(0, (resized.width - 224) // 2)
                    top = max(0, (resized.height - 224) // 2)
                    array = np.asarray(
                        resized.crop((left, top, left + 224, top + 224)),
                        dtype=np.float32,
                    )
        except ClassifierInputError:
            raise
        except PILImage.DecompressionBombError as exc:
            raise ClassifierInputError(f"Image exceeds Pillow safety limit: {path.name}") from exc
        except Exception as exc:
            raise ClassifierInputError(f"Unable to read image for CLIP: {path.name}") from exc
        array = array / 255.0
        array = (array - CLIP_MEAN) / CLIP_STD
        return np.transpose(array, (2, 0, 1))[None, ...].astype(np.float16)

    def _run_session(self, tensor: np.ndarray) -> np.ndarray:
        output = self._session.run(["image_embeds"], {"pixel_values": tensor})[0]
        vector = np.asarray(output[0], dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        if norm <= 1e-12:
            raise ValueError("CLIP produced an empty image embedding")
        return vector / norm

    @staticmethod
    def _softmax(values: np.ndarray) -> np.ndarray:
        shifted = values - float(np.max(values))
        exponentials = np.exp(shifted)
        return exponentials / max(float(exponentials.sum()), 1e-12)

    def _result_from_embedding(
        self, embedding: np.ndarray, rule_result: ClassificationResult
    ) -> ClassificationResult:
        kinds = ("anime", "photo", "text_ui")
        logit_scale = float(getattr(self.plugin, "classifier_clip_logit_scale", 100.0))
        logits = np.asarray(
            [float(np.dot(embedding, self._prototypes[kind])) for kind in kinds],
            dtype=np.float32,
        ) * logit_scale
        probabilities = self._softmax(logits)
        order = np.argsort(probabilities)[::-1]
        best_index, second_index = int(order[0]), int(order[1])
        best_probability = float(probabilities[best_index])
        margin = best_probability - float(probabilities[second_index])
        min_probability = float(
            getattr(self.plugin, "classifier_clip_min_probability", 0.60)
        )
        min_margin = float(getattr(self.plugin, "classifier_clip_min_margin", 0.10))
        kind = (
            kinds[best_index]
            if best_probability >= min_probability and margin >= min_margin
            else "uncertain"
        )
        features = dict(rule_result.features)
        features.update(
            {
                "clip_anime_probability": float(probabilities[0]),
                "clip_photo_probability": float(probabilities[1]),
                "clip_text_ui_probability": float(probabilities[2]),
                "clip_margin": margin,
            }
        )
        return ClassificationResult(kind, best_probability, "clip", features)

    def _cancel_idle_timer(self) -> None:
        task = self._idle_task
        self._idle_task = None
        if task and not task.done():
            task.cancel()

    def _schedule_idle_unload(self) -> None:
        self._cancel_idle_timer()
        if self._closing or self._session is None:
            return
        timeout = max(0.0, float(getattr(self.plugin, "classifier_idle_unload_seconds", 30)))
        self._idle_task = asyncio.create_task(
            self._unload_after_idle(timeout), name="link-resolver-classifier-idle-unload"
        )

    async def _unload_after_idle(self, timeout: float) -> None:
        try:
            await asyncio.sleep(timeout)
            if self._active_inference == 0:
                await self.release()
        except asyncio.CancelledError:
            return

    @asynccontextmanager
    async def _gpu_gate(self):
        gate = getattr(self.plugin, "ai_upscale_slot", None)
        if gate is None:
            yield
            return
        async with gate:
            yield

    async def _review_one(
        self,
        path: Path,
        rule_result: ClassificationResult,
        provider: str | None = None,
    ) -> ClassificationResult:
        self._cancel_idle_timer()
        async with self._run_lock:
            if self._closing:
                return rule_result
            self._active_inference += 1
            self._inference_idle.clear()
            try:
                tensor = await asyncio.to_thread(self._preprocess_image, path)
                selected_provider = self._normalize_provider(
                    provider
                    or str(
                        getattr(self.plugin, "classifier_provider", "DirectML")
                        or "DirectML"
                    )
                )
                if selected_provider == "directml":
                    async with self._gpu_gate():
                        session = await self._ensure_session(selected_provider)
                        if session is None or self._executor is None:
                            return rule_result
                        if self._provider == "DmlExecutionProvider":
                            loop = asyncio.get_running_loop()
                            embedding = await loop.run_in_executor(
                                self._executor, self._run_session, tensor
                            )
                        else:
                            embedding = None
                    if embedding is None:
                        loop = asyncio.get_running_loop()
                        embedding = await loop.run_in_executor(
                            self._executor, self._run_session, tensor
                        )
                else:
                    session = await self._ensure_session(selected_provider)
                    if session is None or self._executor is None:
                        return rule_result
                    loop = asyncio.get_running_loop()
                    embedding = await loop.run_in_executor(
                        self._executor, self._run_session, tensor
                    )
                return self._result_from_embedding(embedding, rule_result)
            finally:
                self._active_inference = max(0, self._active_inference - 1)
                if self._active_inference == 0:
                    self._inference_idle.set()
                    if self._closing:
                        await self.release()
                    else:
                        self._schedule_idle_unload()

    async def _review_with_cpu_fallback(
        self, path: Path, rule_result: ClassificationResult
    ) -> ClassificationResult:
        try:
            return await self._review_one(path, rule_result)
        except Exception as directml_error:
            if isinstance(directml_error, ClassifierInputError):
                logger.warning("Skipping CLIP review for %s: %s", path.name, directml_error)
                return rule_result
            if isinstance(directml_error, ClassifierResourceError):
                self.start_background_download()
                return rule_result
            configured_directml = str(
                getattr(self.plugin, "classifier_provider", "DirectML") or "DirectML"
            ).lower().startswith("direct")
            if self._provider == "CPUExecutionProvider" or not configured_directml:
                raise
            logger.warning(
                "CLIP DirectML inference failed, retrying on CPU: %s", directml_error
            )
            await self.release_before_upscale()
            try:
                return await self._review_one(path, rule_result, "cpu")
            except Exception as cpu_error:
                self.model_path.unlink(missing_ok=True)
                self.start_background_download()
                raise ClassifierResourceError(
                    "CLIP failed on both DirectML and CPU"
                ) from cpu_error

    async def classify_many(
        self, image_paths: Iterable[Path]
    ) -> list[ClassificationResult]:
        paths = [Path(path) for path in image_paths]
        rule_results = await asyncio.to_thread(self.rule.classify_many, paths)
        if self._closing or not bool(getattr(self.plugin, "hybrid_classifier_enabled", True)):
            return rule_results
        uncertain_indices = [
            index for index, result in enumerate(rule_results) if result.kind == "uncertain"
        ]
        if not uncertain_indices:
            return rule_results
        if not self.resources_ready():
            self.start_background_download()
            return rule_results

        reviewed = list(rule_results)
        for index in uncertain_indices:
            try:
                reviewed[index] = await self._review_with_cpu_fallback(
                    paths[index], rule_results[index]
                )
            except Exception as exc:
                logger.warning(
                    "CLIP review failed for %s; using Rule V2 result: %s",
                    paths[index].name,
                    exc,
                )
                configured_cpu = not str(
                    getattr(self.plugin, "classifier_provider", "DirectML")
                    or "DirectML"
                ).lower().startswith("direct")
                if self._provider == "CPUExecutionProvider" or configured_cpu:
                    self.model_path.unlink(missing_ok=True)
                    self._validated_resource_key = None
                self.start_background_download()
        return reviewed

    async def classify(self, image_path: Path) -> ClassificationResult:
        return (await self.classify_many([image_path]))[0]

    async def release(self) -> None:
        if self._active_inference > 0:
            return
        current_task = asyncio.current_task()
        idle_task = self._idle_task
        self._idle_task = None
        if idle_task and idle_task is not current_task and not idle_task.done():
            idle_task.cancel()
        async with self._load_lock:
            if self._active_inference > 0:
                return
            had_runtime = self._session is not None or self._executor is not None
            await self._clear_runtime_locked()
            if had_runtime:
                logger.info("CLIP classifier released")

    async def release_before_upscale(self) -> None:
        self._cancel_idle_timer()
        if self._active_inference > 0:
            await self._inference_idle.wait()
        await self.release()

    async def shutdown(self, timeout: float = 10.0) -> None:
        self._closing = True
        self._cancel_idle_timer()
        if self._download_task and not self._download_task.done():
            self._download_task.cancel()
            await asyncio.gather(self._download_task, return_exceptions=True)
        try:
            await asyncio.wait_for(self._inference_idle.wait(), timeout=max(0.0, timeout))
        except asyncio.TimeoutError:
            logger.warning("Timed out waiting for CLIP inference during plugin shutdown")
        await self.release()


class AnimePhotoClassifier(RuleV2Classifier):
    """Backward-compatible boolean facade for older integrations."""

    def predict_is_anime(self, image_path) -> bool:
        result = self.classify(Path(image_path))
        return result.kind == "anime"


_classifier_instance = AnimePhotoClassifier()


def get_classifier():
    return _classifier_instance
