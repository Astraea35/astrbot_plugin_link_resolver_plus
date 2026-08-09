import unittest

from core.common.base_mixin import BaseUtilsMixin


class DummyEvent:
    def __init__(self, group_id=None, sender_id="10001"):
        self._group_id = group_id
        self._sender_id = sender_id

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id


class FilterPlugin(BaseUtilsMixin):
    def __init__(self, **kwargs):
        self.group_filter_mode = "黑名单"
        self.group_filter_list = []
        self.private_filter_mode = "黑名单"
        self.private_filter_list = []
        self.__dict__.update(kwargs)


def _plugin(**kwargs):
    return FilterPlugin(**kwargs)


class TestMessageFilter(unittest.TestCase):
    def test_private_blacklist_blocks_listed_sender(self):
        plugin = _plugin(private_filter_list=["10001"])

        self.assertFalse(plugin._is_message_allowed(DummyEvent()))
        self.assertTrue(plugin._is_message_allowed(DummyEvent(sender_id="10002")))

    def test_private_whitelist_allows_only_listed_sender(self):
        plugin = _plugin(private_filter_mode="白名单", private_filter_list=["10001"])

        self.assertTrue(plugin._is_message_allowed(DummyEvent()))
        self.assertFalse(plugin._is_message_allowed(DummyEvent(sender_id="10002")))

    def test_group_messages_continue_to_use_group_filter(self):
        plugin = _plugin(group_filter_list=["30003"], private_filter_mode="白名单")

        self.assertFalse(plugin._is_message_allowed(DummyEvent(group_id="30003")))

if __name__ == "__main__":
    unittest.main()
