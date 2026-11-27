"""
Per-forward filter editor (Telegram buttons).

Every batch forward (/fwd) and every live watcher (/live) has its OWN filters.
  • /fwd   → confirm message has a "⚙️ Filters" button (edit before you start)
  • /filters → lists your live watchers; tap one to edit its filters while it runs

Callback data:
  tfb#<forward_id>#<action>          batch forward (before start)
  tfl#<from_id>#<to_id>#<action>     live watcher
"""
import html
import logging
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as M
from config import temp
from database import db
from .utils import STATUS
from .engine import MEDIA_TYPES, normalize, summarize_filters, build_task_filters, _as_list

logger = logging.getLogger(__name__)

ICON = {True: "✅", False: "❌"}


def _keyboard(prefix: str, tf: dict, start_cb: str = None) -> M:
    rows, row = [], []
    for t in MEDIA_TYPES:
        row.append(B(f"{ICON[tf['types'].get(t, True)]} {t}", f"{prefix}#t:{t}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        B(f"🔁 dupes: {'skip' if tf['skip_duplicates'] else 'allow'}", f"{prefix}#d"),
        B(f"↪️ fwd tag: {'on' if tf['forward_tag'] else 'off'}", f"{prefix}#fw"),
    ])
    rows.append([B("🔑 Keywords", f"{prefix}#k"), B("🚫 Extensions", f"{prefix}#e")])
    rows.append([B("📏 Size", f"{prefix}#s"), B("📝 Caption", f"{prefix}#c")])
    rows.append([B("♻️ Reset to /settings", f"{prefix}#r")])
    last = []
    if start_cb:
        last.append(B("▶️ Start", start_cb))
    last.append(B("✖️ Close", "close_btn"))
    rows.append(last)
    return M(rows)


def _text(title: str, tf: dict) -> str:
    return (
        f"<b>⚙️ Filters — {html.escape(title)}</b>\n"
        f"<i>These apply to this forward only.</i>\n\n"
        f"<code>{html.escape(summarize_filters(tf))}</code>\n\n"
        f"Keywords: only files/messages containing one of them are sent.\n"
        f"Extensions: files with these extensions are skipped."
    )


async def _locate(query):
    """Return (prefix, tf, title, start_cb, save_coro_factory, action) or None."""
    uid = query.from_user.id
    parts = query.data.split("#")
    if parts[0] == "tfb" and len(parts) >= 3:
        fid, action = parts[1], parts[2]
        job = STATUS.get(fid)
        if not job or not fid.startswith(f"{uid}-"):
            return None
        if not job.get("TASK_FILTERS"):
            job["TASK_FILTERS"] = build_task_filters(await db.get_configs(uid))
        job["TASK_FILTERS"] = normalize(job["TASK_FILTERS"])
        tf = job["TASK_FILTERS"]

        async def save():
            return None
        return f"tfb#{fid}", tf, "batch forward", f"start_public_{fid}", save, action

    if parts[0] == "tfl" and len(parts) >= 4:
        try:
            from_id, to_id = int(parts[1]), int(parts[2])
        except ValueError:
            return None
        action = parts[3]
        meta = temp.LIVE_WATCHERS.get((from_id, to_id))
        if not meta or meta.get("user_id") != uid:
            return None
        if not meta.get("task_filters"):
            meta["task_filters"] = normalize(build_task_filters(await db.get_configs(uid)))
        tf = meta["task_filters"]  # same dict the running watcher reads

        async def save():
            await db.live.update_one(
                {"user_id": uid, "from_id": from_id, "to_id": to_id},
                {"$set": {"task_filters": tf}},
            )
        title = f"live {meta.get('from_title') or from_id} → {meta.get('to_title') or to_id}"
        return f"tfl#{from_id}#{to_id}", tf, title, None, save, action
    return None


async def _ask(bot, query, prompt: str):
    try:
        reply = await bot.ask(query.from_user.id, prompt + "\n\n/skip = clear · /cancel = keep as is", timeout=180)
    except Exception:
        return None
    text = (reply.text or "").strip()
    if text == "/cancel" or not text:
        return None
    if text == "/skip":
        return ""
    return text


@Client.on_callback_query(filters.regex(r"^tf[bl]#"))
async def task_filter_cb(bot, query):
    found = await _locate(query)
    if not found:
        return await query.answer("This forward is gone or not yours.", show_alert=True)
    prefix, tf, title, start_cb, save, action = found

    if action.startswith("t:"):
        t = action[2:]
        if t in tf["types"]:
            tf["types"][t] = not tf["types"][t]
    elif action == "d":
        tf["skip_duplicates"] = not tf["skip_duplicates"]
    elif action == "fw":
        tf["forward_tag"] = not tf["forward_tag"]
    elif action == "r":
        fresh = build_task_filters(await db.get_configs(query.from_user.id))
        tf.clear()
        tf.update(normalize(fresh))
    elif action in ("k", "e", "s", "c"):
        await query.answer()
        if action == "k":
            v = await _ask(bot, query, "Send keywords separated by space or comma (e.g. <code>1080p, hindi</code>).")
            if v is not None:
                tf["keywords"] = _as_list(v)
        elif action == "e":
            v = await _ask(bot, query, "Send extensions to BLOCK (e.g. <code>apk exe zip</code>).")
            if v is not None:
                tf["extensions"] = _as_list(v)
        elif action == "s":
            v = await _ask(bot, query, "Send size rule: <code>&gt;100</code> (only bigger than 100 MB) or <code>&lt;50</code> (only smaller).")
            if v is not None:
                v = v.replace(" ", "")
                if v == "":
                    tf["size_mode"], tf["size_mb"] = None, 0
                elif v[0] in "<>":
                    try:
                        tf["size_mb"] = max(0.0, min(4000.0, float(v[1:])))
                        tf["size_mode"] = "gt" if v[0] == ">" else "lt"
                    except ValueError:
                        await bot.send_message(query.from_user.id, "Not a number — size unchanged.")
        elif action == "c":
            v = await _ask(bot, query, "Send caption template. You can use <code>{filename}</code> <code>{size}</code> <code>{caption}</code>.")
            if v is not None:
                tf["caption"] = v or None
        await save()
        return await bot.send_message(
            query.from_user.id, _text(title, tf), reply_markup=_keyboard(prefix, tf, start_cb)
        )

    await save()
    try:
        await query.message.edit_text(_text(title, tf), reply_markup=_keyboard(prefix, tf, start_cb))
    except Exception:
        pass
    await query.answer()


@Client.on_message(filters.private & filters.command("filters"))
async def list_task_filters(bot, message):
    uid = message.from_user.id
    rows = []
    for (fid, tid), meta in list(temp.LIVE_WATCHERS.items()):
        if meta.get("user_id") != uid:
            continue
        label = f"🔴 {meta.get('from_title') or fid} → {meta.get('to_title') or tid}"[:60]
        rows.append([B(label, f"tfl#{fid}#{tid}#v")])
    if not rows:
        return await message.reply(
            "No live forwards running.\n\nFor a batch forward, use /fwd — the confirm "
            "message has a <b>⚙️ Filters</b> button so each forward gets its own filters."
        )
    rows.append([B("✖️ Close", "close_btn")])
    await message.reply("<b>Pick a live forward to edit its filters:</b>", reply_markup=M(rows))
