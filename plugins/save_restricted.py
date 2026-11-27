"""
Save Restricted Content — ported core of Save-Restricted-Content-Bot-v3.

Commands:
  /save [link]       Save one restricted/public post by t.me link
  /batchsave         Batch: start link → count → download+reupload range
  /cancelsave        Cancel active save job

Uses the user's configured userbot session (required for private/restricted).
Public channels can work with bot token for non-restricted posts.
"""
import re
import asyncio
import logging
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import FloodWait
from database import db
from .test import CLIENT as _CLIENT_CLASS, start_clone_bot

CLIENT = _CLIENT_CLASS()
logger = logging.getLogger(__name__)

PENDING_SAVE = {}       # user_id -> guided state
ACTIVE_SAVE = {}        # user_id -> {"cancel": bool, "ok": int, "fail": int}

# t.me/c/CHAT/MSG  or  t.me/username/MSG  (optional topic: .../TOPIC/MSG)
_LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.(?:me|dog))/(?:c/)?(\d+|[a-zA-Z][\w\d_]{2,})/(?:(\d+)/)?(\d+)",
    re.IGNORECASE,
)
_BOT_LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.(?:me|dog))/([a-zA-Z][\w\d_]{2,}bot)/(\d+)",
    re.IGNORECASE,
)


def parse_tg_link(text: str):
    """
    Return (chat_ref, msg_id, kind) where kind is 'private'|'public'|'bot'.
    chat_ref: int (-100…) for private, username str for public.
    """
    text = (text or "").strip().replace("?single", "")
    m = _BOT_LINK_RE.search(text)
    if m:
        return m.group(1), int(m.group(2)), "bot"
    m = _LINK_RE.search(text)
    if not m:
        return None, None, None
    part, topic, msg = m.group(1), m.group(2), m.group(3)
    msg_id = int(msg)
    if part.isdigit():
        # private: /c/12345/msg
        return int(f"-100{part}"), msg_id, "private"
    return part, msg_id, "public"


async def _get_user_client(user_id: int):
    bot_data = await db.get_bot(user_id)
    if not bot_data:
        return None, None, "No bot/session. Use /settings first."
    try:
        client = await start_clone_bot(CLIENT.client(bot_data))
        return client, bot_data, None
    except Exception as e:
        return None, None, f"Client start failed: <code>{e}</code>"


async def _fetch_message(client, chat_ref, msg_id, kind, bot_data):
    """Fetch message; try several chat id forms for old private groups."""
    tries = [chat_ref]
    if kind == "private" and isinstance(chat_ref, int):
        # also try -CHAT without -100 prefix (old groups)
        s = str(chat_ref)
        if s.startswith("-100"):
            tries.append(int("-" + s[4:]))

    last_err = None
    for ref in tries:
        try:
            msg = await client.get_messages(ref, msg_id)
            if msg and not getattr(msg, "empty", False):
                return msg, ref
        except FloodWait as e:
            await asyncio.sleep(e.value)
            try:
                msg = await client.get_messages(ref, msg_id)
                if msg and not getattr(msg, "empty", False):
                    return msg, ref
            except Exception as e2:
                last_err = e2
        except Exception as e:
            last_err = e
    return None, last_err


async def _download_upload(bot: Client, client, msg, dest_chat: int, status_msg: Message, topic_id=None, force_download=False):
    """Prefer copy; fallback download+reupload. Shared via transfer.py."""
    from .transfer import transfer_message

    async def status_cb(t: str):
        try:
            await status_msg.edit_text(t)
        except Exception:
            pass

    return await transfer_message(
        client, msg, dest_chat,
        topic_id=topic_id,
        uploader=bot,
        force_download=force_download,
        status_cb=status_cb,
    )


async def _resolve_dest(user_id: int, bot: Client):
    """Default dest = user DM. If single target channel set, use that (+ topic)."""
    channels = await db.get_user_channels(user_id)
    if channels and len(channels) == 1:
        ch = channels[0]
        return ch["chat_id"], ch.get("title", "target"), ch.get("topic_id")
    return user_id, "Saved (DM)", None


