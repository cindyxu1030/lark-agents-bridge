"""Catch up on @-mentions the bridge missed while offline.

Lark's long connection does not replay events that arrived while the subscriber was
disconnected (laptop asleep, lid closed). This module lists recent messages in the
configured group chats, finds @-mentions of this bot that nobody handled, and feeds
them through the normal message handler as if they had just arrived.

"Handled" means our app already reacted to the message (the bridge adds an instinct
reaction when it picks a message up) or a bot already replied to it. That makes the
scan safe to repeat and safe across restarts without a state file.

Config (env):
  CATCHUP_CHAT_IDS          comma-separated oc_ chat ids to scan; empty = disabled
  CATCHUP_LOOKBACK_HOURS    how far back to look (default 12)
  CATCHUP_INTERVAL_SECONDS  periodic sweep interval (default 600); 0 = only on connect
"""
import asyncio
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import bot_config as config
from agent_routing import extract_text_for_routing, route_message_for_agent

CHAT_IDS = [v.strip() for v in os.getenv("CATCHUP_CHAT_IDS", "").split(",") if v.strip()]
LOOKBACK_HOURS = float(os.getenv("CATCHUP_LOOKBACK_HOURS", "12"))
INTERVAL_SECONDS = int(os.getenv("CATCHUP_INTERVAL_SECONDS", "600"))

_SELF_ALIASES = tuple(config.SELF_BOT_NAMES)
_OTHER_ALIASES = tuple(config.DEBATE_PEER_NAMES)


def _lark_cli() -> str:
    path = "/usr/local/bin/lark-cli"
    return path if os.path.exists(path) else (shutil.which("lark-cli") or "lark-cli")


def _lark_env(with_app_creds: bool) -> dict:
    """Env for lark-cli. The stored CLI config (same app) carries the bot token the REST
    calls need; the app-id/secret overrides the event subscriber uses do not, so the
    overrides are only a fallback for machines without a stored config."""
    env = os.environ.copy()
    env["LARKSUITE_CLI_BRAND"] = os.getenv("LARKSUITE_CLI_BRAND", "lark")
    if with_app_creds:
        env["LARKSUITE_CLI_APP_ID"] = config.FEISHU_APP_ID
        env["LARKSUITE_CLI_APP_SECRET"] = config.FEISHU_APP_SECRET
    else:
        env.pop("LARKSUITE_CLI_APP_ID", None)
        env.pop("LARKSUITE_CLI_APP_SECRET", None)
    return env


def _envelope(text: str) -> dict:
    start = text.find("{")
    return json.loads(text[start:]) if start >= 0 else {}


def list_messages(chat_id: str, since: datetime) -> list[dict]:
    """Messages in chat_id since `since` (UTC), oldest first. [] on any failure."""
    cmd = [
        _lark_cli(), "im", "+chat-messages-list", "--as", "bot",
        "--chat-id", chat_id,
        "--start-time", since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "--order", "asc", "--page-all", "--format", "json",
    ]
    last_err = None
    for with_creds in (False, True):          # stored config first, env creds as fallback
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, env=_lark_env(with_creds), timeout=60)
            data = _envelope(p.stdout) or _envelope(p.stderr)
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as e:
            last_err = f"{type(e).__name__}: {e}"
            continue
        if data.get("ok"):
            return data.get("data", {}).get("messages", []) or []
        err = data.get("error") or {}
        last_err = f"rc={p.returncode} {err.get('type')}/{err.get('subtype')}: {err.get('message')}" if err else f"rc={p.returncode} no envelope"
    print(f"[catchup] list failed for {chat_id[:12]}…: {last_err}", flush=True)
    return []


def normalize_mentions(mentions) -> list[dict]:
    """List-API mentions carry `id` as a bare open_id string; routing reads a dict."""
    out = []
    for m in mentions or []:
        mid = m.get("id")
        out.append({
            "id": mid if isinstance(mid, dict) else {"open_id": mid or ""},
            "key": m.get("key", ""),
            "name": m.get("name", ""),
        })
    return out


