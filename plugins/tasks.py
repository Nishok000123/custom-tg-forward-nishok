"""
/tasks — one screen for everything you have running.

  📦 Batch forwards  (progress, open, cancel)
  🔴 Live forwards   (open, filters, stop)
  ⏰ Schedules       (open, cancel)

One message, edited in place. Every tap answers instantly; destructive actions
(stop / cancel) ask once for confirmation on the same message.

Callback data (all start with "tk#"):
  tk#h                     home (also refresh)
  tk#b#<fid>               batch detail      tk#bx#<fid> / tk#bx!#<fid>  cancel
  tk#l#<from>#<to>         live detail       tk#lx#… / tk#lx!#…          stop
  tk#s#<sid>               schedule detail   tk#sx#<sid> / tk#sx!#<sid>  cancel
"""
import html
import logging
import time

from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as M

from config import temp
from database import db
from .utils import STS
from .engine import normalize, summarize_filters

logger = logging.getLogger(__name__)


def _esc(v) -> str:
    return html.escape(str(v), quote=False)


def _clip(text, n=28) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1] + "…"


def _bar(pct: int, width: int = 10) -> str:
    pct = max(0, min(100, int(pct)))
    full = round(pct * width / 100)
    return "▰" * full + "▱" * (width - full)


def _ago(ts) -> str:
    try:
        secs = int(time.time() - float(ts))
    except Exception:
        return ""
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h {secs % 3600 // 60}m"
    return f"{secs // 86400}d {secs % 86400 // 3600}h"


# ── data ─────────────────────────────────────────────────────────────────────

def _batches(uid):
    out = []
    for fid in list(temp.lock.get(uid, [])):
        sts = STS(fid)
        if not sts.verify():
            continue
        i = sts.get(full=True)
        total = int(getattr(i, "total", 0) or 0)
        fetched = int(getattr(i, "fetched", 0) or 0)
        pct = int(fetched * 100 / total) if total else 0
        out.append({
            "fid": fid, "sts": sts, "i": i, "pct": pct,
            "fetched": fetched, "total": total,
            "cancelling": bool(temp.CANCEL.get(fid)),
        })
    return out


def _lives(uid):
    return [
        (k, v) for k, v in list(temp.LIVE_WATCHERS.items())
        if isinstance(k, tuple) and len(k) == 2 and v.get("user_id") == uid
    ]


async def _schedules(uid):
    try:
        from .connections import SCHEDULE_TASKS
    except Exception:
        SCHEDULE_TASKS = {}
    out = []
    try:
        cursor = await db.get_all_schedules(uid)
        async for s in cursor:
            s["_running"] = s.get("sched_id") in SCHEDULE_TASKS
            out.append(s)
    except Exception as e:
        logger.warning(f"/tasks schedules: {e}")
    return out


# ── screens ──────────────────────────────────────────────────────────────────

async def _home(uid, note=None):
    batches, lives, scheds = _batches(uid), _lives(uid), await _schedules(uid)
    n = len(batches) + len(lives) + len(scheds)
    lines = ["<b>📋 Your tasks</b>"]
    if note:
        lines += ["", note]
    rows = []

    if not n:
        lines += [
            "",
            "<i>Nothing running right now.</i>",
            "",
            "Start one:",
            "• <b>Batch</b> — copy old posts once",
            "• <b>Live</b> — mirror new posts as they arrive",
            "• <b>Schedule</b> — copy on a timer",
        ]
        rows.append([B("🚀 Batch", "menu#forward"), B("🔴 Live", "menu#live"), B("⏰ Schedule", "menu#schedule")])
        rows.append([B("↩ Menu", "menu#main")])
        return "\n".join(lines), M(rows)

    lines.append(f"<i>{n} active · tap one to manage it</i>")

    if batches:
        lines += ["", f"<b>📦 Batch · {len(batches)}</b>"]
        for b in batches:
            state = "⏳ cancelling" if b["cancelling"] else f"{b['pct']}%"
            lines.append(f"{_bar(b['pct'])} {state} · {b['fetched']}/{b['total'] or '?'}")
            rows.append([B(f"📦 {state} · {_clip(getattr(b['i'], 'TO', ''), 22)}", f"tk#b#{b['fid']}")])
    if lives:
        lines += ["", f"<b>🔴 Live · {len(lives)}</b>"]
        for (f, t), meta in lives:
            label = f"{_clip(meta.get('from_title') or f)} → {_clip(meta.get('to_title') or t)}"
            lines.append(f"🟢 {_esc(label)}")
            rows.append([B(f"🔴 {label}"[:60], f"tk#l#{f}#{t}")])
    if scheds:
        lines += ["", f"<b>⏰ Schedules · {len(scheds)}</b>"]
        for s in scheds:
            dot = "🟢" if s["_running"] else "⚪️"
            label = f"{_clip(s.get('from_title') or s.get('from_id'))} → {_clip(s.get('to_title') or s.get('to_id'))}"
            lines.append(f"{dot} {_esc(label)} · every {_esc(s.get('interval_label') or '?')}")
            rows.append([B(f"⏰ {label}"[:60], f"tk#s#{s['sched_id']}")])

    rows.append([B("🔄 Refresh", "tk#h"), B("↩ Menu", "menu#main")])
    return "\n".join(lines), M(rows)


def _batch_view(b, confirm=False):
    i = b["i"]
    text = (
        f"<b>📦 Batch forward</b>\n"
        f"<code>{_esc(b['fid'])}</code>\n\n"
        f"{_bar(b['pct'], 14)} <b>{b['pct']}%</b>\n\n"
        f"📥 Fetched   <code>{b['fetched']}</code> / <code>{b['total'] or '?'}</code>\n"
        f"📤 Sent      <code>{getattr(i, 'total_files', 0) or 0}</code>\n"
        f"🔁 Duplicates <code>{getattr(i, 'duplicate', 0) or 0}</code>"
        f" · 🧹 Filtered <code>{getattr(i, 'filtered', 0) or 0}</code>"
        f" · 🗑 Failed/deleted <code>{getattr(i, 'deleted', 0) or 0}</code>\n"
        f"🎯 Target <code>{_esc(getattr(i, 'TO', ''))}</code>\n"
    )
    tf = b["sts"].get("TASK_FILTERS")
    if tf:
        text += f"\n⚙️ <code>{_esc(summarize_filters(normalize(tf)))}</code>\n"
    if b["cancelling"]:
        text += "\n⏳ <i>Cancelling — finishing the current message…</i>"
        rows = [[B("🔄 Refresh", f"tk#b#{b['fid']}"), B("‹ All tasks", "tk#h")]]
    elif confirm:
        text += "\n<b>Cancel this forward?</b> Messages already sent stay."
        rows = [[B("⏹ Yes, cancel", f"tk#bx!#{b['fid']}"), B("‹ Keep running", f"tk#b#{b['fid']}")]]
    else:
        rows = [
            [B("🔄 Refresh", f"tk#b#{b['fid']}"), B("⏹ Cancel", f"tk#bx#{b['fid']}")],
            [B("‹ All tasks", "tk#h")],
        ]
    return text, M(rows)


def _live_view(key, meta, confirm=False):
    f, t = key
    tf = normalize(meta.get("task_filters"))
    started = meta.get("started_at")
    text = (
        f"<b>🔴 Live forward</b>\n\n"
        f"📡 From <b>{_esc(meta.get('from_title') or f)}</b> <code>{f}</code>\n"
        f"🎯 To <b>{_esc(meta.get('to_title') or t)}</b> <code>{t}</code>\n"
        f"🔌 Session <code>{_esc(meta.get('conn_name') or 'default')}</code>"
        f" · Mode <code>{'download' if meta.get('force_download') else 'auto'}</code>\n"
        + (f"⏱ Running {_ago(started)}\n" if started else "")
        + (f"📤 Sent <code>{meta.get('sent', 0)}</code> · 🧹 Filtered <code>{meta.get('filtered', 0)}</code>\n"
           if "sent" in meta else "")
        + f"\n⚙️ <code>{_esc(summarize_filters(tf))}</code>"
    )
    if confirm:
        text += "\n\n<b>Stop this live forward?</b> You can start it again with /live."
        rows = [[B("⏹ Yes, stop", f"tk#lx!#{f}#{t}"), B("‹ Keep running", f"tk#l#{f}#{t}")]]
    else:
        rows = [
            [B("⚙️ Filters", f"tfl#{f}#{t}#v"), B("⏹ Stop", f"tk#lx#{f}#{t}")],
            [B("🔄 Refresh", f"tk#l#{f}#{t}"), B("‹ All tasks", "tk#h")],
        ]
    return text, M(rows)


def _sched_view(s, confirm=False):
    sid = s["sched_id"]
    text = (
        f"<b>⏰ Schedule</b> <code>{_esc(sid)}</code>\n\n"
        f"📡 From <b>{_esc(s.get('from_title') or s.get('from_id'))}</b>\n"
        f"🎯 To <b>{_esc(s.get('to_title') or s.get('to_id'))}</b>\n"
        f"🔁 Every <b>{_esc(s.get('interval_label') or '?')}</b>"
        f" · Mode <code>{'download' if s.get('force_download') else 'auto'}</code>\n"
        f"{'🟢 Running' if s.get('_running') else '⚪️ Waiting for restart'}"
    )
    if confirm:
        text += "\n\n<b>Cancel this schedule?</b>"
        rows = [[B("🗑 Yes, cancel", f"tk#sx!#{sid}"), B("‹ Keep it", f"tk#s#{sid}")]]
    else:
        rows = [[B("🗑 Cancel schedule", f"tk#sx#{sid}"), B("‹ All tasks", "tk#h")]]
    return text, M(rows)


async def _edit(message, text, kb):
    try:
        await message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.debug(f"/tasks edit failed: {e}")


# ── handlers ─────────────────────────────────────────────────────────────────

@Client.on_message(filters.private & filters.command(["tasks", "filters"]))
async def tasks_cmd(bot, message):
    text, kb = await _home(message.from_user.id)
    await message.reply(text, reply_markup=kb, disable_web_page_preview=True)


@Client.on_callback_query(filters.regex(r"^tk#"))
async def tasks_cb(bot, query):
    uid = query.from_user.id
    parts = query.data.split("#")
    kind = parts[1] if len(parts) > 1 else "h"
    toast = None

    if kind == "h":
        text, kb = await _home(uid)
        await query.answer("Updated" if query.message.text and "Your tasks" in query.message.text else "")
        return await _edit(query.message, text, kb)

    # ── batch
    if kind in ("b", "bx", "bx!"):
        fid = parts[2] if len(parts) > 2 else ""
        b = next((x for x in _batches(uid) if x["fid"] == fid), None)
        if not b:
            text, kb = await _home(uid, "✅ <i>That batch has finished.</i>")
            await query.answer("Finished")
            return await _edit(query.message, text, kb)
        if kind == "bx!":
            temp.CANCEL[fid] = True
            b["cancelling"] = True
            toast = "Cancelling…"
        await query.answer(toast or "")
        text, kb = _batch_view(b, confirm=(kind == "bx"))
        return await _edit(query.message, text, kb)

    # ── live
    if kind in ("l", "lx", "lx!"):
        try:
            key = (int(parts[2]), int(parts[3]))
        except (IndexError, ValueError):
            return await query.answer("Bad button.", show_alert=True)
        meta = temp.LIVE_WATCHERS.get(key)
        if not meta or meta.get("user_id") != uid:
            text, kb = await _home(uid, "ℹ️ <i>That live forward is no longer running.</i>")
            await query.answer("Not running")
            return await _edit(query.message, text, kb)
        if kind == "lx!":
            temp.LIVE_WATCHERS.pop(key, None)
            try:
                await db.remove_live_watcher(uid, key[0], key[1])
            except Exception as e:
                logger.warning(f"/tasks stop live: {e}")
            text, kb = await _home(uid, "⏹ <b>Live forward stopped.</b>")
            await query.answer("Stopped")
            return await _edit(query.message, text, kb)
        await query.answer()
        text, kb = _live_view(key, meta, confirm=(kind == "lx"))
        return await _edit(query.message, text, kb)

    # ── schedule
    if kind in ("s", "sx", "sx!"):
        sid = "#".join(parts[2:])
        s = await db.sched.find_one({"sched_id": sid, "user_id": uid})
        if not s:
            text, kb = await _home(uid, "ℹ️ <i>That schedule no longer exists.</i>")
            await query.answer("Gone")
            return await _edit(query.message, text, kb)
        if kind == "sx!":
            from .connections import _cancel_schedule
            await _cancel_schedule(sid)
            text, kb = await _home(uid, "🗑 <b>Schedule cancelled.</b>")
            await query.answer("Cancelled")
            return await _edit(query.message, text, kb)
        try:
            from .connections import SCHEDULE_TASKS
            s["_running"] = sid in SCHEDULE_TASKS
        except Exception:
            s["_running"] = False
        await query.answer()
        text, kb = _sched_view(s, confirm=(kind == "sx"))
        return await _edit(query.message, text, kb)

    await query.answer()
