import asyncio
import re
import logging
from datetime import datetime, timezone
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from pyrogram.errors import FloodWait
from database import db
from config import temp
from .test import CLIENT as _CLIENT_CLASS, start_clone_bot
from .utils import format_chat_label

CLIENT = _CLIENT_CLASS()

logger = logging.getLogger(__name__)

# ── In-memory schedule task registry ──────────────────────────────────────────
# sched_id -> asyncio.Task
SCHEDULE_TASKS = {}


def _slugify_channel(title: str, max_len: int = 24) -> str:
    """Human-readable schedule id base from channel title."""
    if not title:
        return "chat"
    s = re.sub(r"[^a-zA-Z0-9]+", "_", str(title).strip().lower()).strip("_")
    if not s:
        s = "chat"
    return s[:max_len]


async def _unique_sched_id(user_id: int, from_title: str, from_id) -> str:
    """sched_id = source channel name (slug). Append _2, _3 on clash."""
    base = _slugify_channel(from_title or str(from_id))
    candidate = base
    n = 2
    while await db.sched.find_one({"user_id": user_id, "sched_id": candidate}):
        candidate = f"{base}_{n}"
        n += 1
        if n > 999:
            candidate = f"{base}_{from_id}"
            break
    return candidate


async def _resolve_chat_meta(client, chat_ref):
    """Return (chat_id, title, username) for id / @user / link / title."""
    try:
        chat = await client.get_chat(chat_ref)
        title = chat.title or getattr(chat, "first_name", None) or str(chat.id)
        return chat.id, title, getattr(chat, "username", None)
    except Exception:
        try:
            cid = int(chat_ref)
            return cid, str(cid), None
        except (TypeError, ValueError):
            return None, None, None


def _sched_label(data: dict) -> str:
    """Readable route for alerts: SourceName → TargetName."""
    sid = data.get("sched_id", "?")
    ft = data.get("from_title") or str(data.get("from_id", "?"))
    tt = data.get("to_title") or str(data.get("to_id", "?"))
    return f"<code>{sid}</code> · <b>{ft}</b> → <b>{tt}</b>"


# ══════════════════════════════════════════════════════════════════════════════
# NAMED CONNECTIONS
# ══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.private & filters.command("addconn"))
async def add_connection(bot, message: Message):
    """Save current bot/session under a name with a type tag."""
    user_id = message.from_user.id
    args = message.text.split()
    if len(args) < 2:
        return await message.reply(
            "<b>Usage:</b> <code>/addconn name type</code>\n\n"
            "Types:\n"
            "• <code>live</code> — for live forwarding\n"
            "• <code>schedule</code> — for scheduled forwarding\n"
            "• <code>batch</code> — for batch forwarding\n"
            "• <code>all</code> — usable for any type\n\n"
            "Example: <code>/addconn main live</code>"
        )
    name = args[1].strip().lower().replace(" ", "_")
    if len(name) > 20:
        return await message.reply("Name too long. Max 20 characters.")

    conn_type = args[2].strip().lower() if len(args) >= 3 else "all"
    if conn_type not in ("live", "schedule", "batch", "all"):
        return await message.reply(
            "Invalid type. Use: <code>live</code>, <code>schedule</code>, <code>batch</code>, or <code>all</code>"
        )

    bot_data = await db.get_bot(user_id)
    if not bot_data:
        return await message.reply("No bot/session configured yet.\nUse /settings to add one first.")

    await db.save_connection(user_id, name, bot_data, conn_type=conn_type)
    kind = "🤖 Bot" if bot_data.get("is_bot") else "👤 Userbot"
    await message.reply(
        f"✅ <b>Connection saved</b>\n\n"
        f"Name: <code>{name}</code>\n"
        f"Type: <code>{conn_type}</code>\n"
        f"{kind}: @{bot_data.get('username', 'unknown')}\n\n"
        f"Use /listconn to manage."
    )


