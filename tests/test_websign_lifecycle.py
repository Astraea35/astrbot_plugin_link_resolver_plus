"""Dependency-free checks for the upstream signing and file lifecycle helpers."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ROOT = Path(__file__).resolve().parents[1] / "core"
websign = _load_module("test_local_websign", ROOT / "douyin" / "websign.py")
lifecycle = _load_module(
    "test_local_file_lifecycle", ROOT / "common" / "file_lifecycle.py"
)


class TestWebSign(unittest.TestCase):
    def test_sign_covers_encoded_query_and_returns_matching_header(self):
        query = websign.encode_pairs([("aweme_id", "123"), ("a_bogus", "a+b/=")])
        signed = websign.sign(query, "guest-id", timestamp=1700000000)
        params = parse_qs(signed.query)

        self.assertEqual(params["a_bogus"], ["a+b/="])
        self.assertEqual(params["uifid"], ["guest-id"])
        self.assertEqual(params["timestamp"], ["1700000000"])
        self.assertEqual(params["x-secsdk-web-signature"], [signed.signature])
        self.assertEqual(len(signed.signature), 32)
        self.assertEqual(signed, websign.sign(query, "guest-id", timestamp=1700000000))


class TestCardFileLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_waits_for_thread_then_removes_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "card.png"
            started = threading.Event()
            release = threading.Event()

            class SlowImage:
                def save(self, output_path):
                    started.set()
                    release.wait(timeout=5)
                    output_path.write_bytes(b"card")

            task = asyncio.create_task(lifecycle.save_image_file(SlowImage(), path))
            await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=5)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(path.exists())

    async def test_failed_save_removes_partial_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "card.png"

            class FailingImage:
                def save(self, output_path):
                    output_path.write_bytes(b"partial")
                    raise OSError("save failed")

            with self.assertRaisesRegex(OSError, "save failed"):
                await lifecycle.save_image_file(FailingImage(), path)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
