import asyncio
import os
import sys

import pytest

os.environ.setdefault("FEISHU_APP_ID", "cli_self")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_config as config  # noqa: E402
import reply_context  # noqa: E402

CHAT = "oc_group"
THREAD = "omt_thread"


def msg(mid, text, name="Teammate", sender_id="ou_teammate", chat_id=CHAT):
    return {
        "message_id": mid, "chat_id": chat_id, "msg_type": "text", "content": text,
        "create_time": "2026-11-22 20:30", "deleted": False,
        "sender": {"id": sender_id, "name": name},
    }


def bot(mid, text):
    return msg(mid, f"<card>\n{text}\n</card>", name="Bridge Bot", sender_id=config.FEISHU_APP_ID)


@pytest.fixture
def lark(monkeypatch):
    """Fake Lark: `thread` is what the thread listing returns, `store` what mget can read."""
    state = {"thread": [], "store": {}, "calls": []}

    def fetch_thread(thread_id):
        state["calls"].append(("thread", thread_id))
        return list(state["thread"])

    def fetch_messages(ids):
        state["calls"].append(("mget", tuple(ids)))
        return [state["store"][i] for i in ids if i in state["store"]]

    monkeypatch.setattr(reply_context, "fetch_thread", fetch_thread)
    monkeypatch.setattr(reply_context, "fetch_messages", fetch_messages)
    reply_context._seen.clear()
    return state


def build(**kw):
    kw.setdefault("user_id", "ou_owner")
    kw.setdefault("chat_key", f"{CHAT}:{THREAD}")
    kw.setdefault("chat_id", CHAT)
    return asyncio.run(reply_context.build(**kw))


def test_first_thread_turn_gets_the_whole_thread_without_its_own_messages(lark):
    lark["thread"] = [
        bot("root", "要我建 ticket 吗？回「建」我就做。"),
        msg("cur", "@bot 建", name="Owner", sender_id="ou_owner"),
        bot("placeholder", "思考中"),
    ]
    ctx = build(message_id="cur", parent_id="root", root_id="root", thread_id=THREAD,
                fresh_session=True, exclude_ids=("placeholder",))
    assert "要我建 ticket 吗" in ctx.text
    assert "你自己之前发的" in ctx.text
    assert "<card>" not in ctx.text
    assert "@bot 建" not in ctx.text and "思考中" not in ctx.text
    assert ctx.text.endswith("[用户的新消息：]\n")
    assert ("mget", ("root",)) not in lark["calls"]      # root came with the thread listing


def test_later_turns_only_get_what_was_posted_since(lark):
    lark["thread"] = [bot("root", "方案 A"), msg("cur1", "@bot 建", name="Owner")]
    build(message_id="cur1", parent_id="root", root_id="root", thread_id=THREAD, fresh_session=True).commit()

    lark["thread"] += [bot("reply1", "建好了"), msg("teammate1", "我觉得应该是 P1"), msg("cur2", "@bot 你怎么看", name="Owner")]
    ctx = build(message_id="cur2", parent_id="root", root_id="root", thread_id=THREAD, exclude_ids=("reply1",))
    assert "我觉得应该是 P1" in ctx.text
    assert "方案 A" not in ctx.text and "建好了" not in ctx.text
    assert "用户引用" not in ctx.text                      # plain thread reply, root already shown


def test_nothing_new_in_thread_adds_nothing(lark):
    lark["thread"] = [bot("root", "方案 A"), msg("cur1", "@bot 建")]
    build(message_id="cur1", parent_id="root", root_id="root", thread_id=THREAD, fresh_session=True).commit()
    lark["thread"] += [msg("cur2", "@bot 继续")]
    assert build(message_id="cur2", parent_id="root", root_id="root", thread_id=THREAD).text == ""


def test_uncommitted_context_is_shown_again(lark):
    lark["thread"] = [bot("root", "方案 A"), msg("cur1", "@bot 建")]
    build(message_id="cur1", parent_id="root", root_id="root", thread_id=THREAD)   # run failed: no commit
    lark["thread"] += [msg("cur2", "@bot 建")]
    assert "方案 A" in build(message_id="cur2", parent_id="root", root_id="root", thread_id=THREAD).text


def test_fresh_session_ignores_what_an_old_session_saw(lark):
    lark["thread"] = [bot("root", "方案 A"), msg("cur1", "@bot 建")]
    build(message_id="cur1", parent_id="root", root_id="root", thread_id=THREAD).commit()
    lark["thread"] += [msg("cur2", "@bot 建")]
    assert "方案 A" in build(message_id="cur2", parent_id="root", root_id="root", thread_id=THREAD, fresh_session=True).text


def test_quote_of_a_specific_thread_message_is_marked_in_place(lark):
    lark["thread"] = [bot("root", "三个方案"), msg("teammate1", "选 B"), msg("cur", "@bot 同意")]
    ctx = build(message_id="cur", parent_id="teammate1", root_id="root", thread_id=THREAD, fresh_session=True)
    assert "Teammate ← 用户回复的是这条" in ctx.text
    assert "用户引用" not in ctx.text


def test_dm_quote_fetches_the_quoted_message(lark):
    lark["store"]["digest"] = msg("digest", "今日 AI 新闻：……", name="Other Bot", chat_id="oc_p2p")
    ctx = build(chat_key="ou_owner", chat_id="oc_p2p", message_id="cur", parent_id="digest", root_id="digest")
    assert "[用户引用 / 回复的消息：]" in ctx.text and "今日 AI 新闻" in ctx.text
    assert ("thread", THREAD) not in lark["calls"]


def test_group_reply_chain_shows_root_then_parent(lark):
    lark["store"] = {"root": msg("root", "新预约提醒"), "mid": msg("mid", "这个先跳过", name="Reviewer")}
    ctx = build(chat_key=f"{CHAT}:root", message_id="cur", parent_id="mid", root_id="root")
    assert ctx.text.index("新预约提醒") < ctx.text.index("这个先跳过")
    assert "第一条是这串回复的起点" in ctx.text


def test_messages_from_another_chat_are_dropped(lark):
    lark["store"]["other"] = msg("other", "别的群的内容", chat_id="oc_elsewhere")
    assert build(chat_key=CHAT, message_id="cur", parent_id="other", root_id="other").text == ""


def test_unreadable_quote_adds_nothing(lark):
    assert build(chat_key=CHAT, message_id="cur", parent_id="gone", root_id="gone").text == ""


def test_long_thread_keeps_root_and_latest(lark):
    lark["thread"] = [bot("root", "ROOT")] + [msg(f"m{i}", f"msg{i} " + "x" * 3000) for i in range(12)]
    ctx = build(message_id="cur", parent_id="root", root_id="root", thread_id=THREAD, fresh_session=True)
    assert "ROOT" in ctx.text and "msg11 " in ctx.text and "msg0 " not in ctx.text
    assert "中间省略" in ctx.text
    assert len(ctx.text) < reply_context.MAX_BLOCK_CHARS + 4000


def test_oversized_message_is_truncated(lark):
    lark["store"]["big"] = msg("big", "y" * 10000)
    ctx = build(chat_key=CHAT, message_id="cur", parent_id="big", root_id="big")
    assert "已截断" in ctx.text and len(ctx.text) < 5000