async def get_conn_for(user_id: int, conn_type: str):
    """Return list of saved connections matching a type (or 'all')."""
    conns = await db.get_all_connections(user_id)
    result = []
    async for c in conns:
        t = c.get("conn_type", "all")
        if t in (conn_type, "all"):
            result.append(c)
    return result


@Client.on_message(filters.private & filters.command("listconn"))
async def list_connections(bot, message: Message):
    """List all saved named connections."""
    user_id = message.from_user.id
    conns = await db.get_all_connections(user_id)
    conn_list = [c async for c in conns]

    if not conn_list:
        return await message.reply(
            "No saved connections.\n"
            "Use /addconn <name> to save your current bot/session."
        )

    text = "<b>📋 Saved Connections:</b>\n\n"
    buttons = []
    for c in conn_list:
        name = c["name"]
        bd = c.get("bot_data", {})
        is_bot = bd.get("is_bot", False)
        uname = bd.get("username", "unknown")
        conn_type = c.get("conn_type", "all")
        kind = "🤖 Bot" if is_bot else "👤 Userbot"
        text += f"• <code>{name}</code> — {kind} (@{uname}) | type: <code>{conn_type}</code>\n"
        buttons.append([
            InlineKeyboardButton(f"✅ Use {name}", callback_data=f"useconn#{name}#{user_id}"),
            InlineKeyboardButton(f"🗑 Delete", callback_data=f"delconn#{name}#{user_id}")
        ])

    await message.reply(text, reply_markup=InlineKeyboardMarkup(buttons))


@Client.on_message(filters.private & filters.command("useconn"))
async def use_connection(bot, message: Message):
    """Switch active bot/session to a saved connection: /useconn <name>"""
    user_id = message.from_user.id
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply("<b>Usage:</b> <code>/useconn name</code>")

    name = args[1].strip().lower()
    conn = await db.get_connection(user_id, name)
    if not conn:
        return await message.reply(
            f"No connection named <code>{name}</code>.\n"
            f"Use /listconn to see saved connections."
        )

    bot_data = conn["bot_data"]
    bot_data["user_id"] = user_id
    await db.remove_bot(user_id)
    await db.add_bot(bot_data)
    uname = bot_data.get("username", "unknown")
    kind = "Bot" if bot_data.get("is_bot") else "Userbot"
    await message.reply(
        f"✅ <b>Switched to:</b> <code>{name}</code>\n"
        f"{kind}: @{uname}\n\n"
        f"All forwards will now use this connection."
    )


@Client.on_message(filters.private & filters.command("delconn"))
async def del_connection(bot, message: Message):
    """Delete a saved connection: /delconn <name>"""
    user_id = message.from_user.id
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply("<b>Usage:</b> <code>/delconn name</code>")

    name = args[1].strip().lower()
    conn = await db.get_connection(user_id, name)
    if not conn:
        return await message.reply(f"No connection named <code>{name}</code>.")

    await db.delete_connection(user_id, name)
    await message.reply(f"🗑 Connection <code>{name}</code> deleted.")


@Client.on_callback_query(filters.regex(r'^useconn#'))
async def useconn_btn(bot, query):
    _, name, uid = query.data.split("#")
    if int(uid) != query.from_user.id:
        return await query.answer("Not your connection.", show_alert=True)
    user_id = int(uid)
    conn = await db.get_connection(user_id, name)
    if not conn:
        return await query.answer("Connection not found.", show_alert=True)
    bot_data = conn["bot_data"]
    bot_data["user_id"] = user_id
    await db.remove_bot(user_id)
    await db.add_bot(bot_data)
    await query.answer(f"Switched to: {name}", show_alert=True)
    await query.message.edit(f"✅ Now using connection: <code>{name}</code>")


@Client.on_callback_query(filters.regex(r'^delconn#'))
async def delconn_btn(bot, query):
    _, name, uid = query.data.split("#")
    if int(uid) != query.from_user.id:
        return await query.answer("Not your connection.", show_alert=True)
    await db.delete_connection(int(uid), name)
    await query.answer(f"Deleted: {name}", show_alert=True)
    await query.message.edit(f"🗑 Connection <code>{name}</code> deleted.")


# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULED FORWARDING
# ══════════════════════════════════════════════════════════════════════════════

PENDING_SCHEDULE = {}  # user_id -> partial schedule data


@Client.on_message(filters.private & filters.command("schedule"))
async def schedule_cmd(bot, message: Message):
    """Guided flow to set up a scheduled forward."""
    user_id = message.from_user.id

    bot_data = await db.get_bot(user_id)
    if not bot_data:
        return await message.reply("No bot/session configured. Use /settings first.")

    PENDING_SCHEDULE[user_id] = {"step": "from_id"}
    await message.reply(
        "<b>📅 Schedule Forward Setup</b>\n\n"
        "<b>Step 1/5:</b> Send the <b>source</b> chat\n"
        "Accepts: <code>-100…</code> · <code>@channel</code> · <code>https://t.me/…</code>\n\n"
        "Schedule ID = source channel name. Target topic: <code>-100CHAT/TOPIC</code>\n"
        "Last step picks auto or download (restricted-safe).\n\n"
        "/cancel — cancel"
    )


@Client.on_message(filters.private & ~filters.command([
    "start", "fwd", "forward", "settings", "reset", "status",
    "live", "stoplive", "lives", "cancel", "addconn", "listconn", "useconn",
    "delconn", "schedule", "listschedule", "cancelschedule", "help",
    "broadcast", "resetall", "unequify", "save", "batchsave", "savelink"
]))
async def schedule_flow(bot, message: Message):
    """Handle multi-step schedule setup conversation."""
    user_id = message.from_user.id

    # Don't steal live guided setup
    try:
        from plugins.regix import PENDING_LIVE
        if user_id in PENDING_LIVE:
            return
    except Exception:
        pass

    # Don't steal save-restricted flow
    try:
        from plugins.save_restricted import PENDING_SAVE
        if user_id in PENDING_SAVE:
            return
    except Exception:
        pass

    if user_id not in PENDING_SCHEDULE:
        return

    state = PENDING_SCHEDULE[user_id]
    step = state.get("step")
    text = message.text.strip() if message.text else ""

    if step == "from_id":
        bot_data = await db.get_bot(user_id)
        client = None
        try:
            client = await start_clone_bot(CLIENT.client(bot_data))
            from_id, from_title, from_username = await _resolve_chat_meta(client, text)
        except Exception as e:
            return await message.reply(f"Cannot resolve source: <code>{e}</code>")
        finally:
            if client:
                try:
                    await client.stop()
                except Exception:
                    pass
        if from_id is None:
            return await message.reply(
                "Invalid source. Send chat ID, @username, or t.me link."
            )
        state["from_id"] = from_id
        state["from_title"] = from_title
        state["from_username"] = from_username
        state["step"] = "to_id"
        return await message.reply(
            f"✅ Source: {format_chat_label(title=from_title, username=from_username, chat_id=from_id)}\n\n"
            f"<b>Step 2/4:</b> Send the <b>target</b> chat\n"
            f"Accepts: <code>-100…</code> · <code>@channel</code> · link"
        )

    if step == "to_id":
        from .transfer import parse_dest
        dest_raw, topic_from_text = parse_dest(text)
        resolve_ref = dest_raw if dest_raw is not None else text
        bot_data = await db.get_bot(user_id)
        client = None
        try:
            client = await start_clone_bot(CLIENT.client(bot_data))
            to_id, to_title, to_username = await _resolve_chat_meta(client, resolve_ref)
        except Exception as e:
            return await message.reply(f"Cannot resolve target: <code>{e}</code>")
        finally:
            if client:
                try:
                    await client.stop()
                except Exception:
                    pass
        if to_id is None:
            return await message.reply(
                "Invalid target. Send chat ID, @username, link, or <code>-100CHAT/TOPIC_ID</code>"
            )
        state["to_id"] = to_id
        state["to_title"] = to_title
        state["to_username"] = to_username
        state["to_topic"] = topic_from_text
        state["step"] = "interval"
        topic_note = f" · topic <code>{topic_from_text}</code>" if topic_from_text else ""
        return await message.reply(
            f"✅ Target: {format_chat_label(title=to_title, username=to_username, chat_id=to_id)}{topic_note}\n\n"
            f"<b>Step 3/5:</b> How often?\n"
            f"• <code>30m</code> · <code>6h</code> · <code>1d</code>"
        )

    if step == "interval":
        seconds = _parse_interval(text)
        if not seconds:
            return await message.reply(
                "Invalid format. Use <code>30m</code>, <code>6h</code>, or <code>1d</code>"
            )
        state["interval_seconds"] = seconds
        state["interval_label"] = text
        state["step"] = "start_id"
        return await message.reply(
            "<b>Step 4/5:</b> Start message ID (resume point).\n"
            "Example: <code>1</code>\n\n"
            "Continues from last forwarded message each run."
        )

    if step == "start_id":
        try:
            start_id = int(text)
        except ValueError:
            return await message.reply("Invalid message ID. Send a number like <code>1</code>")
        state["start_id"] = start_id
        state["step"] = "mode"
        return await message.reply(
            "<b>Step 5/5:</b> Transfer mode\n\n"
            "• <code>auto</code> — copy first, download+reupload if restricted\n"
            "• <code>download</code> — always download+reupload (restricted-safe)\n\n"
            "Send <code>auto</code> or <code>download</code>"
        )

    if step == "mode":
        mode = text.strip().lower()
        if mode not in ("auto", "download", "1", "2"):
            return await message.reply("Send <code>auto</code> or <code>download</code>")
        force_download = mode in ("download", "2")
        start_id = state["start_id"]

        from_title = state.get("from_title") or str(state["from_id"])
        to_title = state.get("to_title") or str(state["to_id"])
        sched_id = await _unique_sched_id(user_id, from_title, state["from_id"])
        topic_id = state.get("to_topic")
        sched_data = {
            "from_id": state["from_id"],
            "to_id": state["to_id"],
            "to_topic": topic_id,
            "from_title": from_title,
            "to_title": to_title,
            "from_username": state.get("from_username"),
            "to_username": state.get("to_username"),
            "interval_seconds": state["interval_seconds"],
            "interval_label": state["interval_label"],
            "from_msg": start_id,
            "last_forwarded_id": start_id - 1,
            "force_download": force_download,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        await db.save_schedule(user_id, sched_id, sched_data)
        PENDING_SCHEDULE.pop(user_id, None)

        task = asyncio.create_task(
            _run_schedule(bot, user_id, sched_id, sched_data)
        )
        SCHEDULE_TASKS[sched_id] = task

        topic_note = f" · topic <code>{topic_id}</code>" if topic_id else ""
        mode_note = "download" if force_download else "auto"
        await message.reply(
            f"✅ <b>Schedule created!</b>\n\n"
            f"ID: <code>{sched_id}</code>  ← source name\n"
            f"From: {format_chat_label(title=from_title, username=state.get('from_username'), chat_id=state['from_id'])}\n"
            f"To: {format_chat_label(title=to_title, username=state.get('to_username'), chat_id=state['to_id'])}{topic_note}\n"
            f"Start msg ID: <code>{start_id}</code>\n"
            f"Interval: every <b>{state['interval_label']}</b>\n"
            f"Mode: <code>{mode_note}</code>\n\n"
            f"Use /listschedule to manage."
        )


def _parse_interval(text: str) -> int:
    """Parse interval string to seconds. Returns 0 on invalid."""
    text = text.strip().lower()
    try:
        if text.endswith("m"):
            return int(text[:-1]) * 60
        elif text.endswith("h"):
            return int(text[:-1]) * 3600
        elif text.endswith("d"):
            return int(text[:-1]) * 86400
    except ValueError:
        pass
    return 0


async def _run_schedule(bot, user_id: int, sched_id: str, data: dict):
    """Run a scheduled forward, always continuing from last forwarded message."""
    from_id = data["from_id"]
    to_id = data["to_id"]
    interval = data["interval_seconds"]
    # Enrich titles if old schedules lack them
    if not data.get("from_title"):
        data["from_title"] = str(from_id)
    if not data.get("to_title"):
        data["to_title"] = str(to_id)

    bot_data = await db.get_bot(user_id)
    if not bot_data:
        logger.error(f"[Schedule {sched_id}] No bot found for user {user_id}")
        SCHEDULE_TASKS.pop(sched_id, None)
        return

    if bot_data.get("is_bot"):
        await bot.send_message(
            user_id,
            f"⚠️ <b>Schedule</b> {_sched_label(data)}\n"
            f"Active connection is a bot token — cannot read restricted channels.\n"
            f"Add a userbot session via /settings or /addconn and use /useconn."
        )

    try:
        client = await start_clone_bot(CLIENT.client(bot_data))
    except Exception as e:
        logger.error(f"[Schedule {sched_id}] Client start error: {e}")
        await bot.send_message(
            user_id,
            f"❌ Schedule {_sched_label(data)} failed to start client: <code>{e}</code>"
        )
        SCHEDULE_TASKS.pop(sched_id, None)
        return

    # Fill missing titles from live client once
    try:
        if data.get("from_title") == str(from_id) or not data.get("from_title"):
            _, ft, fu = await _resolve_chat_meta(client, from_id)
            if ft:
                data["from_title"] = ft
                data["from_username"] = fu
                await db.save_schedule(user_id, sched_id, data)
        if data.get("to_title") == str(to_id) or not data.get("to_title"):
            _, tt, tu = await _resolve_chat_meta(client, to_id)
            if tt:
                data["to_title"] = tt
                data["to_username"] = tu
                await db.save_schedule(user_id, sched_id, data)
    except Exception:
        pass

    try:
        while sched_id in SCHEDULE_TASKS:
            forwarded = 0
            failed = 0
            try:
                fresh = await db.sched.find_one({"sched_id": sched_id})
                if fresh:
                    data = fresh
                    if not data.get("from_title"):
                        data["from_title"] = str(from_id)
                    if not data.get("to_title"):
                        data["to_title"] = str(to_id)

                resume_from = data.get("last_forwarded_id", data.get("from_msg", 1) - 1) + 1

                latest_id = resume_from - 1
                async for msg in client.get_chat_history(from_id, limit=1):
                    latest_id = msg.id

                if resume_from > latest_id:
                    await bot.send_message(
                        user_id,
                        f"📅 <b>Schedule</b> {_sched_label(data)} — No new messages. "
                        f"Next check in <b>{data['interval_label']}</b>"
                    )
                else:
                    from .transfer import transfer_message
                    force_dl = bool(data.get("force_download"))
                    to_topic = data.get("to_topic")
                    for msg_id in range(resume_from, latest_id + 1):
                        if sched_id not in SCHEDULE_TASKS:
                            break
                        try:
                            ok, how = await transfer_message(
                                client, None, to_id,
                                from_chat_id=from_id,
                                message_id=msg_id,
                                topic_id=to_topic,
                                uploader=bot,
                                force_download=force_dl,
                            )
                            if ok:
                                forwarded += 1
                                await db.save_schedule(user_id, sched_id, {**data, "last_forwarded_id": msg_id})
                            else:
                                err_str = str(how).lower()
                                fatal_keywords = [
                                    "copyright", "banned", "blocked", "channel_private",
                                    "chat_write_forbidden", "user_banned_in_channel",
                                    "chat_admin_required", "forbidden", "not accessible",
                                    "channelprivate", "chatwriteforbidden", "userbannedinchannel"
                                ]
                                if any(k in err_str for k in fatal_keywords):
                                    raise RuntimeError(how)
                                logger.error(f"[Schedule {sched_id}] msg {msg_id}: {how}")
                                failed += 1
                            await asyncio.sleep(0.5)
                        except FloodWait as e:
                            await bot.send_message(
                                user_id,
                                f"⚠️ <b>Schedule FloodWait</b> {_sched_label(data)}\n"
                                f"Sleeping {e.value}s. Progress saved at msg {msg_id}."
                            )
                            await asyncio.sleep(e.value)
                            try:
                                ok, how = await transfer_message(
                                    client, None, to_id,
                                    from_chat_id=from_id,
                                    message_id=msg_id,
                                    topic_id=to_topic,
                                    uploader=bot,
                                    force_download=force_dl,
                                )
                                if ok:
                                    forwarded += 1
                                    await db.save_schedule(user_id, sched_id, {**data, "last_forwarded_id": msg_id})
                                else:
                                    failed += 1
                            except Exception as retry_e:
                                logger.error(f"[Schedule {sched_id}] Retry failed msg {msg_id}: {retry_e}")
                                failed += 1
                        except Exception as e:
                            err_str = str(e).lower()
                            fatal_keywords = [
                                "copyright", "banned", "blocked", "channel_private",
                                "chat_write_forbidden", "user_banned_in_channel",
                                "chat_admin_required", "forbidden", "not accessible",
                                "channelprivate", "chatwriteforbidden", "userbannedinchannel"
                            ]
                            if any(k in err_str for k in fatal_keywords):
                                logger.error(f"[Schedule {sched_id}] Fatal error: {e}")
                                await bot.send_message(
                                    user_id,
                                    f"🚫 <b>Schedule Stopped — Channel Problem</b>\n\n"
                                    f"{_sched_label(data)}\n"
                                    f"From ID: <code>{from_id}</code>\n"
                                    f"To ID: <code>{to_id}</code>"
                                    + (f"/{data.get('to_topic')}" if data.get('to_topic') else "") + "\n\n"
                                    f"<b>Reason:</b> <code>{e}</code>\n\n"
                                    f"Source <b>{data.get('from_title', from_id)}</b> may be blocked, "
                                    f"copyright struck, or access lost.\n"
                                    f"Schedule removed."
                                )
                                await _cancel_schedule(sched_id)
                                return
                            logger.error(f"[Schedule {sched_id}] msg {msg_id} error: {e}")
                            failed += 1

                    await bot.send_message(
                        user_id,
                        f"✅ <b>Schedule Run Complete</b> {_sched_label(data)}\n"
                        f"Forwarded: {forwarded} | Failed: {failed}\n"
                        f"Last msg ID: <code>{latest_id}</code>\n"
                        f"Next run in: <b>{data['interval_label']}</b>"
                    )

            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception as e:
                err_str = str(e).lower()
                fatal_keywords = [
                    "copyright", "banned", "blocked", "channel_private",
                    "chat_write_forbidden", "user_banned_in_channel",
                    "chat_admin_required", "forbidden", "not accessible",
                    "channelprivate", "chatwriteforbidden", "userbannedinchannel"
                ]
                if any(k in err_str for k in fatal_keywords):
                    logger.error(f"[Schedule {sched_id}] Fatal outer error: {e}")
                    await bot.send_message(
                        user_id,
                        f"🚫 <b>Schedule Stopped — Channel Problem</b>\n\n"
                        f"{_sched_label(data)}\n"
                        f"From ID: <code>{from_id}</code>\n"
                        f"To ID: <code>{to_id}</code>\n\n"
                        f"<b>Reason:</b> <code>{e}</code>\n\n"
                        f"Source <b>{data.get('from_title', from_id)}</b> may be blocked, "
                        f"copyright struck, or access lost.\n"
                        f"Schedule removed."
                    )
                    await _cancel_schedule(sched_id)
                    return
                logger.error(f"[Schedule {sched_id}] Run error: {e}")
                await bot.send_message(
                    user_id,
                    f"⚠️ Schedule {_sched_label(data)} error: <code>{e}</code>"
                )

            await asyncio.sleep(interval)

    finally:
        try:
            await client.stop()
        except Exception:
            pass
        SCHEDULE_TASKS.pop(sched_id, None)


@Client.on_message(filters.private & filters.command("listschedule"))
async def list_schedules(bot, message: Message):
    """List all active schedules for the user."""
    user_id = message.from_user.id
    scheds = await db.get_all_schedules(user_id)
    sched_list = [s async for s in scheds]

    if not sched_list:
        return await message.reply(
            "No active schedules.\n"
            "Use /schedule to create one."
        )

    text = "<b>📅 Active Schedules:</b>\n\n"
    buttons = []
    for s in sched_list:
        sid = s["sched_id"]
        running = "🟢" if sid in SCHEDULE_TASKS else "🔴"
        last = s.get("last_forwarded_id", s.get("from_msg", 1) - 1)
        ft = s.get("from_title") or str(s["from_id"])
        tt = s.get("to_title") or str(s["to_id"])
        topic = s.get("to_topic")
        mode = "download" if s.get("force_download") else "auto"
        topic_s = f"/{topic}" if topic else ""
        text += (
            f"{running} <code>{sid}</code>\n"
            f"  <b>{ft}</b> → <b>{tt}</b>{topic_s}\n"
            f"  <code>{s['from_id']}</code> → <code>{s['to_id']}{topic_s}</code>\n"
            f"  Every {s['interval_label']} | Last msg: <code>{last}</code> | mode: <code>{mode}</code>\n\n"
        )
        # Button shows source name so blocked channel is obvious
        btn_label = f"❌ {sid}"[:64]
        buttons.append([InlineKeyboardButton(
            btn_label, callback_data=f"cancelsched#{sid}#{user_id}"
        )])

    await message.reply(text, reply_markup=InlineKeyboardMarkup(buttons))


@Client.on_message(filters.private & filters.command("cancelschedule"))
async def cancel_schedule_cmd(bot, message: Message):
    """Cancel a schedule: /cancelschedule <sched_id>"""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply("<b>Usage:</b> <code>/cancelschedule sched_id</code>")
    sched_id = args[1].strip()
    await _cancel_schedule(sched_id)
    await message.reply(f"❌ Schedule <code>{sched_id}</code> cancelled.")


@Client.on_callback_query(filters.regex(r'^cancelsched#'))
async def cancelsched_btn(bot, query):
    _, sched_id, uid = query.data.split("#")
    if int(uid) != query.from_user.id:
        return await query.answer("Not your schedule.", show_alert=True)
    await _cancel_schedule(sched_id)
    await query.answer("Cancelled.", show_alert=True)
    await query.message.edit(f"❌ Schedule <code>{sched_id}</code> cancelled.")


async def _cancel_schedule(sched_id: str):
    task = SCHEDULE_TASKS.pop(sched_id, None)
    if task:
        task.cancel()
    await db.delete_schedule(sched_id)


# ── Restore schedules on startup ───────────────────────────────────────────────

async def restore_schedules(bot):
    """Called on startup — restores all saved schedules from DB."""
    try:
        scheds = await db.get_all_schedules()
        count = 0
        async for s in scheds:
            sched_id = s["sched_id"]
            user_id = s["user_id"]
            if sched_id in SCHEDULE_TASKS:
                continue
            task = asyncio.create_task(_run_schedule(bot, user_id, sched_id, s))
            SCHEDULE_TASKS[sched_id] = task
            count += 1
        if count:
            logger.info(f"[AutoRestart] Restored {count} schedule(s) from DB.")
    except Exception as e:
        logger.error(f"[AutoRestart] restore_schedules error: {e}")
