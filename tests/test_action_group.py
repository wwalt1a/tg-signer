"""Tests for ActionGroup feature."""
import json

import pytest
from tg_signer.config import (
    ActionGroup,
    SendTextAction,
    SignChatV3,
    SignConfigV3,
)


class TestActionGroup:
    def test_action_group_with_sign_at(self):
        """ActionGroup 可以独立设置 sign_at。"""
        group = ActionGroup(
            sign_at="38 * * * *",
            actions=[SendTextAction(text="/clean@bot")],
            action_interval=1.0,
        )
        assert group.sign_at == "38 * * * *"
        assert len(group.actions) == 1
        assert group.actions[0].text == "/clean@bot"

    def test_action_group_inherits_sign_at(self):
        """sign_at 默认为 None（继承父级）。"""
        group = ActionGroup(actions=[SendTextAction(text="/water@bot")])
        assert group.sign_at is None

    def test_sign_chat_v3_with_action_groups(self):
        """SignChatV3 支持 action_groups 字段。"""
        chat = SignChatV3(
            chat_id=12345,
            action_groups=[
                ActionGroup(
                    sign_at="38 * * * *",
                    actions=[SendTextAction(text="/clean@bot")],
                ),
                ActionGroup(
                    sign_at="0 12 * * *",
                    actions=[SendTextAction(text="/water@bot")],
                ),
                ActionGroup(
                    sign_at="0 20 * * *",
                    actions=[SendTextAction(text="/harvest@bot")],
                ),
            ],
        )
        assert len(chat.action_groups) == 3
        assert chat.action_groups[0].sign_at == "38 * * * *"
        assert chat.action_groups[1].sign_at == "0 12 * * *"
        assert chat.action_groups[2].sign_at == "0 20 * * *"

    def test_sign_config_v3_with_action_groups_from_json(self):
        """从 JSON 中解析包含 action_groups 的配置。"""
        config_json = {
            "chats": [
                {
                    "chat_id": -1003795217114,
                    "thread_id": 31458,
                    "delete_after": 18,
                    "action_groups": [
                        {
                            "sign_at": "38 * * * *",
                            "actions": [{"action": 1, "text": "/clean@Nebula12s_bot"}],
                        },
                        {
                            "sign_at": "0 12 * * *",
                            "actions": [{"action": 1, "text": "/water@Nebula12s_bot"}],
                        },
                        {
                            "sign_at": "0 20 * * *",
                            "actions": [{"action": 1, "text": "/harvest@Nebula12s_bot"}],
                        },
                    ],
                    "action_interval": 1,
                }
            ],
            "sign_at": "38 * * * *",
            "random_seconds": 5,
            "sign_interval": 1,
        }
        result = SignConfigV3.load(config_json)
        assert result is not None
        config, from_old = result
        assert not from_old
        assert len(config.chats) == 1
        chat = config.chats[0]
        assert chat.action_groups is not None
        assert len(chat.action_groups) == 3
        assert chat.action_groups[0].actions[0].text == "/clean@Nebula12s_bot"
        assert chat.action_groups[1].sign_at == "0 12 * * *"

    def test_sign_chat_v3_backward_compat_actions(self):
        """原有 actions 字段（不含 action_groups）仍向后兼容。"""
        chat = SignChatV3(
            chat_id=12345,
            actions=[SendTextAction(text="/sign")],
        )
        assert len(chat.actions) == 1
        assert chat.action_groups is None

    def test_action_group_serialization(self):
        """ActionGroup 可以被正确序列化为 JSON。"""
        group = ActionGroup(
            sign_at="38 * * * *",
            actions=[SendTextAction(text="/test")],
            action_interval=2.0,
            random_seconds=10,
        )
        data = group.model_dump(mode="json")
        assert data["sign_at"] == "38 * * * *"
        assert data["action_interval"] == 2.0
        assert data["random_seconds"] == 10
        assert data["actions"][0]["text"] == "/test"
