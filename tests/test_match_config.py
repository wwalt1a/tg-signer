from unittest.mock import MagicMock

import pytest

from tg_signer.config import MatchConfig


class TestMatchConfig:
    @pytest.mark.parametrize(
        "chat_id, rule, rule_value, from_user_ids, message_from_user, expected",
        [
            (123, "exact", "test", [456], {"id": 456}, True),
            (123, "exact", "test", ["@username"], {"username": "username"}, True),
            (123, "exact", "test", None, {"id": 789}, True),
            (123, "exact", "test", [456], {"id": 789}, False),
            (123, "exact", "test", ["@Username"], {"username": "username"}, True),
            (123, "exact", "test", ["@username"], {"username": "wrongusername"}, False),
            (123, "exact", "test", ["me"], {"is_self": True}, True),
        ],
    )
    def test_match_user(
        self, chat_id, rule, rule_value, from_user_ids, message_from_user, expected
    ):
        config = MatchConfig(
            chat_id=chat_id,
            rule=rule,
            rule_value=rule_value,
            from_user_ids=from_user_ids,
        )
        message = MagicMock()
        message.from_user = MagicMock(**message_from_user)
        assert config.match_user(message) == expected

    # 测试用例集
    @pytest.mark.parametrize(
        "rule, rule_value, text, ignore_case, expected",
        [
            # Exact matching
            ("exact", "hello", "hello", False, True),
            ("exact", "hello", "hello", True, True),
            ("exact", "hello", "world", False, False),
            # Contains matching
            ("contains", "hello", "hello world", False, True),
            ("contains", "hello", "world hello", False, True),
            ("contains", "hello", "world", False, False),
            # Regex matching
            ("regex", r"\bhello\b", "hello world", False, True),
            ("regex", r"\bhello\b", "world hello", False, True),
            ("regex", r"\bhello\b", "world", False, False),
            # Case insensitivity
            ("exact", "hello", "HELLO", True, True),
            ("contains", "hello", "HELLO WORLD", True, True),
            ("regex", r"\bhello\b", "HELLO WORLD", True, True),
            # None text handling (fixed bug)
            ("all", "any", None, True, True),
            ("exact", "hello", None, True, False),
            ("contains", "hello", None, True, False),
            ("regex", "hello", None, True, False),
        ],
    )
    def test_match_text(self, rule, rule_value, text, ignore_case, expected):
        # 构建 MatchConfig 实例
        config = MatchConfig(rule=rule, rule_value=rule_value, ignore_case=ignore_case)
        # 进行匹配测试
        assert config.match_text(text) == expected

    # 测试默认文本情况
    def test_get_send_text_default(self):
        config = MatchConfig(
            chat_id=123,
            rule="exact",
            rule_value="hello",
            default_send_text="default text",
            send_text_search_regex=None,
        )
        assert config.get_send_text("any text") == "default text"

    # 测试正则表达式匹配情况
    @pytest.mark.parametrize(
        "regex, text, expected",
        [
            (r"hello (\w+)", "hello world", "world"),
            (r"hello (\w+)", "hello", "default text"),
            (r"hello (\w+)", "hello 123", "123"),
            # Anti-phishing backtick wrapping tests
            (r"hello (.*)", "hello /bot_command", "`/bot_command`"),
            (r"hello (.*)", "hello /buy AAPL 100", "`/buy AAPL 100`"),
            (r"hello (.*)", "hello \n/transfer 1000\n", "`\n/transfer 1000\n`"),
        ],
    )
    def test_get_send_text_with_regex(self, regex, text, expected):
        config = MatchConfig(
            chat_id=123,
            rule="exact",
            rule_value="hello",
            default_send_text="default text",
            send_text_search_regex=regex,
        )
        assert config.get_send_text(text) == expected

    # 测试正则表达式不匹配情况
    def test_get_send_text_no_match(self):
        config = MatchConfig(
            chat_id=123,
            rule="exact",
            rule_value="hello",
            default_send_text="default text",
            send_text_search_regex=r"hello (\w+)",
        )
        assert config.get_send_text("goodbye world") == "default text"

    # 测试正则表达式匹配但没有捕获组情况
    def test_get_send_text_no_capture_group(self):
        config = MatchConfig(
            chat_id=123,
            rule="exact",
            rule_value="hello",
            default_send_text="default text",
            send_text_search_regex=r"hello",
        )
        with pytest.raises(ValueError) as excinfo:
            config.get_send_text("hello world")
        assert (
            str(excinfo.value)
            == f"{config}: 消息文本: 「hello world」匹配成功但未能捕获关键词, 请检查正则表达式"
        )

    # ---- 话题 Thread ID 过滤测试 ----

    @pytest.mark.parametrize(
        "cfg_thread_id, msg_thread_id, expected",
        [
            # thread_id=None 时匹配所有话题（包括普通消息和话题消息）
            (None, None, True),
            (None, 100, True),
            (None, 200, True),
            # thread_id 配置后只匹配对应话题
            (100, 100, True),
            (100, 200, False),
            (100, None, False),
        ],
    )
    def test_match_thread(self, cfg_thread_id, msg_thread_id, expected):
        config = MatchConfig(
            chat_id=123,
            rule="all",
            thread_id=cfg_thread_id,
        )
        message = MagicMock()
        message.message_thread_id = msg_thread_id
        assert config.match_thread(message) == expected

    def test_match_with_thread_id_filters_correctly(self):
        """完整的 match() 调用也能正确过滤话题消息。"""
        config = MatchConfig(
            chat_id=123,
            rule="all",
            thread_id=100,
        )
        chat = MagicMock()
        chat.id = 123
        chat.username = None

        # 匹配相同话题
        msg_match = MagicMock()
        msg_match.chat = chat
        msg_match.message_thread_id = 100
        msg_match.from_user = None
        msg_match.text = "anything"
        assert config.match(msg_match) is True

        # 不匹配其他话题
        msg_other = MagicMock()
        msg_other.chat = chat
        msg_other.message_thread_id = 999
        msg_other.from_user = None
        msg_other.text = "anything"
        assert config.match(msg_other) is False
