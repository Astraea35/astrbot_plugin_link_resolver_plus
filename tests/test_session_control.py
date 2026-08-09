import unittest

from core.common.base_mixin import BaseUtilsMixin


class DummyEvent:
    def __init__(self, message_str='https://example.com/post/1', group_id='12345', sender_id='10001'):
        self.message_str = message_str
        self._group_id = group_id
        self._sender_id = sender_id

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id


class SessionPlugin(BaseUtilsMixin):
    def __init__(self):
        self.group_filter_mode = '黑名单'
        self.group_filter_list = []
        self.private_filter_mode = '黑名单'
        self.private_filter_list = []
        self.session_control_enabled = True
        self.debounce_seconds = 30


class TestSessionControl(unittest.TestCase):
    def test_session_can_be_disabled_and_reenabled(self):
        plugin = SessionPlugin()
        event = DummyEvent()

        self.assertTrue(plugin._is_session_enabled(event))
        self.assertTrue(plugin._set_session_enabled(event, False))
        self.assertFalse(plugin._is_session_enabled(event))
        self.assertTrue(plugin._set_session_enabled(event, True))
        self.assertTrue(plugin._is_session_enabled(event))

    def test_duplicate_link_is_debounced_per_platform(self):
        plugin = SessionPlugin()
        event = DummyEvent()

        self.assertTrue(plugin._can_process_event(event, 'YouTube'))
        self.assertFalse(plugin._can_process_event(event, 'YouTube'))
        self.assertTrue(plugin._can_process_event(event, 'Pixiv'))


if __name__ == '__main__':
    unittest.main()
