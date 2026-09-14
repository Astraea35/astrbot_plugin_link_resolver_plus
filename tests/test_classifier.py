# ruff: noqa: E402
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

try:
    import httpx  # noqa: F401
except ModuleNotFoundError:
    httpx_module = types.ModuleType("httpx")

    class Timeout:
        def __init__(self, *args, **kwargs):
            pass

    class AsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def stream(self, *args, **kwargs):
            raise AssertionError("Network access is not expected in classifier tests")

    httpx_module.Timeout = Timeout
    httpx_module.AsyncClient = AsyncClient
    sys.modules["httpx"] = httpx_module

astrbot_module = types.ModuleType("astrbot")
astrbot_api_module = types.ModuleType("astrbot.api")
astrbot_api_module.logger = MagicMock()
astrbot_module.api = astrbot_api_module
sys.modules.setdefault("astrbot", astrbot_module)
sys.modules.setdefault("astrbot.api", astrbot_api_module)

event_module = sys.modules.setdefault(
    "astrbot.api.event", types.ModuleType("astrbot.api.event")
)
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

sys.modules["astrbot"].api = sys.modules["astrbot.api"]
sys.modules["astrbot.api"].event = event_module
sys.modules["astrbot.api"].message_components = components_module

from core.common.media.classifier import (
    CHECKSUMS_FILE_NAME,
    DOWNLOAD_ASSETS,
    MODEL_FILE_NAME,
    PROTOTYPES_FILE_NAME,
    ClassificationResult,
    HybridImageClassifier,
    RuleV2Classifier,
)


class DummyPlugin:
    hybrid_classifier_enabled = True
    classifier_auto_download = False
    classifier_provider = "DirectML"
    classifier_idle_unload_seconds = 30
    classifier_clip_min_probability = 0.60
    classifier_clip_min_margin = 0.10
    classifier_clip_logit_scale = 100.0
    classifier_release_base_url = "https://example.invalid/classifier"
    ai_upscale_slot = None


def uncertain_result() -> ClassificationResult:
    return ClassificationResult(
        "uncertain", 0.55, "rule_v2", {"anime_score": 0.45}, "rule-v2"
    )


def write_resources(directory: Path) -> None:
    (directory / MODEL_FILE_NAME).write_bytes(b"test-model")
    prototypes = {}
    for index, kind in enumerate(("anime", "photo", "text_ui")):
        vector = [0.0] * 512
        vector[index] = 1.0
        prototypes[kind] = vector
    (directory / PROTOTYPES_FILE_NAME).write_text(
        json.dumps({"prototypes": prototypes}), encoding="utf-8"
    )


class FakeSession:
    def __init__(self, embedding=None, delay=0.0):
        self.embedding = embedding or [1.0] + [0.0] * 511
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self._lock = threading.Lock()

    def run(self, output_names, inputs):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls += 1
        try:
            if self.delay:
                time.sleep(self.delay)
            return [np.asarray([self.embedding], dtype=np.float32)]
        finally:
            with self._lock:
                self.active -= 1


class RecordingGate:
    def __init__(self):
        self.active = 0
        self.enters = 0

    async def __aenter__(self):
        self.active += 1
        self.enters += 1

    async def __aexit__(self, exc_type, exc, traceback):
        self.active -= 1


class RaisingSession(FakeSession):
    def run(self, output_names, inputs):
        raise RuntimeError("DirectML test failure")


class GateCheckingSession(FakeSession):
    def __init__(self, gate: RecordingGate):
        super().__init__()
        self.gate = gate

    def run(self, output_names, inputs):
        if self.gate.active:
            raise AssertionError("CPU fallback ran while the DirectML slot was held")
        return super().run(output_names, inputs)


