# ruff: noqa: E402
"""Unit tests for ImageToolMixin multi-image upscaling and transcoding."""

from __future__ import annotations

import asyncio
import base64
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# Provide mock astrbot and httpx modules if running outside AstrBot environment
if "httpx" not in sys.modules:
    httpx_mod = ModuleType("httpx")
    httpx_mod.AsyncClient = MagicMock()
    sys.modules["httpx"] = httpx_mod

if "astrbot" not in sys.modules:
    astrbot_mod = ModuleType("astrbot")
    api_mod = ModuleType("astrbot.api")
    event_mod = ModuleType("astrbot.api.event")
    comp_mod = ModuleType("astrbot.api.message_components")

    class MockAstrMessageEvent:
        pass

    class MockImage:
        def __init__(self, url=None, file=None, path=None):
            self.url = url
            self.file = file
            self.path = path

    class MockReply:
        def __init__(self, id=None, message_id=None):
            self.id = id or message_id
            self.message_id = id or message_id

    api_mod.logger = MagicMock()
    event_mod.AstrMessageEvent = MockAstrMessageEvent
    comp_mod.Image = MockImage
    comp_mod.Reply = MockReply
    astrbot_mod.api = api_mod
    api_mod.event = event_mod
    api_mod.message_components = comp_mod

    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.message_components"] = comp_mod

from astrbot.api.message_components import Image, Reply

# Ensure plugin core is importable
repo_root = Path(__file__).resolve().parents[1]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from core.common.image_tool_mixin import ImageToolMixin


class DummyEvent:
    def __init__(self, message_components=None, raw_message="", message_str=""):
        self.message_obj = SimpleNamespace(
            message=message_components or [],
            raw_message=raw_message,
        )
        self.message_str = message_str
        self.bot = None
        self.results = []

    def plain_result(self, text: str) -> str:
        return text

    def get_sender_name(self) -> str:
        return "TestUser"

    def get_sender_id(self) -> str:
        return "123456"


class MockHarness(ImageToolMixin):
    def __init__(self, cache_dir: Path):
        self._cache_dir = cache_dir
        self.image_tool_enabled = True
        self.image_tool_waiting = 0
        self.current_task_info = None
        self.media_whole_job = asyncio.Lock()
        self.ai_upscale_slot = asyncio.Lock()
        self.image_tool_model_name = "auto"
        self.image_tool_upscayl_scale = 2
        self.image_tool_upscayl_enable_taa = True
        self.image_tool_upscayl_double_pass = True
        self.image_tool_upscayl_max_resolution = 3840
        self.low_quality_threshold = 2160
        self.allow_ai_upscale_ffmpeg_concurrent = False

        self.upscaler = MagicMock()
        self.upscaler.check_is_low_quality = AsyncMock(
            return_value=(True, "二次元动漫", "digital-art-4x")
        )

        self.sent_files = []
        self.processed_files = []

    def _image_tool_cache_dir(self) -> Path:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        return self._cache_dir

    def _get_config_value(self, key: str, default=None):
        return default

    async def _prepare_image_metadata(self, path: Path, metadata: dict):
        pass

    async def _send_file_via_api(self, event, file_path: Path) -> bool:
        self.sent_files.append(file_path)
        return True

    async def _process_image_file(
        self,
        image_path: Path,
        request_id: str,
        *,
        force_upscale_model=None,
        force_upscale_type="未检测",
        force_upscale_options=None,
        generate_preview=False,
        manage_lock=True,
    ):
        self.processed_files.append(
            (image_path, force_upscale_model, force_upscale_options)
        )
        out_path = self._cache_dir / f"{image_path.stem}_upscaled.avif"
        out_path.write_bytes(b"upscaled_and_avif_content")
        return (
            out_path,
            None,
            bool(force_upscale_model),
            force_upscale_type,
            force_upscale_model,
            out_path,
            {"ai_upscale": 1.2, "transcode": 0.5, "elapsed": 1.7},
        )


