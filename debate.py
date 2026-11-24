"""跨 bot 群聊辩论（bot-to-bot debate via history polling）。

飞书不会把机器人发的消息推给其他机器人（事件层反循环限制），
所以两个 bot 无法互相"听见"。本模块用轮询群聊历史绕过：

1. 人类在群里发 `/debate <话题>` 并同时 @ 两个 bot —— 人类消息双方都收得到，
   这就是共同的发令枪。
2. 触发消息里先被 @ 的 bot 先发言。
3. 每个 bot 轮到自己时用 Claude 生成发言并发到群里，然后轮询群历史
   等对方的下一条消息（sender app_id == DEBATE_PEER_APP_IDS 之一）。
4. 双方各自独立执行回合上限（DEBATE_MAX_ROUNDS），最后一轮标 [final] 收尾。
5. 辩论回合一律用访客级工具沙箱（只读，无 shell 写权限）。

对端 bot 必须运行同版本 bridge 并配置好 DEBATE_PEER_* 环境变量。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time

import bot_config as config
from claude_runner import run_claude

# 每个 chat 同时只允许一场辩论
_active_debates: set[str] = set()

# 协作模式：同一套轮流+轮询引擎，不同的回合指令
MODES = {
    "debate": {
        "opening": "给出你的初始立场、论据和风险。",
        "respond": "回应对方观点：明确同意/反驳哪些点，补充新论据或风险。不要重复已说过的内容。",
        "closing": "这是最后一轮：总结共识与分歧，给出你的最终立场和可执行建议，标注 [final]。",
    },
    "brainstorm": {
        "opening": "针对主题提出 3-5 个具体想法，每个附一句可行性判断。",
        "respond": "在对方想法上加建（yes-and）：挑最强的 1-2 个深化，提出新组合或变体，指出被忽略的角度。",
        "closing": "最后一轮：把双方最强的想法合并成一个排序后的候选清单（前 3 名附下一步行动），标注 [final]。",
    },
    "review": {
        "opening": "针对主题产出初稿/方案（利用你能读到的本地文件和数据作为依据）。",
        "respond": "审阅对方的稿子：指出具体问题（引用原句），给出修改建议；然后交回修订版。",
        "closing": "最后一轮：交付吸收了全部审阅意见的最终版本，标注 [final]。",
    },
    "sync": {
        "opening": "汇报你这边的本地状态：你能读到的相关文件/日历/数据中，与主题相关的最新情况、进展和阻塞。",
        "respond": "对照对方的汇报：指出双方信息的差异、缺口和需要对齐的点，补充你这边的对应状态。",
        "closing": "最后一轮：输出一份双方状态对齐摘要 + 各自的下一步行动清单，标注 [final]。",
    },
}

_TRIGGER_RE = re.compile(r"/(debate|brainstorm|review|sync)\s+(.+)", re.DOTALL)


def parse_debate_trigger(text: str, mentions: list[dict] | None) -> tuple[str, str, bool] | None:
    """检测协作触发消息。

    返回 (mode, topic, self_goes_first)；不是触发消息则返回 None。
    需要：文本含 /debate|/brainstorm|/review|/sync <topic>，且 @ 了本 bot 和至少一个对端 bot。
    先被 @ 的 bot 先发言（按 mention 在消息里的顺序）。
    """
    if not config.DEBATE_PEER_APP_IDS and not config.DEBATE_PEER_NAMES:
        return None
    m = _TRIGGER_RE.search(text or "")
    if not m:
        return None
    mode = m.group(1)
    topic = m.group(2).strip()
    if not topic:
        return None

    self_pos, peer_pos = None, None
    peer_names = {n.lower() for n in config.DEBATE_PEER_NAMES}
    self_names = {n.lower() for n in config.SELF_BOT_NAMES}
    for i, mention in enumerate(mentions or []):
        name = str(mention.get("name", "")).strip().lower()
        if name in self_names and self_pos is None:
            self_pos = i
        elif name in peer_names and peer_pos is None:
            peer_pos = i
    if self_pos is None or peer_pos is None:
        return None
    return mode, topic, self_pos < peer_pos


async def run_debate(chat_id: str, topic: str, self_first: bool, feishu, mode: str = "debate") -> None:
    """执行一场辩论。以 asyncio task 方式跑，不阻塞主事件处理。"""
    if chat_id in _active_debates:
        return
    _active_debates.add(chat_id)
    transcript: list[tuple[str, str]] = []  # (speaker, text)
    my_name = config.SELF_BOT_NAMES[0] if config.SELF_BOT_NAMES else "This bot"
    peer_name = config.DEBATE_PEER_NAMES[0] if config.DEBATE_PEER_NAMES else "Peer bot"
    max_rounds = config.DEBATE_MAX_ROUNDS
    try:
        my_turn = self_first
        last_seen_ts = time.time()
        for round_no in range(1, max_rounds + 1):
            if my_turn:
                text = await _generate_turn(topic, transcript, round_no, max_rounds, my_name, peer_name, mode)
                tagged = f"[{my_name} · {mode} {round_no}/{max_rounds}]\n{text}"
                await _send_group_text(chat_id, tagged)
                transcript.append((my_name, text))
                last_seen_ts = time.time()
            else:
                peer_text = await _poll_for_peer_message(chat_id, last_seen_ts)
                if peer_text is None:
                    await _send_group_text(
                        chat_id, f"[{my_name}] 等待 {peer_name} 超时，{mode} 结束。")
                    return
                transcript.append((peer_name, peer_text))
                last_seen_ts = time.time()
                # 对方发完轮到我 —— 同一轮内的第二发言，不占新回合
                text = await _generate_turn(topic, transcript, round_no, max_rounds, my_name, peer_name, mode)
                tagged = f"[{my_name} · {mode} {round_no}/{max_rounds}]\n{text}"
                await _send_group_text(chat_id, tagged)
                transcript.append((my_name, text))
                last_seen_ts = time.time()
            # 等对方回应本轮（最后一轮也等，让对方收尾）
            if round_no < max_rounds or self_first:
                peer_text = await _poll_for_peer_message(chat_id, last_seen_ts)
                if peer_text is None:
                    break
                transcript.append((peer_name, peer_text))
                last_seen_ts = time.time()
                my_turn = True  # 之后的循环回合我直接发言
    finally:
        _active_debates.discard(chat_id)


async def _generate_turn(
    topic: str,
    transcript: list[tuple[str, str]],
    round_no: int,
    max_rounds: int,
    my_name: str,
    peer_name: str,
    mode: str = "debate",
) -> str:
    mode_cfg = MODES.get(mode, MODES["debate"])
    history = "\n\n".join(f"{speaker}:\n{text}" for speaker, text in transcript) or "(会话刚开始，你先发言)"
    if round_no >= max_rounds:
        instruction = mode_cfg["closing"]
    elif not transcript:
        instruction = mode_cfg["opening"]
    else:
        instruction = mode_cfg["respond"]
    prompt = (
        f"You are {my_name}, in a '{mode}' session with {peer_name} in a Lark group chat. "
        f"Audience: the team. Round {round_no} of {max_rounds}.\n"
        f"Topic: {topic}\n\n"
        f"Transcript so far:\n{history}\n\n"
        f"{instruction}\n"
        "Rules: under 300 words, concrete, cite evidence when you reference files or data, "
        "no filler praise of the other bot. Reply in the language of the topic. "
        "Your reply is posted verbatim to the group chat — output ONLY the debate turn text."
    )
    text, _, _ = await run_claude(
        message=prompt,
        session_id=None,
        model=config.DEBATE_MODEL or config.DEFAULT_MODEL,
        cwd=config.GUEST_CWD,
        allowed_tools=config.GUEST_ALLOWED_TOOLS,
    )
    return (text or "").strip() or "（生成失败，跳过本轮）"


async def _send_group_text(chat_id: str, text: str) -> None:
    lark_cli = shutil.which("lark-cli") or "/usr/local/bin/lark-cli"
    proc = await asyncio.create_subprocess_exec(
        lark_cli, "im", "+messages-send", "--as", "bot",
        "--chat-id", chat_id, "--text", text,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        env=_lark_env(),
    )
    await proc.wait()


async def _poll_for_peer_message(chat_id: str, after_ts: float) -> str | None:
    """轮询群历史，等对端 bot 在 after_ts 之后发的第一条消息。"""
    deadline = time.time() + config.DEBATE_TURN_TIMEOUT
    lark_cli = shutil.which("lark-cli") or "/usr/local/bin/lark-cli"
    while time.time() < deadline:
        await asyncio.sleep(config.DEBATE_POLL_SECONDS)
        proc = await asyncio.create_subprocess_exec(
            lark_cli, "im", "+chat-messages-list", "--as", "bot",
            "--chat-id", chat_id, "--page-size", "10",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=_lark_env(),
        )
        stdout, _ = await proc.communicate()
        if not stdout:
            continue
        try:
            data = json.loads(stdout.decode())
        except json.JSONDecodeError:
            continue
        msgs = data.get("data", {}).get("messages", []) or data.get("data", {}).get("items", [])
        for msg in msgs:
            sender = msg.get("sender", {})
            if sender.get("id") not in config.DEBATE_PEER_APP_IDS:
                continue
            create_time = _msg_ts(msg)
            if create_time <= after_ts:
                continue
            content = msg.get("content", "")
            if isinstance(content, str):
                try:
                    content = json.loads(content).get("text", content)
                except (json.JSONDecodeError, AttributeError):
                    pass
            return str(content)
    return None


def _msg_ts(msg: dict) -> float:
    raw = str(msg.get("create_time", "0"))
    try:
        ts = float(raw)
        return ts / 1000 if ts > 1e12 else ts
    except ValueError:
        return 0.0


def _lark_env() -> dict:
    env = os.environ.copy()
    env["LARKSUITE_CLI_APP_ID"] = config.FEISHU_APP_ID
    env["LARKSUITE_CLI_APP_SECRET"] = config.FEISHU_APP_SECRET
    env["LARKSUITE_CLI_BRAND"] = os.getenv("LARKSUITE_CLI_BRAND", "lark")
    return env