class TestRuleV2Classifier(unittest.TestCase):
    def test_rule_thresholds(self):
        classifier = RuleV2Classifier()
        base = {
            "ui_score": 0.0,
            "text_density": 0.0,
            "rectangle_density": 0.0,
        }
        for score, expected in ((0.35, "photo"), (0.50, "uncertain"), (0.65, "anime")):
            with self.subTest(score=score), patch.object(
                classifier, "extract_features", return_value={**base, "anime_score": score}
            ):
                result = classifier.classify_array(np.zeros((8, 8, 3), dtype=np.uint8))
                self.assertEqual(result.kind, expected)

    def test_text_ui_is_detected_without_clip(self):
        image = np.full((720, 1080, 3), 245, dtype=np.uint8)
        for y in range(70, 650, 80):
            cv2.rectangle(image, (60, y), (1020, y + 55), (225, 225, 225), 2)
            cv2.putText(
                image,
                "Settings  Example text  12345",
                (85, y + 37),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.75,
                (25, 25, 25),
                2,
                cv2.LINE_AA,
            )
        result = RuleV2Classifier().classify_array(image)
        self.assertEqual(result.kind, "text_ui")
        self.assertGreaterEqual(result.confidence, 0.72)

    def test_invalid_image_is_uncertain(self):
        result = RuleV2Classifier().classify_array(None)
        self.assertEqual(result.kind, "uncertain")
        self.assertEqual(result.features["read_error"], 1.0)


