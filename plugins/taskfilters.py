"""
Per-forward filter editor (Telegram buttons).

Every batch forward (/fwd) and every live watcher (/live) has its OWN filters.
  • /fwd    → the confirm message has a "⚙️ Filters" button (edit before you start)
  • /tasks  → open a live forward → "⚙️ Filters" (edits apply while it runs)

UI rules (kept small and predictable):
  • one panel message, always edited in place, never a new message per change
  • every tap gets an instant toast; every toggle shows its state (✅ / ▫️)
  • text input happens inside the same panel; your reply is deleted afterwards
  • simple first: home shows a summary, details open on their own page

Callback data:
  tfb#<forward_id>#<action>          batch forward (before start)
  tfl#<from_id>#<to_id>#<action>     live watcher
Actions: v home · p:<t|m|x|s> pages · t:<type> · ta · d fw sl sm pr toggles ·
         sz:<off|lt100|gt500…> · k b e z c rp hd ft text inputs · x:<page> cancel input ·
         r / r! reset
"""
import html
import logging

from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as M

try:  # pyrofork listener errors
    from pyrogram.errors import ListenerTimeout, ListenerStopped
except Exception:  # pragma: no cover
    class ListenerTimeout(Exception):
        pass

    class ListenerStopped(Exception):
        pass

from config import temp
from database import db
from .utils import STATUS
from .engine import (
    MEDIA_TYPES, MAX_RULES, normalize, build_task_filters, _as_list, _as_phrases,
    parse_rules, preview_text,
)

logger = logging.getLogger(__name__)

ON, OFF = "✅", "▫️"
TYPE_ICON = {
    "text": "💬", "photo": "🖼", "video": "🎬", "document": "📄", "audio": "🎵",
    "voice": "🎙", "animation": "🎞", "sticker": "🌟", "poll": "📊",
}
SIZE_PRESETS = [
    ("off", "Any size"), ("lt10", "< 10 MB"), ("lt100", "< 100 MB"), ("lt500", "< 500 MB"),
    ("gt100", "> 100 MB"), ("gt500", "> 500 MB"), ("gt1024", "> 1 GB"), ("gt2048", "> 2 GB"),
]
TOGGLES = {
    "d": ("skip_duplicates", "Skip duplicates", "v"),
    "fw": ("forward_tag", "Forward tag", "v"),
    "sl": ("strip_links", "Strip links", "x"),
    "sm": ("strip_mentions", "Strip @mentions", "x"),
    "pr": ("protect", "Protect content", "x"),
}
# key: (return page, label, help)
INPUTS = {
    "k": ("m", "Include words",
          "Only messages containing <b>one</b> of these words are sent.\n"
          "Separate with spaces or commas. Example: <code>1080p, hindi</code>"),
    "b": ("m", "Block words",
          "Messages containing <b>any</b> of these words or phrases are skipped.\n"
          "Separate with commas. Example: <code>promo, giveaway, join now</code>"),
    "e": ("m", "Block extensions",
          "Files with these extensions are skipped.\nExample: <code>apk exe zip</code>"),
    "z": ("m", "Custom size",
          "Send <code>&lt;50</code> to send only files under 50 MB,\n"
          "or <code>&gt;200</code> to send only files over 200 MB."),
    "c": ("x", "Caption template",
          "Placeholders: <code>{filename}</code> <code>{size}</code> <code>{caption}</code>\n"
          "Telegram formatting (bold, links…) is kept.\n"
          "Example: <code>{filename}</code> ⏎ <code>📦 {size}</code>"),
    "rp": ("x", "Find & replace",
           f"One rule per line, up to {MAX_RULES}: <code>old =&gt; new</code>\n"
           "Leave the right side empty to delete: <code>Join us =&gt;</code>\n"
           "Matching ignores upper/lower case."),
    "hd": ("x", "Header",
           "Added <b>above</b> every caption and text message.\n"
           "Formatting is kept. <code>{filename}</code> and <code>{size}</code> work here too."),
    "ft": ("x", "Footer",
           "Added <b>below</b> every caption and text message.\nExample: <code>📢 @mychannel</code>"),
}
RICH_INPUTS = ("c", "hd", "ft")
LIMITS = {"c": 1024, "hd": 512, "ft": 512}


def _esc(v) -> str:
    return html.escape(str(v), quote=False)


def _flag(v) -> str:
    return ON if v else OFF


def _types_on(tf) -> int:
    return sum(1 for t in MEDIA_TYPES if tf["types"].get(t, True))


