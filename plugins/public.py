"""
Batch forward — free-form multi-source & multi-target.
No need to pre-add targets in settings.
"""
import re
import asyncio
from .utils import STS, resolve_chat, format_chat_label, get_client_for_user
from database import db
from config import temp
from translation import Translation
from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait
from pyrogram.errors.exceptions.not_acceptable_406 import ChannelPrivate as PrivateChat
from pyrogram.errors.exceptions.bad_request_400 import (
    ChannelInvalid, UsernameInvalid, UsernameNotModified, ChannelPrivate
)
from pyrogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)


def _parse_msg_id_from_link(text: str):
    """Return (chat_ref, last_msg_id) from t.me link or None."""
    regex = re.compile(
        r"(https?://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?"
        r"(\d+|[a-zA-Z_0-9]+)/(?:\d+/)?(\d+)$"
    )
    match = regex.search((text or "").replace("?single", ""))
    if not match:
        return None
    chat_id = match.group(4)
    last_msg_id = int(match.group(5))
    if chat_id.isnumeric():
        chat_id = int(("-100" + chat_id))
    return chat_id, last_msg_id


async def _resolve_target_entry(client, message_or_text, bot):
    """Resolve one target: Message or text → {chat_id, topic, title, username}."""
    from .transfer import parse_dest
    topic_id = None
    query = message_or_text

    if isinstance(message_or_text, str):
        dest, topic_id = parse_dest(message_or_text.strip())
        if dest is not None and topic_id is not None:
            query = dest
        else:
            query = message_or_text.strip()

    chat, err = await resolve_chat(client, query, join_if_invite=False)
    if not chat:
        # try main bot
        chat, err = await resolve_chat(bot, query, join_if_invite=False)
    if not chat:
        return None, err or "Cannot resolve target"
    return {
        "chat_id": chat.id,
        "topic": topic_id,
        "title": chat.title or getattr(chat, "first_name", None) or str(chat.id),
        "username": getattr(chat, "username", None),
    }, None


async def _resolve_source_entry(client, message, bot, _bot_data):
    """
    Resolve one source message into {chat_id, title, last_msg_id, continuous}.
    """
    continuous = False
    last_msg_id = 1
    text = (message.text or "").strip() if message.text else ""

    if text and text.lower() in ("me", "saved"):
        if _bot_data.get("is_bot"):
            return None, "Saved Messages need a userbot session."
        # ask mode later — mark special
        return {
            "chat_id": "me",
            "title": "Saved Messages",
            "last_msg_id": None,  # filled later
            "continuous": False,
            "needs_saved_setup": True,
        }, None

    if text and not message.forward_date:
        parsed = _parse_msg_id_from_link(text)
        if parsed:
            chat_id, last_msg_id = parsed
            title = str(chat_id)
            try:
                ch = await client.get_chat(chat_id)
                title = ch.title or getattr(ch, "first_name", title)
            except Exception:
                try:
                    ch = await bot.get_chat(chat_id)
                    title = ch.title or getattr(ch, "first_name", title)
                except Exception:
                    title = "private"
            return {
                "chat_id": chat_id,
                "title": title,
                "last_msg_id": last_msg_id,
                "continuous": False,
            }, None
        # plain @ or id as source (start from recent)
        chat, err = await resolve_chat(client, text)
        if not chat:
            chat, err = await resolve_chat(bot, text)
        if not chat:
            return None, err or "Invalid source"
        # use high msg id as "up to latest"
        last_msg_id = 10_000_000
        return {
            "chat_id": chat.id,
            "title": chat.title or getattr(chat, "first_name", str(chat.id)),
            "last_msg_id": last_msg_id,
            "continuous": False,
        }, None

    if message.forward_from_chat:
        last_msg_id = message.forward_from_message_id or 10_000_000
        chat = message.forward_from_chat
        chat_id = chat.username or chat.id
        if last_msg_id is None:
            return None, "Anonymous admin forward — send last message link instead."
        return {
            "chat_id": chat_id if isinstance(chat_id, str) else chat.id,
            "title": chat.title or str(chat.id),
            "last_msg_id": last_msg_id,
            "continuous": False,
        }, None

    return None, "Send link, @username, ID, forward, or me/saved."