async def save_one(bot: Client, user_id: int, link: str, status: Message = None):
    client, bot_data, err = await _get_user_client(user_id)
    if err:
        if status:
            await status.edit_text(err)
        return False, err

    chat_ref, msg_id, kind = parse_tg_link(link)
    if not chat_ref:
        msg = "Invalid link. Examples:\n<code>https://t.me/c/1234567890/42</code>\n<code>https://t.me/channel/42</code>"
        if status:
            await status.edit_text(msg)
        await client.stop()
        return False, msg

    if kind in ("private", "bot") and bot_data.get("is_bot"):
        tip = (
            "Private/restricted needs a <b>userbot session</b>.\n"
            "Add session in /settings (not bot token only)."
        )
        if status:
            await status.edit_text(tip)
        await client.stop()
        return False, tip

    st = status or await bot.send_message(user_id, "🔍 Fetching…")
    try:
        msg, resolved = await _fetch_message(client, chat_ref, msg_id, kind, bot_data)
        if not msg:
            err = f"Cannot fetch message. Join source with userbot first.\n<code>{resolved}</code>"
            await st.edit_text(err)
            return False, err

        dest, dest_name, topic_id = await _resolve_dest(user_id, bot)
        ok, how = await _download_upload(bot, client, msg, dest, st, topic_id=topic_id)
        if ok:
            src_title = getattr(msg.chat, "title", None) or getattr(msg.chat, "username", None) or chat_ref
            await st.edit_text(
                f"✅ Saved from <b>{src_title}</b> (msg <code>{msg_id}</code>)\n"
                f"Method: <code>{how}</code>\n"
                f"To: <b>{dest_name}</b>"
            )
            return True, how
        await st.edit_text(f"❌ Save failed: <code>{how}</code>")
        return False, how
    finally:
        try:
            await client.stop()
        except Exception:
            pass


# ── Commands ──────────────────────────────────────────────────────────────────

@Client.on_message(filters.private & filters.command(["save", "savelink"]))
async def save_cmd(bot, message: Message):
    user_id = message.from_user.id
    args = message.text.split(maxsplit=1)

    if len(args) >= 2 and parse_tg_link(args[1])[0]:
        st = await message.reply("🔍 Saving…")
        await save_one(bot, user_id, args[1].strip(), st)
        return

    PENDING_SAVE[user_id] = {"step": "single_link"}
    await message.reply(
        "<b>💾 Save Restricted Content</b>\n\n"
        "Send a Telegram post link:\n"
        "• <code>https://t.me/c/CHAT_ID/MSG_ID</code> (private)\n"
        "• <code>https://t.me/channel/MSG_ID</code> (public)\n"
        "• <code>https://t.me/SomeBot/MSG_ID</code> (bot posts)\n\n"
        "Needs userbot session in /settings for private/restricted.\n"
        "Saved to your DM (or sole target channel in settings).\n\n"
        "/cancelsave — cancel"
    )


@Client.on_message(filters.private & filters.command("batchsave"))
async def batchsave_cmd(bot, message: Message):
    user_id = message.from_user.id
    if user_id in ACTIVE_SAVE:
        return await message.reply("Save job already running. /cancelsave first.")
    PENDING_SAVE[user_id] = {"step": "batch_start"}
    await message.reply(
        "<b>📦 Batch Save Restricted</b>\n\n"
        "<b>Step 1/2:</b> Send <b>start</b> post link\n"
        "Example: <code>https://t.me/c/123/10</code>\n\n"
        "/cancelsave — cancel"
    )


@Client.on_message(filters.private & filters.command("cancelsave"))
async def cancelsave_cmd(bot, message: Message):
    user_id = message.from_user.id
    PENDING_SAVE.pop(user_id, None)
    if user_id in ACTIVE_SAVE:
        ACTIVE_SAVE[user_id]["cancel"] = True
        return await message.reply("⏹ Cancel requested. Stops after current file.")
    await message.reply("No active save job.")