def _size_label(tf) -> str:
    if not tf["size_mode"] or not tf["size_mb"]:
        return "any size"
    mb = float(tf["size_mb"])
    val = f"{mb / 1024:g} GB" if mb >= 1024 and mb % 1024 == 0 else f"{mb:g} MB"
    return ("> " if tf["size_mode"] == "gt" else "< ") + val


def _size_code(tf) -> str:
    if not tf["size_mode"] or not tf["size_mb"]:
        return "off"
    return f"{tf['size_mode']}{float(tf['size_mb']):g}"


def _list(items, empty="—", n=8) -> str:
    if not items:
        return f"<i>{empty}</i>"
    shown = ", ".join(_esc(i) for i in items[:n])
    if len(items) > n:
        shown += f" <i>+{len(items) - n} more</i>"
    return shown


def _clip(text, n=120) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1] + "…"


def _match_count(tf) -> int:
    return sum(bool(x) for x in (tf["keywords"], tf["blocked_words"], tf["extensions"], tf["size_mode"]))


def _text_count(tf) -> int:
    return sum(bool(x) for x in (
        tf["caption"], tf["replace"], tf["strip_links"], tf["strip_mentions"],
        tf["header"], tf["footer"], tf["protect"],
    ))


def _badge(n) -> str:
    return f" · {n}" if n else ""


class Ctx:
    """Where a filter dict lives and how to save it."""

    def __init__(self, kind, prefix, tf, title, save, action, start_cb=None, back_cb=None):
        self.kind, self.prefix, self.tf, self.title = kind, prefix, tf, title
        self.save, self.action, self.start_cb, self.back_cb = save, action, start_cb, back_cb

    def cb(self, action) -> str:
        return f"{self.prefix}#{action}"


async def _locate(query):
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

        async def save():
            return None
        return Ctx("batch", f"tfb#{fid}", job["TASK_FILTERS"], "Batch forward", save, action,
                   start_cb=f"start_public_{fid}")

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
            meta["task_filters"] = build_task_filters(await db.get_configs(uid))
        # normalize in place: the running watcher holds a reference to this dict
        fresh = normalize(meta["task_filters"])
        meta["task_filters"].clear()
        meta["task_filters"].update(fresh)
        tf = meta["task_filters"]

        async def save():
            await db.live.update_one(
                {"user_id": uid, "from_id": from_id, "to_id": to_id},
                {"$set": {"task_filters": tf}},
            )
        title = f"{meta.get('from_title') or from_id} → {meta.get('to_title') or to_id}"
        return Ctx("live", f"tfl#{from_id}#{to_id}", tf, _clip(title, 60), save, action,
                   back_cb=f"tk#l#{from_id}#{to_id}")
    return None


# ── pages ────────────────────────────────────────────────────────────────────

def _head(ctx, page=None, note=None) -> str:
    crumb = f"  ›  {page}" if page else ""
    sub = ("Only this forward · changes apply instantly" if ctx.kind == "live"
           else "Only this forward · set it up, then start")
    out = f"<b>⚙️ {_esc(ctx.title)}{crumb}</b>\n<i>{sub}</i>\n"
    if note:
        out += f"\n{note}\n"
    return out


def _page_home(ctx, note):
    tf = ctx.tf
    off = [t for t in MEDIA_TYPES if not tf["types"].get(t, True)]
    text_bits = []
    if tf["caption"]:
        text_bits.append("caption template")
    if tf["replace"]:
        text_bits.append(f"{len(tf['replace'])} replace rule(s)")
    if tf["strip_links"]:
        text_bits.append("no links")
    if tf["strip_mentions"]:
        text_bits.append("no @mentions")
    if tf["header"]:
        text_bits.append("header")
    if tf["footer"]:
        text_bits.append("footer")
    if tf["protect"]:
        text_bits.append("protected")
    body = (
        f"🎞 <b>Types</b> — {_types_on(tf)}/{len(MEDIA_TYPES)} on"
        + (f" <i>(off: {_esc(', '.join(off))})</i>" if off else "") + "\n"
        f"🔎 <b>Include</b> — {_list(tf['keywords'], 'everything')}\n"
        f"🚫 <b>Block</b> — {_list(tf['blocked_words'], 'nothing')}"
        + (f" · ext {_list(tf['extensions'])}" if tf["extensions"] else "") + "\n"
        f"📏 <b>Size</b> — {_size_label(tf)}\n"
        f"✏️ <b>Text</b> — {_esc(', '.join(text_bits)) if text_bits else '<i>original</i>'}\n"
        f"🔁 <b>Duplicates</b> — {'skipped' if tf['skip_duplicates'] else 'allowed'}"
        f" · ↪️ <b>Forward tag</b> — {'on' if tf['forward_tag'] else 'off'}"
    )
    rows = [
        [B(f"🎞 Types · {_types_on(tf)}/{len(MEDIA_TYPES)}", ctx.cb("p:t")),
         B(f"🔎 Match rules{_badge(_match_count(tf))}", ctx.cb("p:m"))],
        [B(f"✏️ Text & caption{_badge(_text_count(tf))}", ctx.cb("p:x"))],
        [B(f"{_flag(tf['skip_duplicates'])} Skip duplicates", ctx.cb("d")),
         B(f"{_flag(tf['forward_tag'])} Forward tag", ctx.cb("fw"))],
        [B("♻️ Reset", ctx.cb("r")), B("✖️ Close", "close_btn")],
    ]
    if ctx.start_cb:
        rows.append([B("▶️ Start forward", ctx.start_cb)])
    if ctx.back_cb:
        rows.append([B("‹ Back to task", ctx.back_cb), B("📋 All tasks", "tk#h")])
    return _head(ctx, None, note) + "\n" + body, M(rows)