@Client.on_message(filters.private & filters.command(["fwd", "forward"]))
async def run(bot, message):
    user_id = message.from_user.id
    _bot = await db.get_bot(user_id)
    if not _bot:
        return await message.reply(
            "No session yet. Add bot/userbot in /settings first."
        )

    # Client for resolving chats
    client, bot_or_err = await get_client_for_user(user_id, None)
    used_main = False
    if not client:
        client = bot
        used_main = True
        bot_or_err = _bot

    sources = []
    targets = []

    try:
        # ── SOURCES ──────────────────────────────────────────────────────────
        await message.reply(
            Translation.FROM_MSG,
            reply_markup=ReplyKeyboardRemove(),
        )
        while True:
            src_msg = await bot.ask(message.chat.id, "➕ Source (or <code>done</code>):", timeout=600)
            if not src_msg:
                return await message.reply(Translation.CANCEL)
            t = (src_msg.text or "").strip().lower() if src_msg.text else ""
            if t in ("/cancel", "cancel"):
                return await message.reply(Translation.CANCEL, reply_markup=ReplyKeyboardRemove())
            if t == "done":
                if not sources:
                    await message.reply("Add at least one source first.")
                    continue
                break

            entry, err = await _resolve_source_entry(client, src_msg, bot, _bot)
            if err:
                await message.reply(f"❌ {err}")
                continue

            # Saved Messages extra steps
            if entry.get("needs_saved_setup"):
                mode_msg = await bot.ask(message.chat.id, Translation.SAVED_MSG_MODE)
                if mode_msg.text and mode_msg.text.startswith("/"):
                    return await message.reply(Translation.CANCEL)
                if mode_msg.text and ("live" in mode_msg.text.lower() or mode_msg.text.strip() == "2"):
                    entry["continuous"] = True
                    entry["last_msg_id"] = 1_000_000
                else:
                    limit_msg = await bot.ask(message.chat.id, Translation.SAVED_MSG_LIMIT)
                    if limit_msg.text and limit_msg.text.startswith("/"):
                        return await message.reply(Translation.CANCEL)
                    if limit_msg.text and limit_msg.text.lower() == "all":
                        entry["last_msg_id"] = 10_000_000
                    elif limit_msg.text and limit_msg.text.isdigit():
                        entry["last_msg_id"] = int(limit_msg.text)
                    else:
                        await message.reply("Invalid number.")
                        continue
                entry.pop("needs_saved_setup", None)

            sources.append(entry)
            await message.reply(
                f"✅ Source #{len(sources)}: <b>{entry['title']}</b>\n"
                f"Send more or <code>done</code>."
            )

        # ── TARGETS ──────────────────────────────────────────────────────────
        # Optional keyboard of saved channels + free form
        buttons = []
        btn_data = {}
        channels = await db.get_user_channels(user_id)
        for channel in channels:
            label = channel["title"]
            if channel.get("topic_id"):
                label = f"{label} · t{channel['topic_id']}"
            # avoid keyboard collision
            while label in btn_data:
                label = label + "·"
            buttons.append([KeyboardButton(label)])
            btn_data[label] = channel
        buttons.append([KeyboardButton("done")])
        buttons.append([KeyboardButton("cancel")])

        await message.reply(
            Translation.TO_MSG,
            reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=False)
            if buttons else ReplyKeyboardRemove(),
        )

        while True:
            tgt_msg = await bot.ask(message.chat.id, "➕ Target (or <code>done</code>):", timeout=600)
            if not tgt_msg:
                return await message.reply(Translation.CANCEL, reply_markup=ReplyKeyboardRemove())
            t = (tgt_msg.text or "").strip() if tgt_msg.text else ""
            tl = t.lower()
            if tl in ("/cancel", "cancel"):
                return await message.reply(Translation.CANCEL, reply_markup=ReplyKeyboardRemove())
            if tl == "done":
                if not targets:
                    await message.reply("Add at least one target first.")
                    continue
                break

            # Saved channel button
            if t in btn_data:
                ch = btn_data[t]
                targets.append({
                    "chat_id": ch["chat_id"],
                    "topic": ch.get("topic_id"),
                    "title": ch.get("title") or str(ch["chat_id"]),
                    "username": None,
                })
                await message.reply(
                    f"✅ Target #{len(targets)}: <b>{targets[-1]['title']}</b>\n"
                    f"More or <code>done</code>."
                )
                continue

            entry, err = await _resolve_target_entry(client, tgt_msg if not t else t, bot)
            if err:
                await message.reply(f"❌ {err}")
                continue
            targets.append(entry)
            topic_note = f" / topic {entry['topic']}" if entry.get("topic") else ""
            await message.reply(
                f"✅ Target #{len(targets)}: <b>{entry['title']}</b>{topic_note}\n"
                f"More or <code>done</code>."
            )

        await message.reply("…", reply_markup=ReplyKeyboardRemove())

        # ── SKIP + MODE ──────────────────────────────────────────────────────
        skipno = await bot.ask(message.chat.id, Translation.SKIP_MSG)
        if skipno.text and skipno.text.startswith("/"):
            return await message.reply(Translation.CANCEL)
        try:
            skip_n = int((skipno.text or "0").strip())
        except ValueError:
            skip_n = 0

        mode_ask = await bot.ask(
            message.chat.id,
            "<b>✦ Transfer mode</b>\n\n"
            "• <code>auto</code> — copy, then download if restricted\n"
            "• <code>download</code> — always download+reupload\n\n"
            "Send <code>auto</code> or <code>download</code>",
        )
        if mode_ask.text and mode_ask.text.startswith("/"):
            return await message.reply(Translation.CANCEL)
        force_download = "download" in (mode_ask.text or "").lower() or (mode_ask.text or "").strip() == "2"

        # ── CONFIRM ──────────────────────────────────────────────────────────
        from_lines = "\n".join(
            f"• <code>{s['title']}</code> (up to msg {s['last_msg_id']})" for s in sources
        )
        to_lines = "\n".join(
            f"• <code>{t['title']}</code>"
            + (f" topic/{t['topic']}" if t.get("topic") else "")
            for t in targets
        )
        continuous = any(s.get("continuous") for s in sources)
        pairs = len(sources) * len(targets)

        forward_id = f"{user_id}-{skipno.id}"
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes — start", callback_data=f"start_public_{forward_id}"),
            InlineKeyboardButton("No", callback_data="close_btn"),
        ]])

        await message.reply_text(
            Translation.DOUBLE_CHECK.format(
                botname=_bot.get("name", "bot"),
                botuname=_bot.get("username", "bot"),
                from_chat=from_lines,
                to_chat=to_lines,
                skip=skip_n,
            )
            + f"\n\n<b>Mode:</b> <code>{'download' if force_download else 'auto'}</code>"
            + f"\n<b>Jobs:</b> <code>{pairs}</code> (sources × targets)"
            + (f"\n<b>Live continuous:</b> yes" if continuous else ""),
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )

        # Use first source/target as primary STS fields; lists for multi loop
        first_src = sources[0]
        first_tgt = targets[0]
        # last_msg_id used as limit upper bound in iter_messages
        STS(forward_id).store(
            first_src["chat_id"],
            first_tgt["chat_id"],
            skip_n,
            int(first_src["last_msg_id"]),
            continuous=continuous,
            to_topic=first_tgt.get("topic"),
            force_download=force_download,
            from_list=sources,
            to_list=targets,
        )
    finally:
        if not used_main and client and client is not bot:
            try:
                await client.stop()
            except Exception:
                pass