class TestHybridClassifier(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.model_dir = Path(self.temp.name)
        write_resources(self.model_dir)
        self.plugin = DummyPlugin()
        self.classifier = HybridImageClassifier(
            self.plugin,
            model_dir=self.model_dir,
            asset_hashes=self._asset_hashes_for(self.model_dir),
        )

    async def asyncTearDown(self):
        await self.classifier.shutdown(timeout=0.2)
        self.temp.cleanup()

    @staticmethod
    def _image(path: Path) -> Path:
        cv2.imwrite(str(path), np.full((32, 32, 3), 127, dtype=np.uint8))
        return path

    @staticmethod
    def _asset_hashes_for(directory: Path) -> dict[str, str]:
        return {
            name: (
                hashlib.sha256((directory / name).read_bytes()).hexdigest()
                if (directory / name).is_file()
                else "0" * 64
            )
            for name in DOWNLOAD_ASSETS
        }

    def _install_fake_runtime(self, session: FakeSession) -> None:
        self.classifier._session = session
        self.classifier._provider = (
            "DmlExecutionProvider"
            if self.plugin.classifier_provider.lower().startswith("direct")
            else "CPUExecutionProvider"
        )
        self.classifier._prototypes = self.classifier._load_prototypes(
            self.classifier.prototypes_path
        )
        self.classifier._executor = ThreadPoolExecutor(max_workers=1)
        resource_key = self.classifier._resource_key()
        self.classifier._validated_resource_key = resource_key
        self.classifier._session_key = (
            *resource_key,
            "directml"
            if self.plugin.classifier_provider.lower().startswith("direct")
            else "cpu",
        )
        self.classifier.rule.classify_many = MagicMock(
            side_effect=lambda paths: [uncertain_result() for _ in paths]
        )
        self.classifier._preprocess_image = MagicMock(
            return_value=np.zeros((1, 3, 224, 224), dtype=np.float16)
        )

    @staticmethod
    def _download_fixture() -> tuple[dict[str, bytes], dict[str, str], str, bytes]:
        contents = {
            MODEL_FILE_NAME: b"downloaded-model",
            PROTOTYPES_FILE_NAME: b'{"prototypes": {}}',
            "CLIP_LICENSE.txt": b"license text",
        }
        hashes = {
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in contents.items()
        }
        manifest = "\n".join(
            f"{hashes[name]}  {name}" for name in DOWNLOAD_ASSETS
        ).encode()
        return contents, hashes, hashlib.sha256(manifest).hexdigest(), manifest

    def test_clip_confidence_and_margin(self):
        self.classifier._prototypes = self.classifier._load_prototypes(
            self.classifier.prototypes_path
        )
        anime = np.zeros(512, dtype=np.float32)
        anime[0] = 1.0
        result = self.classifier._result_from_embedding(anime, uncertain_result())
        self.assertEqual(result.kind, "anime")
        self.assertGreaterEqual(result.confidence, 0.60)

        ambiguous = np.zeros(512, dtype=np.float32)
        ambiguous[0] = ambiguous[1] = 1 / np.sqrt(2)
        result = self.classifier._result_from_embedding(ambiguous, uncertain_result())
        self.assertEqual(result.kind, "uncertain")
        self.assertLess(result.features["clip_margin"], 0.10)

    async def test_concurrent_load_creates_one_session(self):
        calls = 0

        def create_session(path, provider):
            nonlocal calls
            calls += 1
            time.sleep(0.03)
            return FakeSession(), "CPUExecutionProvider"

        with patch.object(self.classifier, "_create_session", side_effect=create_session):
            sessions = await asyncio.gather(
                self.classifier._ensure_session(),
                self.classifier._ensure_session(),
                self.classifier._ensure_session(),
            )
        self.assertEqual(calls, 1)
        self.assertTrue(all(session is sessions[0] for session in sessions))

    async def test_session_run_is_serialized(self):
        session = FakeSession(delay=0.04)
        self._install_fake_runtime(session)
        first = self._image(self.model_dir / "first.png")
        second = self._image(self.model_dir / "second.png")

        results = await asyncio.gather(
            self.classifier.classify(first), self.classifier.classify(second)
        )

        self.assertEqual(session.calls, 2)
        self.assertEqual(session.max_active, 1)
        self.assertEqual([result.kind for result in results], ["anime", "anime"])

    async def test_idle_timer_unloads_and_new_request_renews_it(self):
        self.plugin.classifier_idle_unload_seconds = 0.05
        session = FakeSession()
        self._install_fake_runtime(session)
        image = self._image(self.model_dir / "idle.png")

        await self.classifier.classify(image)
        await asyncio.sleep(0.03)
        await self.classifier.classify(image)
        await asyncio.sleep(0.03)
        self.assertIs(self.classifier._session, session)
        await asyncio.sleep(0.20)
        self.assertIsNone(self.classifier._session)

    async def test_inference_cannot_be_unloaded(self):
        session = FakeSession(delay=0.08)
        self._install_fake_runtime(session)
        image = self._image(self.model_dir / "busy.png")

        task = asyncio.create_task(self.classifier.classify(image))
        await asyncio.sleep(0.02)
        await self.classifier.release()
        self.assertIs(self.classifier._session, session)
        await task

    async def test_corrupt_prototypes_fall_back_to_rule(self):
        (self.model_dir / PROTOTYPES_FILE_NAME).write_text("{}", encoding="utf-8")
        image = self._image(self.model_dir / "corrupt.png")
        self.classifier.rule.classify_many = MagicMock(return_value=[uncertain_result()])

        result = await self.classifier.classify(image)

        self.assertEqual(result.kind, "uncertain")
        self.assertEqual(result.source, "rule_v2")
        self.assertIsNone(self.classifier._session)

    async def test_download_checksum_failure_removes_bad_asset(self):
        for name in (MODEL_FILE_NAME, PROTOTYPES_FILE_NAME):
            (self.model_dir / name).unlink(missing_ok=True)

        async def fake_download(client, url, path):
            temporary = path.with_suffix(path.suffix + ".part")
            if path.name == CHECKSUMS_FILE_NAME:
                temporary.write_text(
                    "\n".join(f"{'0' * 64}  {name}" for name in DOWNLOAD_ASSETS),
                    encoding="utf-8",
                )
            else:
                temporary.write_bytes(b"wrong-content")
            return temporary

        with patch.object(self.classifier, "_download_file", side_effect=fake_download):
            await self.classifier._download_resources()

        self.assertFalse((self.model_dir / MODEL_FILE_NAME).exists())

    async def test_pinned_hashes_reject_same_origin_forged_manifest(self):
        for name in (*DOWNLOAD_ASSETS, CHECKSUMS_FILE_NAME):
            (self.model_dir / name).unlink(missing_ok=True)

        forged_contents, forged_hashes, forged_manifest_hash, forged_manifest = (
            self._download_fixture()
        )
        forged_contents[MODEL_FILE_NAME] = b"attacker controlled model"
        forged_hashes[MODEL_FILE_NAME] = hashlib.sha256(
            forged_contents[MODEL_FILE_NAME]
        ).hexdigest()
        forged_manifest = "\n".join(
            f"{forged_hashes[name]}  {name}" for name in DOWNLOAD_ASSETS
        ).encode()
        forged_manifest_hash = hashlib.sha256(forged_manifest).hexdigest()
        _, expected_hashes, expected_manifest_hash, _ = (
            self._download_fixture()
        )
        self.assertNotEqual(forged_manifest_hash, expected_manifest_hash)
        self.assertNotEqual(forged_hashes[MODEL_FILE_NAME], expected_hashes[MODEL_FILE_NAME])
        classifier = HybridImageClassifier(
            self.plugin,
            model_dir=self.model_dir,
            asset_hashes=expected_hashes,
            manifest_sha256=expected_manifest_hash,
        )
        calls = []

        async def fake_download(client, url, path):
            calls.append(path.name)
            temporary = path.with_name(f".{path.name}.forged.part")
            temporary.write_bytes(
                forged_manifest
                if path.name == CHECKSUMS_FILE_NAME
                else forged_contents[path.name]
            )
            return temporary

        with patch.object(classifier, "_download_file", side_effect=fake_download):
            await classifier._download_resources()

        self.assertEqual(calls, [CHECKSUMS_FILE_NAME])
        self.assertFalse((self.model_dir / CHECKSUMS_FILE_NAME).exists())
        self.assertFalse((self.model_dir / MODEL_FILE_NAME).exists())

    async def test_session_rebuilds_after_provider_path_or_mtime_change(self):
        dynamic_dir = self.model_dir / "dynamic"
        next_dir = self.model_dir / "next"
        dynamic_dir.mkdir()
        next_dir.mkdir()
        write_resources(dynamic_dir)
        write_resources(next_dir)
        self.plugin.classifier_models_path = str(dynamic_dir)
        self.plugin.classifier_provider = "CPU"
        classifier = HybridImageClassifier(
            self.plugin,
            asset_hashes=self._asset_hashes_for(dynamic_dir),
        )
        created = []

        def create_session(path, provider):
            session = FakeSession()
            created.append((Path(path), provider, session))
            return session, (
                "DmlExecutionProvider" if provider == "directml" else "CPUExecutionProvider"
            )

        try:
            with patch.object(classifier, "_create_session", side_effect=create_session):
                first = await classifier._ensure_session()
                self.assertIs(first, await classifier._ensure_session())

                self.plugin.classifier_provider = "DirectML"
                provider_changed = await classifier._ensure_session()

                stat = classifier.model_path.stat()
                os.utime(
                    classifier.model_path,
                    ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000),
                )
                mtime_changed = await classifier._ensure_session()

                self.plugin.classifier_models_path = str(next_dir)
                classifier._expected_hashes = self._asset_hashes_for(next_dir)
                path_changed = await classifier._ensure_session()

            self.assertEqual(len(created), 4)
            self.assertIsNot(first, provider_changed)
            self.assertIsNot(provider_changed, mtime_changed)
            self.assertIsNot(mtime_changed, path_changed)
            self.assertEqual(created[-1][0], next_dir / MODEL_FILE_NAME)
        finally:
            await classifier.shutdown(timeout=0.2)

    async def test_cpu_provider_does_not_acquire_upscale_slot(self):
        gate = RecordingGate()
        self.plugin.ai_upscale_slot = gate
        self.plugin.classifier_provider = "CPU"
        self._install_fake_runtime(FakeSession())
        self.classifier._provider = "CPUExecutionProvider"

        result = await self.classifier.classify(self._image(self.model_dir / "cpu.png"))

        self.assertEqual(result.kind, "anime")
        self.assertEqual(gate.enters, 0)

    async def test_directml_acquires_upscale_slot(self):
        gate = RecordingGate()
        self.plugin.ai_upscale_slot = gate
        self._install_fake_runtime(FakeSession())

        result = await self.classifier.classify(
            self._image(self.model_dir / "directml.png")
        )

        self.assertEqual(result.kind, "anime")
        self.assertEqual(gate.enters, 1)

    async def test_cpu_fallback_releases_upscale_slot_before_cpu_inference(self):
        gate = RecordingGate()
        self.plugin.ai_upscale_slot = gate
        self.plugin.classifier_provider = "DirectML"
        self.classifier.rule.classify_many = MagicMock(return_value=[uncertain_result()])
        self.classifier._preprocess_image = MagicMock(
            return_value=np.zeros((1, 3, 224, 224), dtype=np.float16)
        )

        def create_session(path, provider):
            if provider == "directml":
                return RaisingSession(), "DmlExecutionProvider"
            return GateCheckingSession(gate), "CPUExecutionProvider"

        with patch.object(self.classifier, "_create_session", side_effect=create_session):
            result = await self.classifier.classify(
                self._image(self.model_dir / "fallback.png")
            )

        self.assertEqual(result.kind, "anime")
        self.assertEqual(gate.enters, 1)

    async def test_release_before_upscale_clears_runtime(self):
        self._install_fake_runtime(FakeSession())
        await self.classifier.release_before_upscale()
        self.assertIsNone(self.classifier._session)
        self.assertIsNone(self.classifier._executor)

    async def test_shutdown_rejects_new_clip_reviews(self):
        session = FakeSession()
        self._install_fake_runtime(session)
        image = self._image(self.model_dir / "shutdown.png")

        await self.classifier.shutdown(timeout=0.2)
        result = await self.classifier.classify(image)

        self.assertEqual(result.source, "rule_v2")
        self.assertEqual(session.calls, 0)
        self.assertIsNone(self.classifier._session)

    async def test_shutdown_timeout_releases_when_inference_finishes(self):
        session = FakeSession(delay=0.08)
        self._install_fake_runtime(session)
        image = self._image(self.model_dir / "shutdown-timeout.png")

        inference = asyncio.create_task(self.classifier.classify(image))
        await asyncio.sleep(0.02)
        await self.classifier.shutdown(timeout=0.01)

        self.assertIs(self.classifier._session, session)
        await inference
        self.assertIsNone(self.classifier._session)
        self.assertIsNone(self.classifier._executor)

    async def test_concurrent_downloads_use_one_directory_lock_and_unique_parts(self):
        for name in (*DOWNLOAD_ASSETS, CHECKSUMS_FILE_NAME):
            (self.model_dir / name).unlink(missing_ok=True)
        contents, hashes, manifest_hash, manifest = self._download_fixture()
        first = HybridImageClassifier(
            self.plugin,
            model_dir=self.model_dir,
            asset_hashes=hashes,
            manifest_sha256=manifest_hash,
        )
        second = HybridImageClassifier(
            self.plugin,
            model_dir=self.model_dir,
            asset_hashes=hashes,
            manifest_sha256=manifest_hash,
        )
        calls = []
        temporary_paths = []

        async def fake_download(client, url, path):
            await asyncio.sleep(0.02)
            temporary = path.with_name(
                f".{path.name}.{len(temporary_paths)}.test.part"
            )
            temporary_paths.append(temporary)
            calls.append(path.name)
            temporary.write_bytes(
                manifest if path.name == CHECKSUMS_FILE_NAME else contents[path.name]
            )
            return temporary

        with patch.object(HybridImageClassifier, "_download_file", side_effect=fake_download):
            await asyncio.gather(first._download_resources(), second._download_resources())

        self.assertEqual(calls.count(MODEL_FILE_NAME), 1)
        self.assertEqual(calls.count(PROTOTYPES_FILE_NAME), 1)
        self.assertEqual(calls.count("CLIP_LICENSE.txt"), 1)
        self.assertEqual(len(temporary_paths), len(set(temporary_paths)))
        self.assertFalse((self.model_dir / ".classifier-download.lock").exists())

    async def test_shutdown_cancels_download_and_waits_for_active_inference(self):
        session = FakeSession(delay=0.08)
        self._install_fake_runtime(session)
        image = self._image(self.model_dir / "shutdown-active.png")
        inference = asyncio.create_task(self.classifier.classify(image))
        await asyncio.sleep(0.01)

        download_dir = self.model_dir / "download-shutdown"
        downloader = HybridImageClassifier(self.plugin, model_dir=download_dir)
        download_started = asyncio.Event()
        download_cancelled = asyncio.Event()

        async def blocked_download(client, url, path):
            download_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                download_cancelled.set()
                raise

        with patch.object(downloader, "_download_file", side_effect=blocked_download):
            self.plugin.classifier_auto_download = True
            download_task = downloader.start_background_download()
            await asyncio.wait_for(download_started.wait(), timeout=0.2)
            await asyncio.gather(
                self.classifier.shutdown(timeout=0.5), downloader.shutdown(timeout=0.5)
            )

        result = await inference
        self.assertEqual(result.kind, "anime")
        self.assertTrue(download_cancelled.is_set())
        self.assertTrue(download_task.done())
        self.assertIsNone(self.classifier._session)


class TestChecksumHelpers(unittest.TestCase):
    def test_parse_checksum_manifest(self):
        digest = hashlib.sha256(b"model").hexdigest()
        parsed = HybridImageClassifier._parse_checksums(
            f"{digest}  nested/{MODEL_FILE_NAME}\n"
        )
        self.assertEqual(parsed[MODEL_FILE_NAME], digest)


if __name__ == "__main__":
    unittest.main()
