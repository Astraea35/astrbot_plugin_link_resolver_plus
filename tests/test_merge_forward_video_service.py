"""Verify merged-forward videos use callback URLs when available.

Run inside AstrBot container:
    cd /AstrBot
    python /AstrBot/data/plugins/astrbot_plugin_link_resolver/tests/test_merge_forward_video_service.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

for candidate in Path(__file__).resolve().parents:
    if (candidate / "data" / "plugins").exists():
        root_path = str(candidate)
        if root_path not in sys.path:
            sys.path.insert(0, root_path)
        break

from astrbot.api.message_components import Image, Video

from data.plugins.astrbot_plugin_link_resolver.main import LinkResolver


class TestMergeForwardVideoService(unittest.IsolatedAsyncioTestCase):
    async def test_local_video_is_converted_to_callback_url(self):
        component = Video.fromFileSystem("/tmp/demo.mp4")

        with patch.object(
            Video,
            "register_to_file_service",
            new=AsyncMock(return_value="http://astrbot:6185/api/file/token123"),
        ) as register_mock:
            converted = await LinkResolver._prepare_component_for_merge_send(
                None, component
            )

        self.assertIsInstance(converted, Video)
        self.assertEqual(converted.file, "http://astrbot:6185/api/file/token123")
        self.assertEqual(
            converted.toDict()["data"]["file"], "http://astrbot:6185/api/file/token123"
        )
        register_mock.assert_awaited_once()

    async def test_non_video_component_is_left_unchanged(self):
        component = Image.fromFileSystem("/tmp/demo.jpg")

        converted = await LinkResolver._prepare_component_for_merge_send(None, component)

        self.assertIs(converted, component)

    async def test_failed_registration_keeps_original_component(self):
        component = Video.fromFileSystem("/tmp/demo.mp4")

        with patch.object(
            Video,
            "register_to_file_service",
            new=AsyncMock(side_effect=RuntimeError("callback disabled")),
        ) as register_mock:
            converted = await LinkResolver._prepare_component_for_merge_send(
                None, component
            )

        self.assertIs(converted, component)
        self.assertTrue(converted.file.startswith("file:///"))
        register_mock.assert_awaited_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
