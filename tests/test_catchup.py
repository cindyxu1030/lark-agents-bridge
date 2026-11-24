import os
import sys

import pytest

os.environ.setdefault("FEISHU_APP_ID", "cli_self")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")
os.environ.setdefault("BOT_SELF_ALIASES", "Bridge Bot")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import catchup  # noqa: E402

APP = "cli_self"


def human(mid, text, mention_name="Bridge Bot", reacted_by=None, reply_to=None, sender_type="user"):
    msg = {
        "message_id": mid, "chat_id": "oc_group", "msg_type": "text", "content": text,
        "sender": {"id": "ou_teammate", "sender_type": sender_type, "name": "Teammate"},
        "mentions": [{"id": "ou_bot", "key": "@_user_1", "name": mention_name}] if mention_name else [],
        "create_time": "2026-11-03 10:29",
    }
    if reacted_by:
        msg["reactions"] = {"details": [{"emoji_type": "JIAYI", "operator": {"operator_id": reacted_by, "operator_type": "app"}}]}
    if reply_to:
        msg["reply_to"] = reply_to
    return msg


def bot_reply(mid, reply_to):
    return {"message_id": mid, "chat_id": "oc_group", "msg_type": "interactive", "content": "<card>",
            "sender": {"id": APP, "sender_type": "app", "name": "Bridge Bot"}, "reply_to": reply_to}


def test_unhandled_self_mention_is_missed():
    msgs = [human("m1", "@Bridge Bot 周五那场我有冲突")]
    assert [m["message_id"] for m in catchup.find_missed(msgs, APP)] == ["m1"]


def test_reacted_message_counts_as_handled():
    msgs = [human("m1", "@Bridge Bot 帮我换一下", reacted_by=APP)]
    assert catchup.find_missed(msgs, APP) == []


def test_reaction_by_another_app_does_not_count():
    msgs = [human("m1", "@Bridge Bot 帮我换一下", reacted_by="cli_other")]
    assert len(catchup.find_missed(msgs, APP)) == 1


def test_bot_reply_counts_as_handled():
    msgs = [human("m1", "@Bridge Bot 下周二谁有空"), bot_reply("m2", "m1")]
    assert catchup.find_missed(msgs, APP) == []


def test_messages_from_bots_and_without_self_mention_are_skipped():
    msgs = [
        human("m1", "@Other Bot 接手", mention_name="Other Bot"),
        human("m2", "no mention at all", mention_name=None),
        human("m3", "@Bridge Bot hi", sender_type="app"),
    ]
    assert catchup.find_missed(msgs, APP) == []


def test_to_event_shape_has_no_create_time_and_dict_mentions():
    evt = catchup.to_event(human("m1", "@Bridge Bot 周三那场现在是谁去"))
    assert "create_time" not in evt and "timestamp" not in evt
    assert evt["_catchup"] is True and evt["chat_type"] == "group"
    assert evt["user_id"] == "ou_teammate" and evt["message_id"] == "m1"
    assert evt["mentions"][0]["id"] == {"open_id": "ou_bot"}
    assert '"text"' in evt["content"]


@pytest.mark.asyncio
async def test_run_once_feeds_only_missed(monkeypatch):
    fed = []
    monkeypatch.setattr(catchup, "CHAT_IDS", ["oc_group"])
    monkeypatch.setattr(catchup, "list_messages", lambda chat_id, since: [
        human("m1", "@Bridge Bot a"),
        human("m2", "@Bridge Bot b", reacted_by=APP),
    ])

    async def handle(evt):
        fed.append(evt["message_id"])

    n = await catchup.run_once(handle, "test")
    assert n == 1 and fed == ["m1"]
