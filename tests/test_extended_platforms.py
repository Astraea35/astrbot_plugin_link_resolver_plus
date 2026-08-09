import re
import unittest

from core.extended_platforms.specs import (
    EXTENDED_MESSAGE_PATTERN,
    EXTENDED_PLATFORM_LABELS,
    find_extended_platform,
)


class TestExtendedPlatforms(unittest.TestCase):
    def test_all_requested_platforms_are_exposed(self):
        expected = {
            '快手', '视频号', '知乎', '小黑盒', 'A站', 'YouTube', 'TikTok',
            'Instagram', 'Pixiv', 'Iwara', '网易云', 'NGA',
        }
        self.assertTrue(expected.issubset(EXTENDED_PLATFORM_LABELS))

    def test_url_is_mapped_to_platform(self):
        platform = find_extended_platform('https://www.youtube.com/watch?v=abc')

        self.assertIsNotNone(platform)
        self.assertEqual(platform.label, 'YouTube')

    def test_short_domain_and_message_pattern_are_supported(self):
        url = 'https://youtu.be/abc123'

        self.assertEqual(find_extended_platform(url).label, 'YouTube')
        self.assertIsNotNone(re.search(EXTENDED_MESSAGE_PATTERN, f'看看这个 {url}'))

    def test_unknown_url_is_not_claimed(self):
        self.assertIsNone(find_extended_platform('https://example.com/video/1'))


if __name__ == '__main__':
    unittest.main()
