# ruff: noqa: E402
"""Regression tests for the upstream Douyin guest API and media fallbacks."""

from __future__ import annotations

import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

for candidate in Path(__file__).resolve().parents:
    if (candidate / "core").exists():
        root_path = str(candidate)
        if root_path not in sys.path:
            sys.path.insert(0, root_path)
        break

try:
    import aiohttp  # noqa: F401
    import httpx
    import msgspec  # noqa: F401
    from gmssl import sm3  # noqa: F401
except ModuleNotFoundError:
    DEPENDENCIES_AVAILABLE = False
else:
    DEPENDENCIES_AVAILABLE = True
    try:
        import astrbot.api  # noqa: F401
    except ModuleNotFoundError:
        astrbot = types.ModuleType("astrbot")
        api = types.ModuleType("astrbot.api")
        api.logger = types.SimpleNamespace(debug=lambda *args, **kwargs: None)
        astrbot.api = api
        sys.modules["astrbot"] = astrbot
        sys.modules["astrbot.api"] = api

    from core.douyin import DouyinExtractor
    from core.douyin import DOUYIN_MESSAGE_PATTERN, extract_douyin_links
    from core.douyin.guest_api import DouyinGuestAPI, GuestRequest, GuestSession
    from core.xiaohongshu import (
        XHS_MESSAGE_PATTERN,
        XiaohongshuExtractor,
        extract_xhs_links,
    )


@unittest.skipUnless(DEPENDENCIES_AVAILABLE, "project dependencies are not installed")
class TestDouyinGuestSession(unittest.IsolatedAsyncioTestCase):
    def test_modal_link_is_detected_and_parsed(self):
        link = "https://www.douyin.com/?from=share&modal_id=123456789"
        self.assertRegex(link, DOUYIN_MESSAGE_PATTERN)
        self.assertEqual(extract_douyin_links(link), [link])
        self.assertEqual(DouyinExtractor()._match_type_and_id(link), (None, "123456789"))

    def test_xhs_profile_note_link_is_recognized(self):
        link = "https://www.xiaohongshu.com/user/profile/user123/note456"
        self.assertIsNotNone(re.search(XHS_MESSAGE_PATTERN, link))
        self.assertEqual(extract_xhs_links(link), [link])

    async def test_request_includes_guest_websign_and_source_referer(self):
        api = DouyinGuestAPI()
        source = "https://www.douyin.com/video/123"
        request = await api._build_request("123", GuestSession("tt", "guest"), source)
        params = parse_qs(urlparse(request.endpoint).query)
        self.assertEqual(params["uifid"], ["guest"])
        self.assertEqual(
            params["x-secsdk-web-signature"],
            [request.headers["x-secsdk-web-signature"]],
        )
        self.assertEqual(request.headers["Referer"], source)
        self.assertEqual(request.headers["uifid"], "guest")

    async def test_guest_api_refreshes_session_after_empty_response(self):
        api = DouyinGuestAPI()
        sessions = iter(
            [GuestSession("old", "old-fingerprint"), GuestSession("new", "new-fingerprint")]
        )
        created: list[GuestSession] = []
        responses = iter(
            [
                httpx.Response(200, content=b""),
                httpx.Response(200, json={"aweme_detail": {"aweme_id": "123"}}),
            ]
        )

        async def fake_create_session():
            session = next(sessions)
            created.append(session)
            return session

        async def fake_build_request(_aweme_id, session, source_url):
            self.assertIsNone(source_url)
            return GuestRequest(
                "https://example.test/detail",
                {"uifid": session.uifid},
            )

        async def fake_get(_client, _url, **_kwargs):
            return next(responses)

        with (
            patch.object(api, "_create_session", new=fake_create_session),
            patch.object(api, "_build_request", new=fake_build_request),
            patch("core.douyin.guest_api.httpx.AsyncClient.get", new=fake_get),
        ):
            detail = await api.fetch_detail("123")

        self.assertEqual(detail["aweme_id"], "123")
        self.assertEqual([session.ttwid for session in created], ["old", "new"])

    def test_video_candidates_prefer_highest_quality(self):
        video = {
            "bit_rate": [
                {
                    "bit_rate": 800_000,
                    "play_addr": {
                        "width": 720,
                        "height": 1280,
                        "url_list": ["https://example.com/720p"],
                    },
                },
                {
                    "bit_rate": 2_000_000,
                    "play_addr": {
                        "width": 1080,
                        "height": 1920,
                        "url_list": ["https://example.com/1080p"],
                    },
                },
            ]
        }

        urls = DouyinExtractor._select_highest_quality_video_urls(
            video, {"url_list": ["https://example.com/default"]}
        )

        self.assertEqual(
            urls,
            [
                "https://example.com/1080p",
                "https://example.com/720p",
                "https://example.com/default",
            ],
        )

    def test_xhs_media_candidates_keep_indices_aligned(self):
        extractor = XiaohongshuExtractor()
        result = extractor._build_result_from_note(
            {
                "imageList": [
                    {"fileId": "ignored-without-image"},
                    {
                        "urlDefault": "https://example.com/1.jpg!small",
                        "urlPre": "https://backup.example.com/1.jpg",
                        "fileId": "image-one",
                    },
                    {
                        "urlDefault": "https://example.com/2.jpg",
                        "fileId": "image-two",
                        "stream": {
                            "h264": [
                                {
                                    "masterUrl": "https://example.com/2.mp4",
                                    "backupUrls": [
                                        "https://backup.example.com/2.mp4"
                                    ],
                                }
                            ]
                        },
                    },
                ]
            },
            "https://www.xiaohongshu.com/explore/example",
        )

        self.assertEqual(
            result.image_urls,
            ["https://example.com/1.jpg", "https://example.com/2.jpg"],
        )
        self.assertEqual(result.file_ids, ["image-one", "image-two"])
        self.assertEqual(
            result.image_url_candidates[0],
            ["https://example.com/1.jpg", "https://backup.example.com/1.jpg"],
        )
        self.assertEqual(result.live_photo_urls, [None, "https://example.com/2.mp4"])
        self.assertEqual(
            result.live_photo_url_candidates[1],
            ["https://example.com/2.mp4", "https://backup.example.com/2.mp4"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
