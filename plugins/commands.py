import os
import sys
import asyncio
from database import db, mongodb_version
from config import Config, temp
from platform import python_version
from translation import Translation
from pyrogram import Client, filters, enums, __version__ as pyrogram_version
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def get_main_menu():
    """Elegant full feature panel."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚀 Forward", callback_data="menu#forward"),
            InlineKeyboardButton("🔴 Live", callback_data="menu#live"),
        ],
        [
            InlineKeyboardButton("📅 Schedule", callback_data="menu#schedule"),
            InlineKeyboardButton("💾 Save", callback_data="menu#save"),
        ],
        [
            InlineKeyboardButton("🔌 Connections", callback_data="menu#conn"),
            InlineKeyboardButton("⚙️ Settings", callback_data="settings#main"),
        ],
        [
            InlineKeyboardButton("📊 Status", callback_data="menu#status"),
            InlineKeyboardButton("🧹 Unequify", callback_data="menu#unequify"),
        ],
        [
            InlineKeyboardButton("📜 Help", callback_data="menu#help"),
            InlineKeyboardButton("ℹ️ About", callback_data="menu#about"),
        ],
        [
            InlineKeyboardButton("⏹ Cancel batches", callback_data="menu#cancel"),
        ],
    ])


def get_back_to_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↩ Menu", callback_data="menu#main")]
    ])


main_buttons = [
    [InlineKeyboardButton("✦ Open Menu", callback_data="menu#main")],
]


async def build_full_status(user_id: int) -> str:
    """All jobs: batch, live, schedules, connections."""
    from plugins.connections import SCHEDULE_TASKS
    from plugins.utils import STS

    users_count, bots_count = await db.total_users_bots_count()
    total_channels = await db.total_channels()

    # batch
    active_batches = [
        fid for fid in temp.lock.get(user_id, [])
        if not temp.CANCEL.get(fid)
    ]
    batch_lines = []
    for fid in active_batches:
        sts = STS(fid)
        if sts.verify():
            i = sts.get(full=True)
            total = i.total if i.total else 1
            pct = int((i.fetched or 0) * 100 / max(total, 1))
            batch_lines.append(
                f"  · <code>{fid}</code> — {pct}% "
                f"({i.fetched}/{i.total}) → <code>{i.TO}</code>"
            )

    # live
    live = [
        (k, v) for k, v in temp.LIVE_WATCHERS.items()
        if isinstance(k, tuple) and len(k) == 2 and v.get("user_id") == user_id
    ]
    live_lines = []
    for (fid, tid), meta in live:
        ft = meta.get("from_title") or fid
        tt = meta.get("to_title") or tid
        live_lines.append(f"  · <b>{ft}</b> → <b>{tt}</b>")

    # schedules
    sched_lines = []
    try:
        cursor = await db.get_all_schedules(user_id)
        async for s in cursor:
            sid = s["sched_id"]
            run = "●" if sid in SCHEDULE_TASKS else "○"
            ft = s.get("from_title") or s["from_id"]
            tt = s.get("to_title") or s["to_id"]
            mode = "dl" if s.get("force_download") else "auto"
            sched_lines.append(
                f"  {run} <code>{sid}</code> · {ft} → {tt} · every {s.get('interval_label')} · {mode}"
            )
    except Exception:
        pass

    # connections
    conn_n = 0
    try:
        ccur = await db.get_all_connections(user_id)
        conn_n = len([c async for c in ccur])
    except Exception:
        pass

    text = (
        f"<b>╭─── Status ───╮</b>\n"
        f"│ Users <code>{users_count}</code> · Sessions <code>{bots_count}</code>\n"
        f"│ Saved channels <code>{total_channels}</code> · Conns <code>{conn_n}</code>\n"
        f"│ Global batch counter <code>{getattr(temp, 'forwardings', 0)}</code>\n"
        f"<b>├── Batch ({len(active_batches)})</b>\n"
    )
    text += ("\n".join(batch_lines) + "\n") if batch_lines else "  · none\n"
    text += f"<b>├── Live ({len(live)})</b>\n"
    text += ("\n".join(live_lines) + "\n") if live_lines else "  · none\n"
    text += f"<b>├── Schedules ({len(sched_lines)})</b>\n"
    text += ("\n".join(sched_lines) + "\n") if sched_lines else "  · none\n"
    text += "<b>╰──────────────╯</b>\n\n"
    text += "<i>Progress stays in this chat only — not Saved Messages.</i>"
    return text


@Client.on_message(filters.private & filters.command(["start", "menu"]))
async def start_handler(client, message):
    user = message.from_user
    if not await db.is_user_exist(user.id):
        await db.add_user(user.id, user.first_name)
    await client.send_message(
        chat_id=message.chat.id,
        text=Translation.START_TXT.format(user.first_name),
        reply_markup=get_main_menu(),
        disable_web_page_preview=True,
    )


@Client.on_message(filters.private & filters.command("help"))
async def help_cmd(client, message):
    await message.reply_text(
        Translation.HELP_TXT,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❓ How to use", callback_data="menu#how")],
            [InlineKeyboardButton("↩ Menu", callback_data="menu#main")],
        ]),
        disable_web_page_preview=True,
    )


@Client.on_message(filters.private & filters.command("status"))
async def status_cmd(client, message):
    text = await build_full_status(message.from_user.id)
    await message.reply_text(
        text,
        reply_markup=get_back_to_menu(),
        disable_web_page_preview=True,
    )


@Client.on_message(filters.private & filters.command("cancel"))
async def cancel_forward(client, message):
    user_id = message.from_user.id
    active = temp.lock.get(user_id, [])
    if not active:
        return await message.reply("No active batch forwards.")
    for fid in list(active):
        temp.CANCEL[fid] = True
    await message.reply(f"⏹ Cancelling {len(active)} batch job(s)…")


@Client.on_message(filters.private & filters.command("restart") & filters.user(Config.BOT_OWNER_ID))
async def restart(client, message):
    msg = await message.reply_text("<i>Restarting…</i>")
    await asyncio.sleep(2)
    await msg.edit("<i>Restarted.</i>")
    os.execl(sys.executable, sys.executable, *sys.argv)


@Client.on_callback_query(filters.regex(r"^menu#"))
async def menu_router(bot, query):
    data = query.data.split("#", 1)[1]
    user_id = query.from_user.id

    if data == "main":
        await query.message.edit_text(
            Translation.START_TXT.format(query.from_user.first_name),
            reply_markup=get_main_menu(),
            disable_web_page_preview=True,
        )

    elif data == "help":
        await query.message.edit_text(
            Translation.HELP_TXT,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❓ How to use", callback_data="menu#how")],
                [InlineKeyboardButton("↩ Menu", callback_data="menu#main")],
            ]),
            disable_web_page_preview=True,
        )

    elif data == "how":
        await query.message.edit_text(
            Translation.HOW_USE_TXT,
            reply_markup=get_back_to_menu(),
            disable_web_page_preview=True,
        )

    elif data == "about":
        await query.message.edit_text(
            Translation.ABOUT_TXT.format(
                python_version=python_version(),
                pyrogram_version=pyrogram_version,
                mongodb_version=await mongodb_version(),
            ),
            reply_markup=get_back_to_menu(),
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
        )

    elif data == "status":
        text = await build_full_status(user_id)
        await query.message.edit_text(
            text,
            reply_markup=get_back_to_menu(),
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
        )

    elif data == "forward":
        await query.message.edit_text(
            "<b>✦ Batch Forward</b>\n\n"
            "Multi <b>source</b> × multi <b>target</b>.\n"
            "Any chat — not only saved channels.\n\n"
            "Send <code>/forward</code> or <code>/fwd</code>\n\n"
            "Flow: sources → <code>done</code> → targets → <code>done</code> → skip → mode → confirm",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶ Start /forward", callback_data="menu#fwd_hint")],
                [InlineKeyboardButton("↩ Menu", callback_data="menu#main")],
            ]),
        )

    elif data == "fwd_hint":
        await query.answer("Type /forward in chat", show_alert=True)

    elif data == "live":
        await query.message.edit_text(
            "<b>✦ Live mirror</b>\n\n"
            "• <code>/live</code> — guided setup\n"
            "• <code>/live source target [download]</code>\n"
            "• <code>/lives</code> — list\n"
            "• <code>/stoplive</code> — stop\n\n"
            "Topic target: <code>-100CHAT/TOPIC</code>\n"
            "Restricted-safe transfer built-in.",
            reply_markup=get_back_to_menu(),
        )

    elif data == "schedule":
        await query.message.edit_text(
            "<b>✦ Schedule</b>\n\n"
            "• <code>/schedule</code> — create (ID = source name)\n"
            "• <code>/listschedule</code> — list / cancel\n"
            "• <code>/cancelschedule id</code>\n\n"
            "Modes: <code>auto</code> · <code>download</code>\n"
            "Topic: <code>-100CHAT/TOPIC</code>",
            reply_markup=get_back_to_menu(),
        )

    elif data == "save":
        await query.message.edit_text(
            "<b>✦ Save restricted</b>\n\n"
            "• <code>/save</code> — one post link\n"
            "• <code>/batchsave</code> — range\n"
            "• <code>/cancelsave</code>\n\n"
            "Needs userbot for private/restricted.",
            reply_markup=get_back_to_menu(),
        )

    elif data == "conn":
        await query.message.edit_text(
            "<b>✦ Connections</b>\n\n"
            "• <code>/addconn name [live|schedule|batch|all]</code>\n"
            "• <code>/listconn</code>\n"
            "• <code>/useconn name</code>\n"
            "• <code>/delconn name</code>\n\n"
            "Save multiple bot/userbot sessions.",
            reply_markup=get_back_to_menu(),
        )

    elif data == "unequify":
        await query.message.edit_text(
            "<b>✦ Unequify</b>\n\n"
            "Remove duplicate messages.\n"
            "Send <code>/unequify</code> and follow steps.",
            reply_markup=get_back_to_menu(),
        )

    elif data == "cancel":
        active = temp.lock.get(user_id, [])
        if not active:
            await query.answer("No active batch jobs.", show_alert=True)
            return
        for fid in list(active):
            temp.CANCEL[fid] = True
        await query.answer(f"Cancelling {len(active)}…", show_alert=True)
        await query.message.edit_text(
            f"⏹ Cancelling {len(active)} batch job(s)…",
            reply_markup=get_back_to_menu(),
        )

    await query.answer()


# legacy callbacks
@Client.on_callback_query(filters.regex(r"^help$"))
async def helpcb(bot, query):
    await query.message.edit_text(
        text=Translation.HELP_TXT,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("How to use", callback_data="menu#how")],
            [InlineKeyboardButton("↩ Menu", callback_data="menu#main")],
        ]),
    )


@Client.on_callback_query(filters.regex(r"^about$"))
async def about(bot, query):
    await query.message.edit_text(
        text=Translation.ABOUT_TXT.format(
            python_version=python_version(),
            pyrogram_version=pyrogram_version,
            mongodb_version=await mongodb_version(),
        ),
        reply_markup=get_back_to_menu(),
        disable_web_page_preview=True,
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_callback_query(filters.regex(r"^status$"))
async def status(bot, query):
    text = await build_full_status(query.from_user.id)
    await query.message.edit_text(
        text=text,
        reply_markup=get_back_to_menu(),
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True,
    )


@Client.on_callback_query(filters.regex(r"^back$"))
async def back(bot, query):
    await query.message.edit_text(
        reply_markup=get_main_menu(),
        text=Translation.START_TXT.format(query.from_user.first_name),
    )