def _reply_target(msg: dict) -> str:
    rt = msg.get("reply_to")
    if isinstance(rt, dict):
        return str(rt.get("message_id") or rt.get("id") or "")
    return str(rt or "")


def is_handled(msg: dict, messages: list[dict], app_id: str) -> bool:
    """True if our app reacted to msg, or any bot replied to it."""
    for d in (msg.get("reactions") or {}).get("details", []) or []:
        op = d.get("operator") or {}
        if op.get("operator_type") == "app" and op.get("operator_id") == app_id:
            return True
    mid = msg.get("message_id")
    for other in messages:
        if (other.get("sender") or {}).get("sender_type") == "app" and _reply_target(other) == mid:
            return True
    return False


def find_missed(messages: list[dict], app_id: str) -> list[dict]:
    """Human messages that @-mention this bot and were never handled, oldest first."""
    missed = []
    for msg in messages:
        sender = msg.get("sender") or {}
        if sender.get("sender_type") == "app" or msg.get("deleted"):
            continue
        mentions = normalize_mentions(msg.get("mentions"))
        text = extract_text_for_routing(msg.get("msg_type", ""), {"text": msg.get("content", "")})
        decision = route_message_for_agent(
            text, mentions, is_group=True,
            self_aliases=_SELF_ALIASES, other_aliases=_OTHER_ALIASES,
            self_ids=config.BOT_MENTION_OPEN_IDS, other_ids=config.OTHER_BOT_MENTION_OPEN_IDS,
            require_mention_in_group=True,
        )
        if not (decision.mentioned_self and not decision.mentioned_other):
            continue
        if is_handled(msg, messages, app_id):
            continue
        missed.append(msg)
    return missed


def to_event(msg: dict) -> dict:
    """Compact event in the shape handle_message_from_cli accepts.

    No create_time on purpose: the handler drops events older than 30 s, and these are
    old by definition.
    """
    sender = msg.get("sender") or {}
    content = msg.get("content", "")
    if not (isinstance(content, str) and content.lstrip().startswith("{")):
        content = json.dumps({"text": content}, ensure_ascii=False)
    return {
        "message_type": msg.get("msg_type", "text"),
        "chat_type": "group",
        "user_id": sender.get("id", ""),
        "chat_id": msg.get("chat_id", ""),
        "message_id": msg.get("message_id", ""),
        "content": content,
        "mentions": normalize_mentions(msg.get("mentions")),
        "thread_id": msg.get("thread_id", "") or "",
        "root_id": _reply_target(msg),
        "parent_id": _reply_target(msg),
        "_catchup": True,
    }


async def run_once(handle, reason: str = "") -> int:
    """Scan every configured chat once; feed missed mentions to `handle`. Returns count."""
    if not CHAT_IDS:
        return 0
    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    fed = 0
    for chat_id in CHAT_IDS:
        messages = await asyncio.to_thread(list_messages, chat_id, since)
        missed = find_missed(messages, config.FEISHU_APP_ID)
        print(f"[catchup] {reason or 'sweep'}: {chat_id[:12]}… scanned {len(messages)}, missed {len(missed)}", flush=True)
        for msg in missed:
            print(f"[catchup] replaying {msg.get('message_id')} from {(msg.get('sender') or {}).get('name', '?')} @ {msg.get('create_time', '')}", flush=True)
            try:
                await handle(to_event(msg))
                fed += 1
            except Exception as e:  # one bad message must not stop the rest
                print(f"[catchup] replay failed {msg.get('message_id')}: {type(e).__name__}: {e}", flush=True)
    return fed


async def periodic_loop(handle):
    if not CHAT_IDS or INTERVAL_SECONDS <= 0:
        return
    while True:
        await asyncio.sleep(INTERVAL_SECONDS)
        try:
            await run_once(handle, "periodic")
        except Exception as e:
            print(f"[catchup] periodic sweep error: {type(e).__name__}: {e}", flush=True)