def _page_types(ctx, note):
    tf = ctx.tf
    rows, row = [], []
    for t in MEDIA_TYPES:
        on = tf["types"].get(t, True)
        row.append(B(f"{_flag(on)} {TYPE_ICON.get(t, '')} {t.title()}", ctx.cb(f"t:{t}")))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([B("✅ All on", ctx.cb("ta")), B("‹ Back", ctx.cb("v"))])
    body = "Tap a type to turn it on or off.\n✅ = forwarded · ▫️ = skipped"
    return _head(ctx, "Types", note) + "\n" + body, M(rows)


def _page_match(ctx, note):
    tf = ctx.tf
    body = (
        f"🔎 <b>Include words</b>\n{_list(tf['keywords'], 'off — everything passes')}\n\n"
        f"🚫 <b>Block words</b>\n{_list(tf['blocked_words'], 'off')}\n\n"
        f"🧩 <b>Block extensions</b>\n{_list(tf['extensions'], 'off')}\n\n"
        f"📏 <b>Size</b>\n{_size_label(tf)}"
    )
    rows = [
        [B(f"🔎 Include{_badge(len(tf['keywords']))}", ctx.cb("k")),
         B(f"🚫 Block words{_badge(len(tf['blocked_words']))}", ctx.cb("b"))],
        [B(f"🧩 Extensions{_badge(len(tf['extensions']))}", ctx.cb("e")),
         B(f"📏 {_size_label(tf).capitalize()}", ctx.cb("p:s"))],
        [B("‹ Back", ctx.cb("v"))],
    ]
    return _head(ctx, "Match rules", note) + "\n" + body, M(rows)


def _page_size(ctx, note):
    cur = _size_code(ctx.tf)
    rows, row = [], []
    for code, label in SIZE_PRESETS:
        row.append(B(f"{'● ' if code == cur else ''}{label}", ctx.cb(f"sz:{code}")))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([B("✍️ Custom…", ctx.cb("z")), B("‹ Back", ctx.cb("p:m"))])
    body = f"Now: <b>{_size_label(ctx.tf)}</b>\nOnly applies to files; text messages always pass."
    return _head(ctx, "Size", note) + "\n" + body, M(rows)


def _page_text(ctx, note, safe=False):
    tf = ctx.tf
    rules = "\n".join(
        f"  <code>{_esc(_clip(o, 30))}</code> → <code>{_esc(_clip(n, 30)) or '(delete)'}</code>"
        for o, n in tf["replace"][:6]
    ) or "  <i>none</i>"
    if len(tf["replace"]) > 6:
        rules += f"\n  <i>+{len(tf['replace']) - 6} more</i>"
    prev = preview_text(tf)
    if safe:
        prev = _esc(prev)
    warn = ("\n⚠️ <i>Forward tag is on, so text changes are not applied. "
            "Turn it off on the main page.</i>\n" if tf["forward_tag"] else "")
    body = (
        f"📝 <b>Caption</b> — {'<code>' + _esc(_clip(tf['caption'], 60)) + '</code>' if tf['caption'] else '<i>original</i>'}\n"
        f"🔁 <b>Replace</b>\n{rules}\n"
        f"⬆️ <b>Header</b> — {'set' if tf['header'] else '<i>none</i>'}"
        f" · ⬇️ <b>Footer</b> — {'set' if tf['footer'] else '<i>none</i>'}\n"
        f"{warn}\n"
        f"👁 <b>Preview</b> <i>(sample post)</i>\n<blockquote>{prev}</blockquote>"
    )
    rows = [
        [B(f"📝 Caption{' ✓' if tf['caption'] else ''}", ctx.cb("c")),
         B(f"🔁 Replace{_badge(len(tf['replace']))}", ctx.cb("rp"))],
        [B(f"⬆️ Header{' ✓' if tf['header'] else ''}", ctx.cb("hd")),
         B(f"⬇️ Footer{' ✓' if tf['footer'] else ''}", ctx.cb("ft"))],
        [B(f"{_flag(tf['strip_links'])} Strip links", ctx.cb("sl")),
         B(f"{_flag(tf['strip_mentions'])} Strip @mentions", ctx.cb("sm"))],
        [B(f"{_flag(tf['protect'])} Protect content", ctx.cb("pr")), B("‹ Back", ctx.cb("v"))],
    ]
    return _head(ctx, "Text & caption", note) + "\n" + body, M(rows)


