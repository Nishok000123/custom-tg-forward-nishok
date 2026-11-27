import os
from config import Config

class Translation(object):
    START_TXT = """<b>✦ Forward Bot</b>

Hi <b>{}</b> — pick an action below.

Batch · Live · Schedule · Save · Settings
All in one place. Clean & fast."""

    HELP_TXT = """<b>✦ Help</b>

<b>Core</b>
• /menu — main panel
• /tasks — manage running tasks + per-task filters
• /status — all active jobs
• /cancel — stop batch jobs

<b>Forward</b>
• /forward · /fwd — multi source → multi target
• /live · /lives · /stoplive
• /schedule · /listschedule · /cancelschedule
• /unequify — remove duplicates

<b>Save restricted</b>
• /save · /batchsave · /cancelsave

<b>Connections</b>
• /addconn · /listconn · /useconn · /delconn

<b>Settings</b>
• /settings — session, targets, filters, caption

Target any chat during forward (ID / @user / link / forward).
Topic: <code>-100CHAT/TOPIC_ID</code>"""

    HOW_USE_TXT = """<b>✦ Quick start</b>

1. /settings → add <b>Bot</b> or <b>Userbot</b>
2. /forward
3. Send one or more <b>sources</b> (done when finished)
4. Send one or more <b>targets</b> (done when finished)
5. Skip count + mode → confirm

Userbot must join private sources.
Bot/userbot must be admin on targets."""

    ABOUT_TXT = """<b>╭── Forward Bot ──╮
│ Batch · Live · Schedule
│ Restricted save · Filters
│ Multi source · Multi target
│ Python {python_version}
│ Pyrogram {pyrogram_version}
│ Mongo {mongodb_version}
╰──────────────────╯</b>"""

    STATUS_TXT = """<b>╭── Status ──╮
├ Users: {}
├ Sessions: {}
├ Batch jobs: {}
├ Live: {}
├ Schedules: {}
├ Channels: {}
╰────────────╯</b>"""

    FROM_MSG = (
        "<b>✦ Sources</b>\n\n"
        "Send source chat(s) — one per message:\n"
        "• link · <code>@channel</code> · <code>-100…</code>\n"
        "• forward a post from the chat\n"
        "• <code>me</code> / <code>saved</code>\n\n"
        "Send <code>done</code> when all sources added.\n"
        "/cancel — abort"
    )
    TO_MSG = (
        "<b>✦ Targets</b>\n\n"
        "Send target chat(s) — any channel you can post to:\n"
        "• link · <code>@channel</code> · <code>-100…</code>\n"
        "• topic: <code>-100CHAT/TOPIC_ID</code>\n"
        "• forward a post from the chat\n"
        "• optional: pick a saved channel button\n\n"
        "Send <code>done</code> when all targets added.\n"
        "/cancel — abort"
    )
    SAVED_MSG_MODE = (
        "<b>✦ Saved Messages mode</b>\n\n"
        "1. <code>batch</code> — existing messages\n"
        "2. <code>live</code> — keep waiting for new\n\n"
        "Send <code>batch</code> or <code>live</code>"
    )
    SAVED_MSG_LIMIT = (
        "<b>✦ How many messages?</b>\n\n"
        "Number (e.g. <code>100</code>) or <code>all</code>"
    )
    SKIP_MSG = (
        "<b>✦ Skip messages</b>\n\n"
        "How many to skip from the start (default <code>0</code>)\n"
        "/cancel — abort"
    )
    CANCEL = "<b>Cancelled.</b>"
    BOT_DETAILS = (
        "<b>Bot</b>\n"
        "Name: <code>{}</code>\n"
        "ID: <code>{}</code>\n"
        "User: @{}"
    )
    USER_DETAILS = (
        "<b>Userbot</b>\n"
        "Name: <code>{}</code>\n"
        "ID: <code>{}</code>\n"
        "User: @{}"
    )

    TEXT = """<b>╭─ Forward ─╮
│ Fetched   <code>{}</code>
│ Sent      <code>{}</code>
│ Duplicate <code>{}</code>
│ Deleted   <code>{}</code>
│ Skipped   <code>{}</code>
│ Status    <code>{}</code>
│ Progress  <code>{}</code>%
│ ETA       <code>{}</code>
│ {}
╰───────────╯</b>"""

    TEXT1 = TEXT

    DUPLICATE_TEXT = """
╔═ Unequify ═╗
║ Fetched: <code>{}</code>
║ Deleted: <code>{}</code>
║ {}
╚══════════╝
"""
    DOUBLE_CHECK = """<b>✦ Confirm forward</b>

<b>Session:</b> [{botname}](t.me/{botuname})
<b>Sources:</b>
{from_chat}
<b>Targets:</b>
{to_chat}
<b>Skip:</b> `{skip}`

Session must be admin on every target.
Private sources need a userbot that is a member.

Tap <b>Yes</b> to start."""
