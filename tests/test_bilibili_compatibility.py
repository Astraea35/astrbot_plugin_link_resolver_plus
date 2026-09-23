# ruff: noqa: E402
"""B站 API 响应兼容性回归测试."""

from __future__ import annotations

import sys
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

for candidate in Path(__file__).resolve().parents:
    if (candidate / "data" / "plugins").exists():
        root_path = str(candidate)
        if root_path not in sys.path:
            sys.path.insert(0, root_path)
        break

from data.plugins.astrbot_plugin_link_resolver.core.bilibili.handler import (
    BILI_MESSAGE_PATTERN,
    BilibiliMixin,
)


class TestBilibiliCompatibility(unittest.TestCase):
    """验证 bilibili-api-python 不同响应形态下的兼容逻辑."""

    def test_schemeless_short_link_and_identifier_boundaries(self):
        harness = BilibiliMixin()
        self.assertRegex("b23.tv/abcdef", BILI_MESSAGE_PATTERN)
        self.assertIn(
            "https://b23.tv/abcdef",
            harness.extract_links_from_text("b23.tv/abcdef"),
        )
        self.assertIsNone(re.search(BILI_MESSAGE_PATTERN, "prefix_BV1xx411c7mD_suffix"))

    def test_none_current_quality_has_no_lower_quality_candidates(self):
        harness = BilibiliMixin.__new__(BilibiliMixin)
        harness.allow_hdr = False
        harness.allow_dolby = False

        self.assertEqual(
            harness._get_lower_qualities(SimpleNamespace(value=None)),
            [],
        )

    def test_normalizes_hvc1_and_hev1_for_detector(self):
        payload = {
            "dash": {
                "video": [
                    {"codecs": "hvc1.1.6.L120.90"},
                    {"codecs": "hev1.1.6.L120.90"},
                    {"codecs": "avc1.640032"},
                ]
            }
        }

        BilibiliMixin._normalize_bili_codecs_for_detector(payload)

        codecs = [item["codecs"] for item in payload["dash"]["video"]]
        self.assertEqual(codecs[0], "hev hvc1.1.6.L120.90")
        self.assertEqual(codecs[1], "hev hev1.1.6.L120.90")
        self.assertEqual(codecs[2], "avc1.640032")


if __name__ == "__main__":
    unittest.main(verbosity=2)