def _page_reset(ctx, note):
    body = ("Reset this forward's filters to your /settings defaults?\n"
            "<i>Other forwards are not affected.</i>")
    rows = [[B("♻️ Yes, reset", ctx.cb("r!")), B("‹ No, go back", ctx.cb("v"))]]
    return _head(ctx, "Reset", note) + "\n" + body, M(rows)


PAGES = {"v": _page_home, "t": _page_types, "m": _page_match, "s": _page_size,
         "x": _page_text, "r": _page_reset}


async def _edit(message, text, kb) -> bool:
    try:
        await message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        return True
    except MessageNotModified:
        return True
    except Exception as e:
        logger.debug(f"filter panel edit failed: {e}")
        return False


async def _render(message, ctx, page="v", note=None):
    fn = PAGES.get(page, _page_home)
    text, kb = fn(ctx, note)
    if await _edit(message, text, kb):
        return
    if page == "x":  # a header/footer with odd HTML can break the preview
        text, kb = _page_text(ctx, note, safe=True)
        if await _edit(message, text, kb):
            return
    await _edit(message, _esc(text), kb)


# ── text input inside the panel ──────────────────────────────────────────────

def _current_value(tf, key) -> str:
    if key == "k":
        return _list(tf["keywords"], "off")
    if key == "b":
        return _list(tf["blocked_words"], "off")
    if key == "e":
        return _list(tf["extensions"], "off")
    if key == "z":
        return _size_label(tf)
    if key == "rp":
        return f"{len(tf['replace'])} rule(s)" if tf["replace"] else "<i>none</i>"
    field = {"c": "caption", "hd": "header", "ft": "footer"}[key]
    return f"<code>{_esc(_clip(tf[field], 200))}</code>" if tf[field] else "<i>none</i>"


def _apply_input(tf, key, value: str) -> str:
    """Store `value` ("" = clear). Returns the note shown on the panel."""
    if value == "":
        if key == "k":
            tf["keywords"] = []
        elif key == "b":
            tf["blocked_words"] = []
        elif key == "e":
            tf["extensions"] = []
        elif key == "z":
            tf["size_mode"], tf["size_mb"] = None, 0
        elif key == "rp":
            tf["replace"] = []
        else:
            tf[{"c": "caption", "hd": "header", "ft": "footer"}[key]] = None
        return "🧹 <b>Cleared.</b>"
    if key == "k":
        tf["keywords"] = _as_list(value)
        return f"✅ <b>Include words saved</b> ({len(tf['keywords'])})."
    if key == "b":
        tf["blocked_words"] = _as_phrases(value)
        return f"✅ <b>Block words saved</b> ({len(tf['blocked_words'])})."
    if key == "e":
        tf["extensions"] = _as_list(value)
        return f"✅ <b>Blocked extensions saved</b> ({len(tf['extensions'])})."
    if key == "z":
        v = value.replace(" ", "").lower().replace("mb", "")
        if v[:1] in "<>" and len(v) > 1:
            try:
                tf["size_mb"] = max(0.0, min(4000.0, float(v[1:])))
                tf["size_mode"] = "gt" if v[0] == ">" else "lt"
                return f"✅ <b>Size:</b> {_size_label(tf)}."
            except ValueError:
                pass
        return "⚠️ <b>Not understood.</b> Use <code>&lt;50</code> or <code>&gt;200</code>. Size unchanged."
    if key == "rp":
        rules = parse_rules(value)
        if not rules:
            return "⚠️ <b>No rules found.</b> Write them as <code>old =&gt; new</code>, one per line."
        tf["replace"] = rules
        return f"✅ <b>{len(rules)} replace rule(s) saved.</b>"
    field = {"c": "caption", "hd": "header", "ft": "footer"}[key]
    limit = LIMITS[key]
    if len(value) > limit:
        return f"⚠️ <b>Too long</b> ({len(value)}/{limit} characters). Nothing changed."
    tf[field] = value
    return f"✅ <b>{field.title()} saved.</b> Check the preview below."