class TestImageToolMixin(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.temp_dir.name)
        self.harness = MockHarness(self.cache_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_dummy_image(self, name: str, size: tuple[int, int] = (100, 100)) -> Path:
        img_path = self.cache_dir / name
        from PIL import Image as PImg
        img = PImg.new("RGB", size, color="red")
        img.save(img_path, format="PNG")
        return img_path

    async def test_extract_multiple_images_from_message(self):
        """Verify multiple Image components in a single message are all extracted."""
        img1 = self._create_dummy_image("img1.png")
        img2 = self._create_dummy_image("img2.png")
        img3 = self._create_dummy_image("img3.png")

        event = DummyEvent(
            message_components=[
                Image(url=f"file://{img1.as_posix()}"),
                Image(url=f"file://{img2.as_posix()}"),
                Image(url=f"file://{img3.as_posix()}"),
            ],
            message_str="/升图",
        )

        urls = await self.harness._extract_image_urls_for_tool(event)
        self.assertEqual(len(urls), 3)
        self.assertIn(f"file://{img1.as_posix()}", urls)
        self.assertIn(f"file://{img2.as_posix()}", urls)
        self.assertIn(f"file://{img3.as_posix()}", urls)

    async def test_extract_multiple_images_from_reply(self):
        """Verify multiple images in a replied message are extracted."""
        img1 = self._create_dummy_image("reply1.png")
        img2 = self._create_dummy_image("reply2.png")

        bot_mock = MagicMock()
        bot_mock.call_action = AsyncMock(
            return_value={
                "message": [
                    {"type": "image", "data": {"url": f"file://{img1.as_posix()}"}},
                    {"type": "image", "data": {"url": f"file://{img2.as_posix()}"}},
                ]
            }
        )

        event = DummyEvent(
            message_components=[Reply(id=99999)],
            message_str="/升图 二次元",
        )
        event.bot = bot_mock

        urls = await self.harness._extract_image_urls_for_tool(event)
        self.assertEqual(len(urls), 2)
        self.assertEqual(urls[0], f"file://{img1.as_posix()}")
        self.assertEqual(urls[1], f"file://{img2.as_posix()}")

    async def test_extract_cq_codes(self):
        """Verify multiple [CQ:image,...] codes are parsed correctly."""
        raw_msg = (
            "/升图 [CQ:image,file=abc,url=https://example.com/1.png]"
            "[CQ:image,file=https://example.com/2.png]"
        )
        event = DummyEvent(raw_message=raw_msg, message_str=raw_msg)
        urls = await self.harness._extract_image_urls_for_tool(event)
        self.assertEqual(len(urls), 2)
        self.assertEqual(urls[0], "https://example.com/1.png")
        self.assertEqual(urls[1], "https://example.com/2.png")

    async def test_run_image_tool_upscale_multiple_images(self):
        """Verify /升图 processes and sends all images and outputs multi-line annotation."""
        img1 = self._create_dummy_image("batch1.png")
        img2 = self._create_dummy_image("batch2.png")

        event = DummyEvent(
            message_components=[
                Image(url=f"file://{img1.as_posix()}"),
                Image(url=f"file://{img2.as_posix()}"),
            ],
            message_str="/升图",
        )

        results = []
        async for res in self.harness.cmd_image_tool_upscale(event):
            results.append(res)

        self.assertEqual(len(results), 2)
        self.assertIn("已加入图片处理队列", results[0])
        self.assertIn("图片 AI 升图处理标注", results[1])
        self.assertIn("图 1:", results[1])
        self.assertIn("图 2:", results[1])
        self.assertIn("耗时汇总", results[1])

        self.assertEqual(len(self.harness.processed_files), 2)
        self.assertEqual(len(self.harness.sent_files), 2)

    async def test_automatic_batch_classification_happens_before_whole_job(self):
        image1 = self._create_dummy_image("preclassify1.png")
        image2 = self._create_dummy_image("preclassify2.png")

        class BatchClassifier:
            def __init__(self, lock):
                self.lock = lock
                self.was_inside_whole_job = None
                self.released = False

            async def classify_many(self, paths):
                self.was_inside_whole_job = self.lock.locked()
                return [MagicMock() for _ in paths]

            async def release_before_upscale(self):
                self.released = True

        classifier = BatchClassifier(self.harness.media_whole_job)
        self.harness.image_classifier = classifier
        event = DummyEvent(
            message_components=[
                Image(url=f"file://{image1.as_posix()}"),
                Image(url=f"file://{image2.as_posix()}"),
            ],
            message_str="/升图",
        )

        async for _ in self.harness.cmd_image_tool_upscale(event):
            pass

        self.assertFalse(classifier.was_inside_whole_job)
        self.assertTrue(classifier.released)

    async def test_run_image_tool_with_model_override(self):
        """Verify model specified in command argument is passed to all processed images."""
        img1 = self._create_dummy_image("m1.png")
        img2 = self._create_dummy_image("m2.png")

        event = DummyEvent(
            message_components=[
                Image(url=f"file://{img1.as_posix()}"),
                Image(url=f"file://{img2.as_posix()}"),
            ],
            message_str="/升图 高保真",
        )

        results = []
        async for res in self.harness.cmd_image_tool_upscale(event):
            results.append(res)

        self.assertEqual(len(self.harness.processed_files), 2)
        for _, model, options in self.harness.processed_files:
            self.assertEqual(model, "high-fidelity-4x")
            self.assertEqual(options, (2, True, True))

    async def test_automatic_model_uses_dynamic_scale_options(self):
        """Automatic selection must let the upscaler choose and cap the scale."""
        image = self._create_dummy_image("auto.png")
        event = DummyEvent(
            message_components=[Image(url=f"file://{image.as_posix()}")],
            message_str="/升图",
        )

        async for _ in self.harness.cmd_image_tool_upscale(event):
            pass

        _, model, options = self.harness.processed_files[0]
        self.assertEqual(model, "digital-art-4x")
        self.assertIsNone(options)

    async def test_run_image_tool_avif_command(self):
        """Verify /avif transcodes multiple images without upscale."""
        img1 = self._create_dummy_image("avif1.png")
        img2 = self._create_dummy_image("avif2.png")

        event = DummyEvent(
            message_components=[
                Image(url=f"file://{img1.as_posix()}"),
                Image(url=f"file://{img2.as_posix()}"),
            ],
            message_str="/avif",
        )

        results = []
        async for res in self.harness.cmd_image_tool_avif(event):
            results.append(res)

        self.assertEqual(len(self.harness.processed_files), 2)
        for _, model, _ in self.harness.processed_files:
            self.assertIsNone(model)

    async def test_download_tool_image_decodes_base64_urls(self):
        """Verify image data URLs and OneBot base64 URLs bypass HTTP downloads."""
        image_path = self._create_dummy_image("base64-source.png")
        image_bytes = image_path.read_bytes()
        encoded = base64.b64encode(image_bytes).decode("ascii")

        for url in (f"data:image/png;base64,{encoded}", f"base64://{encoded}"):
            decoded_path = await self.harness._download_tool_image(url)
            self.assertEqual(decoded_path.read_bytes(), image_bytes)

    async def test_run_image_tool_resolution_limit(self):
        """Verify image exceeding max resolution skips upscale."""
        small_img = self._create_dummy_image("small.png", size=(500, 500))
        large_img = self._create_dummy_image("large.png", size=(5000, 3000))

        self.harness.image_tool_upscayl_max_resolution = 4000

        event = DummyEvent(
            message_components=[
                Image(url=f"file://{small_img.as_posix()}"),
                Image(url=f"file://{large_img.as_posix()}"),
            ],
            message_str="/升图",
        )

        results = []
        async for res in self.harness.cmd_image_tool_upscale(event):
            results.append(res)

        self.assertEqual(len(self.harness.processed_files), 2)
        # First image gets model, second exceeds limit
        self.assertEqual(self.harness.processed_files[0][1], "digital-art-4x")
        self.assertIsNone(self.harness.processed_files[1][1])

    async def test_run_image_tool_no_images(self):
        """Verify prompt when no image is supplied."""
        event = DummyEvent(message_components=[], message_str="/升图")
        results = []
        async for res in self.harness.cmd_image_tool_upscale(event):
            results.append(res)
        self.assertEqual(len(results), 2)
        self.assertIn("请发送图片或引用", results[1])

    async def test_backward_compatible_extract_single(self):
        """Verify _extract_image_url_for_tool still returns the first image url."""
        img1 = self._create_dummy_image("single1.png")
        img2 = self._create_dummy_image("single2.png")
        event = DummyEvent(
            message_components=[
                Image(url=f"file://{img1.as_posix()}"),
                Image(url=f"file://{img2.as_posix()}"),
            ]
        )
        single_url = await self.harness._extract_image_url_for_tool(event)
        self.assertEqual(single_url, f"file://{img1.as_posix()}")


if __name__ == "__main__":
    unittest.main()