@Client.on_message(filters.private & filters.text & ~filters.command([
    "start", "fwd", "forward", "settings", "reset", "status",
    "live", "stoplive", "lives", "cancel", "addconn", "listconn", "useconn",
    "delconn", "schedule", "listschedule", "cancelschedule", "help", "filters",
    "broadcast", "resetall", "unequify", "save", "batchsave", "savelink", "cancelsave"
]))
async def save_flow(bot, message: Message):
    user_id = message.from_user.id
    if user_id not in PENDING_SAVE:
        return

    # don't steal schedule/live flows
    try:
        from plugins.connections import PENDING_SCHEDULE
        if user_id in PENDING_SCHEDULE:
            return
    except Exception:
        pass
    try:
        from plugins.regix import PENDING_LIVE
        if user_id in PENDING_LIVE:
            return
    except Exception:
        pass

    state = PENDING_SAVE[user_id]
    step = state.get("step")
    text = message.text.strip()

    if step == "single_link":
        PENDING_SAVE.pop(user_id, None)
        st = await message.reply("🔍 Saving…")
        await save_one(bot, user_id, text, st)
        return

    if step == "batch_start":
        chat_ref, msg_id, kind = parse_tg_link(text)
        if not chat_ref:
            return await message.reply("Invalid start link. Send full t.me post URL.")
        state.update({
            "step": "batch_count",
            "link": text,
            "chat_ref": chat_ref,
            "start_id": msg_id,
            "kind": kind,
        })
        return await message.reply(
            f"✅ Start msg <code>{msg_id}</code>\n\n"
            f"<b>Step 2/2:</b> How many messages to save?\n"
            f"Example: <code>50</code> (max 500)"
        )

    if step == "batch_count":
        try:
            count = int(text)
        except ValueError:
            return await message.reply("Send a number, e.g. <code>20</code>")
        if count < 1 or count > 500:
            return await message.reply("Count must be 1–500.")

        chat_ref = state["chat_ref"]
        start_id = state["start_id"]
        kind = state["kind"]
        PENDING_SAVE.pop(user_id, None)

        bot_data = await db.get_bot(user_id)
        if not bot_data:
            return await message.reply("No bot/session. /settings first.")
        if kind in ("private", "bot") and bot_data.get("is_bot"):
            return await message.reply(
                "Private/restricted needs userbot session in /settings."
            )

        st = await message.reply(f"📦 Batch save starting… 0/{count}")
        ACTIVE_SAVE[user_id] = {"cancel": False, "ok": 0, "fail": 0}

        client = None
        try:
            client = await start_clone_bot(CLIENT.client(bot_data))
            dest, dest_name, topic_id = await _resolve_dest(user_id, bot)

            # resolve chat once
            msg0, resolved_chat = await _fetch_message(client, chat_ref, start_id, kind, bot_data)
            if not msg0:
                await st.edit_text(f"Cannot open source: <code>{resolved_chat}</code>")
                return
            chat_id = msg0.chat.id
            src_title = msg0.chat.title or getattr(msg0.chat, "username", None) or str(chat_id)

            for i in range(count):
                if ACTIVE_SAVE.get(user_id, {}).get("cancel"):
                    break
                mid = start_id + i
                try:
                    msg = await client.get_messages(chat_id, mid)
                    if not msg or getattr(msg, "empty", False):
                        ACTIVE_SAVE[user_id]["fail"] += 1
                        continue
                    ok, _ = await _download_upload(bot, client, msg, dest, st, topic_id=topic_id)
                    if ok:
                        ACTIVE_SAVE[user_id]["ok"] += 1
                    else:
                        ACTIVE_SAVE[user_id]["fail"] += 1
                    if (i + 1) % 5 == 0 or i + 1 == count:
                        a = ACTIVE_SAVE[user_id]
                        await st.edit_text(
                            f"📦 <b>{src_title}</b>\n"
                            f"Progress: {i + 1}/{count}\n"
                            f"✅ {a['ok']} · ❌ {a['fail']} → {dest_name}"
                        )
                    await asyncio.sleep(0.6)
                except FloodWait as e:
                    await asyncio.sleep(e.value)
                except Exception as e:
                    ACTIVE_SAVE[user_id]["fail"] += 1
                    logger.error(f"[batchsave] msg {mid}: {e}")

            a = ACTIVE_SAVE.get(user_id, {"ok": 0, "fail": 0})
            cancelled = ACTIVE_SAVE.get(user_id, {}).get("cancel")
            await st.edit_text(
                f"{'⏹ Cancelled' if cancelled else '✅ Batch done'}\n"
                f"Source: <b>{src_title}</b>\n"
                f"✅ {a.get('ok', 0)} · ❌ {a.get('fail', 0)}\n"
                f"To: <b>{dest_name}</b>"
            )
        except Exception as e:
            await st.edit_text(f"Batch error: <code>{e}</code>")
        finally:
            ACTIVE_SAVE.pop(user_id, None)
            if client:
                try:
                    await client.stop()
                except Exception:
                    pass