async def _collect(bot, query, ctx, key):
    uid = query.from_user.id
    back, label, help_ = INPUTS[key]
    await query.answer("✍️ Send your reply in this chat")
    try:  # only one pending input at a time
        await bot.stop_listening(chat_id=uid, user_id=uid)
    except Exception:
        pass
    text = (
        f"<b>✍️ {label}</b>\n\n{help_}\n\n"
        f"<b>Now:</b> {_current_value(ctx.tf, key)}\n\n"
        f"<i>Send it as a message · /skip clears it · Cancel keeps it</i>"
    )
    await _edit(query.message, text, M([[B("✖️ Cancel", ctx.cb(f"x:{back}"))]]))
    try:
        reply = await bot.listen(chat_id=uid, user_id=uid, filters=filters.text, timeout=180)
    except ListenerStopped:
        return
    except ListenerTimeout:
        return await _render(query.message, ctx, back, "⌛ <i>Timed out — nothing changed.</i>")
    except Exception as e:
        logger.warning(f"filter input failed: {e}")
        return await _render(query.message, ctx, back, "⚠️ <i>Could not read your reply.</i>")

    raw = reply.text or ""
    if key in RICH_INPUTS:
        raw = getattr(reply.text, "html", None) or raw
    try:
        await reply.delete()
    except Exception:
        pass
    stripped = raw.strip()
    if stripped == "/cancel":
        note = "↩️ <i>Kept as is.</i>"
    else:
        note = _apply_input(ctx.tf, key, "" if stripped == "/skip" else stripped)
        await ctx.save()
    await _render(query.message, ctx, back, note)


# ── callback router ──────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^tf[bl]#"))
async def task_filter_cb(bot, query):
    ctx = await _locate(query)
    if not ctx:
        return await query.answer("This forward has finished or isn't yours.", show_alert=True)
    a, tf, uid = ctx.action, ctx.tf, query.from_user.id
    page, toast, changed = "v", None, False

    if a == "v":
        pass
    elif a.startswith("p:"):
        page = a[2:]
    elif a.startswith("t:"):
        t, page = a[2:], "t"
        if t in tf["types"]:
            if tf["types"][t] and _types_on(tf) == 1:
                return await query.answer("Keep at least one type on.", show_alert=True)
            tf["types"][t] = not tf["types"][t]
            toast, changed = f"{t.title()}: {'on' if tf['types'][t] else 'off'}", True
    elif a == "ta":
        page = "t"
        for t in MEDIA_TYPES:
            tf["types"][t] = True
        toast, changed = "All types on", True
    elif a in TOGGLES:
        field, label, page = TOGGLES[a]
        tf[field] = not tf[field]
        toast, changed = f"{label}: {'on' if tf[field] else 'off'}", True
    elif a.startswith("sz:"):
        code, page = a[3:], "m"
        if code == "off":
            tf["size_mode"], tf["size_mb"] = None, 0
        else:
            try:
                tf["size_mode"], tf["size_mb"] = code[:2], float(code[2:])
            except ValueError:
                return await query.answer("Unknown size.", show_alert=True)
        toast, changed = f"Size: {_size_label(tf)}", True
    elif a == "r":
        page = "r"
    elif a == "r!":
        fresh = normalize(build_task_filters(await db.get_configs(uid)))
        tf.clear()
        tf.update(fresh)
        toast, changed = "Reset to your /settings defaults", True
    elif a.startswith("x:"):
        page = a[2:] or "v"
        try:
            await bot.stop_listening(chat_id=uid, user_id=uid)
        except Exception:
            pass
        toast = "Kept as is"
    elif a in INPUTS:
        return await _collect(bot, query, ctx, a)

    # feedback first (instant), then persist + redraw
    try:
        await query.answer(toast or "")
    except Exception:
        pass
    if changed:
        await ctx.save()
    await _render(query.message, ctx, page)


def filters_panel(ctx_kind: str, key, note=None):
    """Helper for other modules: (text, markup) of the home page is built on demand
    via the callback, so they only need the entry callback data."""
    if ctx_kind == "live":
        return f"tfl#{key[0]}#{key[1]}#v"
    return f"tfb#{key}#v"
