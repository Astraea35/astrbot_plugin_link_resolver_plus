"""Focused tests for dual-backend model routing in the upscaler."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image


media_path = Path(__file__).resolve().parents[1] / "core" / "common" / "media"
media_package = types.ModuleType("core.common.media")
media_package.__path__ = [str(media_path)]
sys.modules.setdefault("core.common.media", media_package)

astrbot_module = types.ModuleType("astrbot")
astrbot_api_module = types.ModuleType("astrbot.api")
astrbot_api_module.logger = MagicMock()
astrbot_module.api = astrbot_api_module
sys.modules.setdefault("astrbot", astrbot_module)
sys.modules.setdefault("astrbot.api", astrbot_api_module)

# Keep this lightweight test stub compatible with tests that import the image tool
# in the same unittest process.
active_astrbot = sys.modules["astrbot"]
active_api = sys.modules["astrbot.api"]
if not hasattr(active_api, "logger"):
    active_api.logger = MagicMock()

event_module = sys.modules.setdefault("astrbot.api.event", types.ModuleType("astrbot.api.event"))
if not hasattr(event_module, "AstrMessageEvent"):
    event_module.AstrMessageEvent = type("AstrMessageEvent", (), {})

components_module = sys.modules.setdefault(
    "astrbot.api.message_components", types.ModuleType("astrbot.api.message_components")
)
if not hasattr(components_module, "Image"):
    class ImageComponent:
        def __init__(self, url=None, file=None, path=None):
            self.url = url
            self.file = file
            self.path = path

    components_module.Image = ImageComponent
if not hasattr(components_module, "Reply"):
    class ReplyComponent:
        def __init__(self, id=None, message_id=None):
            self.id = id or message_id
            self.message_id = id or message_id

    components_module.Reply = ReplyComponent

active_astrbot.api = active_api
active_api.event = event_module
active_api.message_components = components_module

from core.common.media.upscaler import (  # noqa: E402
    AUTO_ANIME_MODEL,
    AUTO_PHOTO_MODEL,
    MODEL_REGISTRY,
    UPSCAYL_MODEL_NAME_MAP,
    UpscaylUpscaler,
)
from core.common.media.classifier import ClassificationResult  # noqa: E402


class UpscalerRoutingTests(unittest.TestCase):
    def setUp(self):
        self.plugin = types.SimpleNamespace(
            auto_upscale_max_long_edge=3840,
            upscayl_enable_taa=True,
        )
        self.upscaler = UpscaylUpscaler(self.plugin)

    def _image(self, directory: str, name: str, size: tuple[int, int]) -> Path:
        path = Path(directory) / name
        Image.new("RGB", size, "white").save(path)
        return path

    def test_registry_routes_new_models_to_intended_backends(self):
        self.assertEqual(MODEL_REGISTRY["realesr-animevideov3"].backend, "upscayl")
        self.assertEqual(MODEL_REGISTRY[AUTO_PHOTO_MODEL].backend, "span")
        self.assertEqual(MODEL_REGISTRY["liveaction-v1-span-2x"].native_scale, 2)
        self.assertEqual(MODEL_REGISTRY["hfa2k-span-2x"].native_scale, 2)
        self.assertEqual(MODEL_REGISTRY["animejanai-v3.1-balanced"].backend, "animejanai")
        self.assertEqual(MODEL_REGISTRY["ultrasharp-4x"].backend, "upscayl")

    def test_all_schema_model_display_names_resolve_to_registered_models(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        model_fields = [
            schema[section]["items"]["upscayl_model_name"]
            for section in ("douyin_settings", "weibo_settings", "twitter_settings", "xhs_settings")
        ]
        model_fields.append(schema["image_tool_settings"]["items"]["model_name"])

        configured_names = {name for field in model_fields for name in field["options"]}
        unresolved = {
            name
            for name in configured_names
            if UPSCAYL_MODEL_NAME_MAP.get(name, name) not in MODEL_REGISTRY and name != "自动 (CV特征识别)"
        }
        self.assertFalse(unresolved, f"schema model names missing registry aliases: {sorted(unresolved)}")

    def test_automatic_scale_respects_maximum_long_edge(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.upscaler._select_automatic_scale(self._image(directory, "small.png", (960, 540))), 4)
            self.assertEqual(self.upscaler._select_automatic_scale(self._image(directory, "medium.png", (3000, 1600))), 2)
            self.assertIsNone(self.upscaler._select_automatic_scale(self._image(directory, "large.png", (3840, 2160))))

    def test_cache_key_isolated_by_backend_model_and_scale(self):
        input_path = Path("C:/cache/source.png")
        anime = self.upscaler._cache_path(input_path, AUTO_ANIME_MODEL, 4, True)
        photo = self.upscaler._cache_path(input_path, AUTO_PHOTO_MODEL, 4, True)
        scaled = self.upscaler._cache_path(input_path, AUTO_ANIME_MODEL, 2, True)

        self.assertNotEqual(anime, photo)
        self.assertNotEqual(anime, scaled)
        self.assertIn("animejanai_animejanai-v3.1-balanced_4x", anime.name)

    def test_animejanai_can_use_an_external_command_template(self):
        self.plugin.animejanai_command_template = '"runner.exe" --input "{input}" --output "{output}" --model {model} --scale {scale}'
        command = self.upscaler._build_command(
            "unused.exe",
            "C:/models",
            MODEL_REGISTRY["animejanai-v3.1-balanced"],
            Path("C:/input image.png"),
            Path("C:/output image.png"),
            "animejanai-v3.1-balanced",
            2,
            False,
        )

        self.assertEqual(command[0], "runner.exe")
        self.assertEqual(command[1], "--input")
        self.assertTrue(command[2].endswith("input image.png"))
        self.assertEqual(command[-2:], ["--scale", "2"])

    def test_animejanai_uses_persistent_model_directory_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            module = sys.modules[UpscaylUpscaler.__module__]
            with patch.object(module, "get_persistent_animejanai_models_path", return_value=Path(directory)):
                binary, models_dir = self.upscaler._backend_paths("animejanai")

        self.assertEqual(binary, sys.executable)
        self.assertEqual(models_dir, directory)

    def test_automatic_output_is_capped_to_configured_long_edge(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = self._image(directory, "output.png", (6000, 3000))
            asyncio.run(self.upscaler._cap_automatic_output(output_path))
            with Image.open(output_path) as output:
                self.assertEqual(output.size, (3840, 1920))

    def test_span_anime_failure_falls_back_to_legacy_anime_model(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = self._image(directory, "input.png", (400, 300))
            self.upscaler._run_model = AsyncMock(side_effect=[False, True])

            result = asyncio.run(self.upscaler.upscale_image(input_path, "test", override_model=AUTO_ANIME_MODEL))

        self.assertIn("upscayl_digital-art-4x", result.name)
        self.assertEqual(self.upscaler._run_model.await_args_list[0].args[2], AUTO_ANIME_MODEL)
        self.assertEqual(self.upscaler._run_model.await_args_list[1].args[2], "digital-art-4x")

    def test_span_models_are_forced_to_their_native_scale(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = self._image(directory, "input.png", (400, 300))
            self.upscaler._run_model = AsyncMock(side_effect=[False, True])

            asyncio.run(self.upscaler.upscale_image(input_path, "test", override_model=AUTO_PHOTO_MODEL, scale=2))

        self.assertEqual(self.upscaler._run_model.await_args_list[0].args[3], 4)

    def test_external_models_are_forced_to_their_native_scale(self):
        for model_name, requested_scale, expected_scale in (("animejanai-v3.1-balanced", 4, 2),):
            with self.subTest(model=model_name), tempfile.TemporaryDirectory() as directory:
                input_path = self._image(directory, "input.png", (400, 300))
                self.upscaler._run_model = AsyncMock(side_effect=[False, True])

                asyncio.run(
                    self.upscaler.upscale_image(
                        input_path,
                        "test",
                        override_model=model_name,
                        scale=requested_scale,
                    )
                )

                self.assertEqual(
                    self.upscaler._run_model.await_args_list[0].args[3], expected_scale
                )

    def test_automatic_model_selection_matches_the_safe_scale(self):
        class AnimeClassifier:
            def __init__(self, is_anime):
                self.is_anime = is_anime

            def predict_is_anime(self, _path):
                return self.is_anime

        with tempfile.TemporaryDirectory() as directory:
            two_x_input = self._image(directory, "two-x.png", (3000, 1500))
            four_x_input = self._image(directory, "four-x.png", (1000, 500))
            with patch.object(sys.modules[UpscaylUpscaler.__module__], "get_classifier", return_value=AnimeClassifier(False)):
                _, _, two_x_photo = asyncio.run(self.upscaler.check_is_low_quality(two_x_input))
                _, _, four_x_photo = asyncio.run(self.upscaler.check_is_low_quality(four_x_input))
            with patch.object(sys.modules[UpscaylUpscaler.__module__], "get_classifier", return_value=AnimeClassifier(True)):
                _, _, two_x_anime = asyncio.run(self.upscaler.check_is_low_quality(two_x_input))
                _, _, four_x_anime = asyncio.run(self.upscaler.check_is_low_quality(four_x_input))

        self.assertEqual(two_x_photo, "liveaction-v1-span-2x")
        self.assertEqual(four_x_photo, AUTO_PHOTO_MODEL)
        self.assertEqual(two_x_anime, AUTO_ANIME_MODEL)
        self.assertEqual(four_x_anime, AUTO_ANIME_MODEL)

    def test_text_ui_skips_ai_upscale(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = self._image(directory, "ui.png", (800, 600))
            decision = asyncio.run(
                self.upscaler.check_is_low_quality(
                    input_path,
                    classification_hint=ClassificationResult(
                        "text_ui", 0.96, "clip", {},
                    ),
                )
            )

        self.assertFalse(decision[0])
        self.assertIn("文字/UI", decision[1])
        self.assertIsNone(decision[2])

    def test_uncertain_uses_remacri_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = self._image(directory, "uncertain.png", (800, 600))
            decision = asyncio.run(
                self.upscaler.check_is_low_quality(
                    input_path,
                    classification_hint=ClassificationResult(
                        "uncertain", 0.55, "clip", {},
                    ),
                )
            )

        self.assertTrue(decision[0])
        self.assertEqual(decision[2], "remacri-4x")
