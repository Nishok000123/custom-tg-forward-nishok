import os
import sys 
import math
import time
import asyncio 
import html
import logging
from .utils import STS, resolve_chat, format_chat_label, get_client_for_user
from database import db
from .test import CLIENT , start_clone_bot, parse_buttons
from .engine import build_task_filters, normalize, should_forward, render_caption, build_output, summarize_filters
from config import Config, temp
from translation import Translation
from pyrogram import Client, filters 
from pyrogram.errors import FloodWait, MessageNotModified, RPCError
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, Message 

CLIENT = CLIENT()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
TEXT = Translation.TEXT

# Guided /live state
PENDING_LIVE = {}
PENDING_LIVE_META = {}


@Client.on_callback_query(filters.regex(r'^start_public'))
async def pub_(bot, message):
    user = message.from_user.id
    frwd_id = message.data.split("_")[2]
    temp.CANCEL[frwd_id] = False
    active_tasks = temp.lock.get(user, [])
    if len(active_tasks) >= 3:
      return await message.answer("Max 3 concurrent forwards running. Cancel one first.", show_alert=True)
    sts = STS(frwd_id)
    if not sts.verify():
      await message.answer("your are clicking on my old button", show_alert=True)
      return await message.message.delete()
    i = sts.get(full=True)
    to_list_chk = sts.get("TO_LIST") or [{"chat_id": i.TO}]
    for t in to_list_chk:
        tid = t.get("chat_id") if isinstance(t, dict) else t
        if tid in temp.IS_FRWD_CHAT:
            return await message.answer(
                "A target chat already has a job running. Wait or /cancel.",
                show_alert=True,
            )
    m = await msg_edit(message.message, "<code>Verifying…</code>")
    _bot = await db.get_bot(user)
    if not _bot:
      return await msg_edit(m, "<code>No bot/session. Use /settings first.</code>", wait=True)
    # Per-forward filters: snapshot taken when the forward was created (/fwd).
    # Older jobs without a snapshot fall back to the user's current /settings.
    tf = sts.get("TASK_FILTERS") or build_task_filters(await db.get_configs(user))
    tf = normalize(tf)
    forward_tag = tf["forward_tag"]
    protect = tf["protect"] or None
    button = (parse_buttons(tf["button"]) or None) if tf["button"] else None
    try:
      client = await start_clone_bot(CLIENT.client(_bot))
    except Exception as e:
      logger.exception("start client failed")
      return await msg_edit(m, f"<b>Could not start session:</b> <code>{html.escape(str(e))[:300]}</code>", wait=True)
    await msg_edit(m, "<code>Starting…</code>")
    temp.forwardings += 1
    await db.add_frwd(user)
    # Status only on this bot message — never spam Saved Messages / userbot DM
    sts.add(time=True)
    sleep = 0.3 if _bot['is_bot'] else 0.5
    await msg_edit(m, "<code>Processing…</code>")

    from_list = sts.get("FROM_LIST") or [{"chat_id": sts.get("FROM"), "last_msg_id": sts.get("limit"), "continuous": getattr(sts, "continuous", False)}]
    to_list = sts.get("TO_LIST") or [{"chat_id": sts.get("TO"), "topic": sts.get("TO_TOPIC")}]
    # normalize from_list entries
    norm_from = []
    for s in from_list:
        if isinstance(s, dict):
            norm_from.append(s)
        else:
            norm_from.append({"chat_id": s, "last_msg_id": sts.get("limit"), "continuous": False})
    norm_to = []
    for t in to_list:
        if isinstance(t, dict):
            norm_to.append(t)
        else:
            norm_to.append({"chat_id": t, "topic": None})

    # lock all target chats
    for t in norm_to:
        tid = t.get("chat_id")
        if tid not in temp.IS_FRWD_CHAT:
            temp.IS_FRWD_CHAT.append(tid)
    if user not in temp.lock:
        temp.lock[user] = []
    temp.lock[user].append(frwd_id)

    try:
        pling = 0
        await edit(m, "Progressing", 10, sts)
        for src in norm_from:
            if await is_cancelled(client, user, m, sts):
                return
            from_id = src.get("chat_id")
            limit = int(src.get("last_msg_id") or sts.get("limit") or 1)
            is_continuous = bool(src.get("continuous") or getattr(sts, "continuous", False))
            # probe source
            try:
                await client.get_messages(from_id, 1)
            except Exception:
                await msg_edit(
                    m,
                    f"**Cannot read source** <code>{from_id}</code>. "
                    f"Use userbot member or bot admin there.",
                    retry_btn(frwd_id), True,
                )
                continue

            for tgt in norm_to:
                if await is_cancelled(client, user, m, sts):
                    return
                to_id = tgt.get("chat_id")
                to_topic = tgt.get("topic")
                # update STS current pair for copy/forward helpers
                STATUS = sts.data
                if frwd_id in STATUS:
                    STATUS[frwd_id]["FROM"] = from_id
                    STATUS[frwd_id]["TO"] = to_id
                    STATUS[frwd_id]["TO_TOPIC"] = to_topic
                    STATUS[frwd_id]["limit"] = limit

                try:
                    k = await client.send_message(to_id, "·")
                    await k.delete()
                except Exception:
                    await msg_edit(
                        m,
                        f"**Cannot write target** <code>{to_id}</code>. "
                        f"Make session admin there.",
                        retry_btn(frwd_id), True,
                    )
                    continue

                MSG = []
                seen = set()  # duplicate tracking for this source → target pair
                logger.info(f"Forward {from_id} → {to_id} limit={limit} skip={sts.get('skip')}")
                async for message in client.iter_messages(
                    client,
                    chat_id=from_id,
                    limit=int(limit),
                    offset=int(sts.get("skip") or 0),
                    continuous=is_continuous,
                ):
                    if await is_cancelled(client, user, m, sts):
                        return
                    if pling % 20 == 0:
                        await edit(m, "Progressing", 10, sts)
                    pling += 1
                    sts.add("fetched")
                    ok, why = should_forward(message, tf, seen)
                    if not ok:
                        if why == "duplicate":
                            sts.add("duplicate")
                        elif why == "empty":
                            sts.add("deleted")
                        else:
                            sts.add("filtered")
                        continue
                    if forward_tag:
                        MSG.append(message.id)
                        notcompleted = len(MSG)
                        completed = sts.get("total") - sts.get("fetched")
                        if notcompleted >= 100 or completed <= 100:
                            await forward(client, MSG, m, sts, protect)
                            sts.add("total_files", notcompleted)
                            await asyncio.sleep(1)
                            MSG = []
                    else:
                        new_caption = build_output(message, tf)
                        details = {
                            "msg_obj": message,
                            "msg_id": message.id,
                            "media": media(message),
                            "caption": new_caption,
                            "button": button,
                            "protect": protect,
                        }
                        await copy(client, details, m, sts)
                        sts.add("total_files")
                        await asyncio.sleep(sleep)
                if MSG:
                    await forward(client, MSG, m, sts, protect)
                    sts.add("total_files", len(MSG))

        await edit(m, "Completed", "completed", sts)
    except Exception as e:
        await msg_edit(m, f"<b>ERROR:</b>\n<code>{e}</code>", wait=True)
    finally:
        for t in norm_to:
            tid = t.get("chat_id")
            if tid in temp.IS_FRWD_CHAT:
                try:
                    temp.IS_FRWD_CHAT.remove(tid)
                except ValueError:
                    pass
        await stop(client, user, frwd_id)
            
async def copy(bot, msg, m, sts):
   """Copy with restricted fallback (download+reupload) + topic support."""
   from .transfer import transfer_message, parse_dest
   to_raw = sts.get('TO')
   dest_chat, topic_id = parse_dest(to_raw)
   # channel may store topic separately
   topic_id = topic_id or sts.get('TO_TOPIC')
   try:
     ok, how = await transfer_message(
         bot, msg.get("msg_obj"), dest_chat,
         from_chat_id=sts.get('FROM'),
         message_id=msg.get("msg_id"),
         topic_id=topic_id,
         caption=msg.get("caption"),
         reply_markup=msg.get('button'),
         protect_content=msg.get("protect"),
         force_download=bool(sts.get('force_download')),
     )
     if not ok:
       print(f"Failed to transfer message {msg.get('msg_id')}: {how}")
       sts.add('deleted')
   except FloodWait as e:
     await edit(m, 'Progressing', e.value, sts)
     await asyncio.sleep(e.value)
     await edit(m, 'Progressing', 10, sts)
     await copy(bot, msg, m, sts)
   except Exception as e:
     print(f"Failed to copy message {msg.get('msg_id')}: {e}")
     sts.add('deleted')
        
async def forward(bot, msg, m, sts, protect):
   """Forward batch: try forward_messages, on fail per-msg restricted transfer."""
   from .transfer import transfer_message, parse_dest
   dest_chat, topic_id = parse_dest(sts.get('TO'))
   topic_id = topic_id or sts.get('TO_TOPIC')
   try:
     if topic_id:
       # forward_messages may not land in topic reliably — per-message transfer
       raise RuntimeError("topic_requires_copy")
     await bot.forward_messages(
           chat_id=dest_chat,
           from_chat_id=sts.get('FROM'), 
           protect_content=protect,
           message_ids=msg)
   except FloodWait as e:
     await edit(m, 'Progressing', e.value, sts)
     await asyncio.sleep(e.value)
     await edit(m, 'Progressing', 10, sts)
     await forward(bot, msg, m, sts, protect)
   except Exception as e:
      print(f"forward_messages failed ({e}); restricted transfer per msg")
      for mid in msg:
        ok, how = await transfer_message(
            bot, None, dest_chat,
            from_chat_id=sts.get('FROM'),
            message_id=mid,
            topic_id=topic_id,
            protect_content=protect,
            force_download=bool(sts.get('force_download')),
        )
        if not ok:
          print(f"Failed transfer {mid}: {how}")
          sts.add('deleted')
        await asyncio.sleep(0.3)

PROGRESS = """
📈 Percetage: {0} %

♻️ Feched: {1}

♻️ Fowarded: {2}

♻️ Remaining: {3}

♻️ Stataus: {4}

⏳️ ETA: {5}
"""

async def msg_edit(msg, text, button=None, wait=None):
    try:
        return await msg.edit(text, reply_markup=button)
    except MessageNotModified:
        pass 
    except FloodWait as e:
        if wait:
           await asyncio.sleep(e.value)
           return await msg_edit(msg, text, button, wait)
        
async def edit(msg, title, status, sts):
   i = sts.get(full=True)
   status = 'Forwarding' if status == 10 else f"Sleeping {status} s" if str(status).isnumeric() else status
   total = float(i.total) if float(i.total) > 0 else 1.0
   percentage = "{:.0f}".format(float(i.fetched)*100/total)
   
   now = time.time()
   diff = int(now - i.start)
   speed = sts.divide(i.fetched, diff)
   elapsed_time = round(diff) * 1000
   time_to_completion = round(sts.divide(i.total - i.fetched, int(speed))) * 1000
   estimated_total_time = elapsed_time + time_to_completion  
   progress = "◉{0}{1}".format(
       ''.join(["◉" for i in range(math.floor(int(percentage) / 10))]),
       ''.join(["◎" for i in range(10 - math.floor(int(percentage) / 10))]))
   button =  [[InlineKeyboardButton(title, f'fwrdstatus#{status}#{estimated_total_time}#{percentage}#{i.id}')]]
   estimated_total_time = TimeFormatter(milliseconds=estimated_total_time)
   estimated_total_time = estimated_total_time if estimated_total_time != '' else '0 s'

   text = TEXT.format(i.fetched, i.total_files, i.duplicate, i.deleted, i.skip, status, percentage, estimated_total_time, progress)
   if status not in ["cancelled", "completed"]:
      button.append([InlineKeyboardButton("• Cancel", f"terminate_frwd#{i.id}")])
   else:
      button.append([InlineKeyboardButton("• Menu", "menu#main")])
   await msg_edit(msg, text, InlineKeyboardMarkup(button))
   
async def is_cancelled(client, user, msg, sts):
   frwd_id = sts.id
   if temp.CANCEL.get(frwd_id)==True:
      to_list = sts.get("TO_LIST") or [{"chat_id": sts.TO}]
      for t in to_list:
         tid = t.get("chat_id") if isinstance(t, dict) else t
         if tid in temp.IS_FRWD_CHAT:
            try:
               temp.IS_FRWD_CHAT.remove(tid)
            except ValueError:
               pass
      await edit(msg, "Cancelled", "completed", sts)
      # no extra DM / Saved Messages noise
      await stop(client, user, frwd_id)
      return True 
   return False 

async def stop(client, user, frwd_id=None):
   try:
     await client.stop()
   except:
     pass 
   await db.rmve_frwd(user)
   temp.forwardings -= 1
   if frwd_id and user in temp.lock:
       try:
           temp.lock[user].remove(frwd_id)
       except ValueError:
           pass
   if frwd_id:
       temp.CANCEL.pop(frwd_id, None)
    
async def send(bot, user, text):
   try:
      await bot.send_message(user, text=text)
   except:
      pass 
     
def custom_caption(msg, caption):
  if msg.media:
    if (msg.video or msg.document or msg.audio or msg.photo):
      media = getattr(msg, msg.media.value, None)
      if media:
        file_name = getattr(media, 'file_name', '')
        file_size = getattr(media, 'file_size', '')
        fcaption = getattr(msg, 'caption', '')
        if fcaption:
          fcaption = fcaption.html
        if caption:
          return caption.format(filename=file_name, size=get_size(file_size), caption=fcaption)
        return fcaption
  return None

def get_size(size):
  units = ["Bytes", "KB", "MB", "GB", "TB", "PB", "EB"]
  size = float(size)
  i = 0
  while size >= 1024.0 and i < len(units):
     i += 1
     size /= 1024.0
  return "%.2f %s" % (size, units[i]) 

def media(msg):
  if msg.media:
     media = getattr(msg, msg.media.value, None)
     if media:
        return getattr(media, 'file_id', None)
  return None 

def TimeFormatter(milliseconds: int) -> str:
    seconds, milliseconds = divmod(int(milliseconds), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    tmp = ((str(days) + "d, ") if days else "") + \
        ((str(hours) + "h, ") if hours else "") + \
        ((str(minutes) + "m, ") if minutes else "") + \
        ((str(seconds) + "s, ") if seconds else "") + \
        ((str(milliseconds) + "ms, ") if milliseconds else "")
    return tmp[:-2]

def retry_btn(id):
    return InlineKeyboardMarkup([[InlineKeyboardButton('♻️ RETRY ♻️', f"start_public_{id}")]])

@Client.on_callback_query(filters.regex(r'^terminate_frwd'))
async def terminate_frwding(bot, m):
    parts = m.data.split("#")
    if len(parts) > 1:
        frwd_id = parts[1]
        temp.CANCEL[frwd_id] = True
    else:
        user_id = m.from_user.id
        for fid in temp.lock.get(user_id, []):
            temp.CANCEL[fid] = True
    await m.answer("Forwarding cancelled !", show_alert=True)
          
@Client.on_callback_query(filters.regex(r'^fwrdstatus'))
async def status_msg(bot, msg):
    _, status, est_time, percentage, frwd_id = msg.data.split("#")
    sts = STS(frwd_id)
    if not sts.verify():
       fetched, forwarded, remaining = 0, 0, 0
    else:
       fetched, forwarded = sts.get('fetched'), sts.get('total_files')
       remaining = fetched - forwarded 
    est_time = TimeFormatter(milliseconds=est_time)
    est_time = est_time if (est_time != '' or status not in ['completed', 'cancelled']) else '0 s'
    return await msg.answer(PROGRESS.format(percentage, fetched, forwarded, remaining, status, est_time), show_alert=True)
                  
@Client.on_callback_query(filters.regex(r'^close_btn$'))
async def close(bot, update):
    await update.answer()
    await update.message.delete()


# ─── LIVE FORWARD ────────────────────────────────────────────────────────────

def format_dest_live(chat_id, topic_id=None) -> str:
    return f"{chat_id}/{topic_id}" if topic_id else str(chat_id)


async def _live_watcher(client, from_id, to_id, user_id, notify_bot, auto_restart=False, to_topic=None, force_download=False):
    """Watches from_id; restricted transfer to to_id (optional topic)."""
    from .transfer import transfer_message
    key = (from_id, to_id)
    last_id = 0
    flood_notified = False
    meta = temp.LIVE_WATCHERS.get(key, {})
    force_download = force_download or meta.get("force_download", False)
    topic_id = to_topic if to_topic is not None else meta.get("to_topic")
    dest_chat = to_id
    # This watcher's own filters (snapshot from when it was created).
    tf = meta.get("task_filters")
    if not tf:
        tf = build_task_filters(await db.get_configs(user_id))
    tf = normalize(tf)
    if key in temp.LIVE_WATCHERS:
        # share the same dict with /filters editor so edits apply live
        temp.LIVE_WATCHERS[key]["task_filters"] = tf
    seen = set()
    live_markup = (parse_buttons(tf["button"]) or None) if tf["button"] else None

    try:
        async for msg in client.get_chat_history(from_id, limit=1):
            last_id = msg.id
    except Exception:
        last_id = 0

    while key in temp.LIVE_WATCHERS:
        await asyncio.sleep(3)
        try:
            new_msgs = []
            async for msg in client.get_chat_history(from_id, limit=50):
                if msg.id <= last_id:
                    break
                if msg.empty or msg.service:
                    continue
                new_msgs.append(msg)

            for msg in reversed(new_msgs):
                if key not in temp.LIVE_WATCHERS:
                    break
                ok_f, _why = should_forward(msg, tf, seen)
                live_meta = temp.LIVE_WATCHERS.get(key) or {}
                if not ok_f:
                    if _why != "empty":
                        live_meta["filtered"] = live_meta.get("filtered", 0) + 1
                    last_id = max(last_id, msg.id)
                    continue
                live_caption = build_output(msg, tf)
                try:
                    ok, how = await transfer_message(
                        client, msg, dest_chat,
                        topic_id=topic_id,
                        caption=live_caption,
                        reply_markup=live_markup,
                        protect_content=tf["protect"] or None,
                        uploader=notify_bot,
                        force_download=force_download,
                    )
                    if not ok:
                        # restricted fail without fatal access error — log + skip
                        err_str = str(how).lower()
                        fatal_keywords = [
                            "copyright", "banned", "blocked", "channel_private",
                            "chat_write_forbidden", "user_banned_in_channel",
                            "chat_admin_required", "forbidden", "not accessible",
                            "channelprivate", "chatwriteforbidden", "userbannedinchannel"
                        ]
                        if any(k in err_str for k in fatal_keywords):
                            raise RuntimeError(how)
                        logger.error(f"[LiveFwd] Transfer fail msg {msg.id}: {how}")
                    if ok:
                        live_meta["sent"] = live_meta.get("sent", 0) + 1
                    last_id = max(last_id, msg.id)
                    flood_notified = False
                    await asyncio.sleep(0.5)
                except FloodWait as e:
                    wait = e.value
                    logger.warning(f"[LiveFwd] FloodWait {wait}s on msg {msg.id}")
                    if not flood_notified:
                        try:
                            await notify_bot.send_message(
                                user_id,
                                f"⚠️ <b>Live Forward FloodWait</b>\n"
                                f"From: <code>{from_id}</code> → <code>{format_dest_live(dest_chat, topic_id)}</code>\n"
                                f"Sleeping <b>{wait}s</b> due to Telegram rate limit. Will resume automatically."
                            )
                            flood_notified = True
                        except Exception:
                            pass
                    await asyncio.sleep(wait)
                    try:
                        ok, how = await transfer_message(
                            client, msg, dest_chat,
                            topic_id=topic_id,
                            caption=live_caption,
                            reply_markup=live_markup,
                            protect_content=tf["protect"] or None,
                            uploader=notify_bot,
                            force_download=force_download,
                        )
                        if ok:
                            last_id = max(last_id, msg.id)
                        else:
                            logger.error(f"[LiveFwd] Retry failed msg {msg.id}: {how}")
                    except Exception as retry_err:
                        logger.error(f"[LiveFwd] Retry failed msg {msg.id}: {retry_err}")
                except Exception as e:
                    err_str = str(e).lower()
                    fatal_keywords = [
                        "copyright", "banned", "blocked", "channel_private",
                        "chat_write_forbidden", "user_banned_in_channel",
                        "chat_admin_required", "forbidden", "not accessible",
                        "channelprivate", "chatwriteforbidden", "userbannedinchannel"
                    ]
                    if any(k in err_str for k in fatal_keywords):
                        logger.error(f"[LiveFwd] Fatal error on msg {msg.id}: {e}")
                        try:
                            await notify_bot.send_message(
                                user_id,
                                f"🚫 <b>Live Forward Stopped</b>\n\n"
                                f"From: <code>{from_id}</code> → <code>{format_dest_live(dest_chat, topic_id)}</code>\n\n"
                                f"<b>Reason:</b> <code>{e}</code>\n\n"
                                f"Channel may be blocked, copyright struck, or bot lost access.\n"
                                f"Watcher has been removed."
                            )
                        except Exception:
                            pass
                        temp.LIVE_WATCHERS.pop(key, None)
                        await db.remove_live_watcher(user_id, from_id, to_id)
                        return
                    logger.error(f"[LiveFwd] Error msg {msg.id}: {e}")
                    last_id = max(last_id, msg.id)

        except FloodWait as e:
            wait = e.value
            logger.warning(f"[LiveFwd] Outer FloodWait {wait}s")
            if not flood_notified:
                try:
                    await notify_bot.send_message(
                        user_id,
                        f"⚠️ <b>Live Forward FloodWait</b>\n"
                        f"From: <code>{from_id}</code> → <code>{format_dest_live(dest_chat, topic_id)}</code>\n"
                        f"Sleeping <b>{wait}s</b>. Will resume automatically."
                    )
                    flood_notified = True
                except Exception:
                    pass
            await asyncio.sleep(wait)
        except Exception as e:
            err_str = str(e).lower()
            fatal_keywords = [
                "copyright", "banned", "blocked", "channel_private",
                "chat_write_forbidden", "user_banned_in_channel",
                "chat_admin_required", "forbidden", "not accessible",
                "channelprivate", "chatwriteforbidden", "userbannedinchannel"
            ]
            if any(k in err_str for k in fatal_keywords):
                logger.error(f"[LiveFwd] Fatal outer error: {e}")
                try:
                    await notify_bot.send_message(
                        user_id,
                        f"🚫 <b>Live Forward Stopped</b>\n\n"
                        f"From: <code>{from_id}</code> → <code>{format_dest_live(dest_chat, topic_id)}</code>\n\n"
                        f"<b>Reason:</b> <code>{e}</code>\n\n"
                        f"Channel may be blocked, copyright struck, or bot lost access.\n"
                        f"Watcher has been removed."
                    )
                except Exception:
                    pass
                temp.LIVE_WATCHERS.pop(key, None)
                await db.remove_live_watcher(user_id, from_id, to_id)
                return
            logger.error(f"[LiveFwd] Watcher crashed: {e}")
            if auto_restart and key in temp.LIVE_WATCHERS:
                logger.info(f"[LiveFwd] Auto-restarting in 10s...")
                await asyncio.sleep(10)
                continue
            break

    try:
        await client.stop()
    except Exception:
        pass
    temp.LIVE_WATCHERS.pop(key, None)
    await db.remove_live_watcher(user_id, from_id, to_id)
    try:
        await notify_bot.send_message(
            user_id,
            f"🔴 <b>Live forward stopped</b>\n<code>{from_id}</code> → <code>{format_dest_live(dest_chat, topic_id)}</code>"
        )
    except Exception:
        pass


async def _start_live_watcher(
    bot,
    user_id,
    from_id,
    to_id,
    conn_name=None,
    notify=True,
    from_title=None,
    to_title=None,
    from_username=None,
    to_username=None,
    to_topic=None,
    force_download=False,
    task_filters=None,
):
    """Helper to start a watcher task and register it.
    task_filters: this watcher's own filters; defaults to a snapshot of /settings."""
    from .transfer import parse_dest
    # allow target as chat/topic
    dest_chat, parsed_topic = parse_dest(to_id)
    if isinstance(dest_chat, int) or (isinstance(dest_chat, str) and str(dest_chat).lstrip("-").isdigit()):
        try:
            to_id = int(dest_chat)
        except Exception:
            to_id = dest_chat
    else:
        # resolve later if username — keep as passed int expected
        pass
    to_topic = to_topic if to_topic is not None else parsed_topic

    key = (from_id, to_id)
    if key in temp.LIVE_WATCHERS:
        return False, "already_running"

    if conn_name:
        conn = await db.get_connection(user_id, conn_name)
        if not conn:
            return False, f"no_conn:{conn_name}"
        _bot_data = conn["bot_data"]
        _bot_data["user_id"] = user_id
    else:
        _bot_data = await db.get_bot(user_id)
    if not _bot_data:
        return False, "no_bot"
    try:
        client = await start_clone_bot(CLIENT.client(_bot_data))
    except Exception as e:
        return False, str(e)

    if not task_filters:
        task_filters = build_task_filters(await db.get_configs(user_id))
    task_filters = normalize(task_filters)

    topic_label = f"/{to_topic}" if to_topic else ""
    temp.LIVE_WATCHERS[key] = {
        "started_at": time.time(),
        "sent": 0,
        "filtered": 0,
        "task_filters": task_filters,
        "client": client,
        "user_id": user_id,
        "conn_name": conn_name or "default",
        "from_title": from_title or str(from_id),
        "to_title": (to_title or str(to_id)) + topic_label,
        "from_username": from_username,
        "to_username": to_username,
        "to_topic": to_topic,
        "force_download": force_download,
    }
    await db.save_live_watcher(
        user_id, from_id, to_id,
        conn_name=conn_name,
        from_title=from_title,
        to_title=to_title,
        from_username=from_username,
        to_username=to_username,
        to_topic=to_topic,
        force_download=force_download,
        task_filters=task_filters,
    )
    asyncio.create_task(
        _live_watcher(
            client, from_id, to_id, user_id, bot,
            auto_restart=True, to_topic=to_topic, force_download=force_download,
        )
    )
    return True, "ok"


def _live_label(fid, tid, meta: dict = None) -> str:
    meta = meta or {}
    ft = meta.get("from_title") or str(fid)
    tt = meta.get("to_title") or str(tid)
    fu = meta.get("from_username")
    tu = meta.get("to_username")
    topic = meta.get("to_topic")
    mode = "download" if meta.get("force_download") else "auto"
    fpart = f"{ft}" + (f" (@{fu})" if fu else "")
    tpart = f"{tt}" + (f" (@{tu})" if tu else "")
    topic_s = f"/{topic}" if topic else ""
    return (
        f"{fpart} → {tpart}{topic_s}\n"
        f"<code>{fid}</code> → <code>{tid}{topic_s}</code> · mode <code>{mode}</code>"
    )


@Client.on_message(filters.private & filters.command("live"))
async def live_forward_setup(bot, message):
    """
    /live                         → guided setup
    /live <source> <target> [conn]
    source/target: ID, @username, t.me link
    """
    user_id = message.from_user.id
    args = message.text.split(maxsplit=3)

    if len(args) == 1:
        bot_data = await db.get_bot(user_id)
        if not bot_data:
            return await message.reply("No bot/session configured. Use /settings first.")
        PENDING_LIVE[user_id] = {"step": "source"}
        return await message.reply(
            "<b>🔴 Live Forward Setup</b>\n\n"
            "<b>Step 1/2:</b> Send the <b>SOURCE</b> chat\n\n"
            "You can send:\n"
            "• <code>@channel</code> or username\n"
            "• Numeric ID <code>-100...</code>\n"
            "• <code>https://t.me/channel</code>\n"
            "• Forward any message from that chat\n\n"
            "/cancel — cancel"
        )

    if len(args) < 3:
        return await message.reply(
            "<b>Usage:</b>\n"
            "• <code>/live</code> — guided setup\n"
            "• <code>/live source target [conn_name]</code>\n\n"
            "Source/target examples:\n"
            "• <code>@mychannel</code>\n"
            "• <code>-1001234567890</code>\n"
            "• <code>https://t.me/mychannel</code>\n\n"
            "Example:\n"
            "<code>/live @sourcechannel @targetchannel</code>\n"
            "<code>/live @source -1009876543210/42 myconn</code>\n"
            "Topic target: <code>-100CHAT/TOPIC_ID</code>\n"
            "Optional last arg: <code>download</code> force restricted mode"
        )

    source_raw = args[1]
    target_raw = args[2]
    # optional conn and/or download flag
    rest = args[3].strip().lower() if len(args) >= 4 else ""
    conn_name = None
    force_download = False
    if rest:
        parts = rest.split()
        for p in parts:
            if p in ("download", "dl", "force"):
                force_download = True
            else:
                conn_name = p

    status = await message.reply("⏳ Resolving chats…")

    from .transfer import parse_dest
    dest_chat_ref, to_topic = parse_dest(target_raw)
    resolve_target = dest_chat_ref if dest_chat_ref is not None else target_raw

    client, err = await get_client_for_user(user_id, conn_name)
    if not client:
        client2, err2 = await get_client_for_user(user_id, None)
        if not client2:
            return await status.edit(err or err2)
        client = client2

    try:
        from_chat, from_err = await resolve_chat(client, source_raw)
        if not from_chat:
            return await status.edit(f"❌ <b>Source:</b> {from_err}")

        to_chat, to_err = await resolve_chat(client, resolve_target)
        if not to_chat:
            return await status.edit(f"❌ <b>Target:</b> {to_err}")

        from_id, to_id = from_chat.id, to_chat.id
        from_title = from_chat.title or getattr(from_chat, "first_name", str(from_id))
        to_title = to_chat.title or getattr(to_chat, "first_name", str(to_id))
        from_user = from_chat.username
        to_user = to_chat.username
    finally:
        try:
            await client.stop()
        except Exception:
            pass

    if not conn_name:
        from plugins.connections import get_conn_for
        live_conns = await get_conn_for(user_id, "live")
        if live_conns:
            buttons = [[InlineKeyboardButton(
                f"@{c['bot_data'].get('username','?')} ({c['name']})",
                callback_data=f"live_conn#{from_id}#{to_id}#{c['name']}#{user_id}"
            )] for c in live_conns]
            buttons.append([InlineKeyboardButton(
                "🔄 Use Active Connection",
                callback_data=f"live_conn#{from_id}#{to_id}#default#{user_id}"
            )])
            PENDING_LIVE_META[user_id] = {
                "from_title": from_title, "to_title": to_title,
                "from_username": from_user, "to_username": to_user,
                "to_topic": to_topic, "force_download": force_download,
            }
            return await status.edit(
                f"<b>Select connection for live forward:</b>\n\n"
                f"From: {format_chat_label(title=from_title, username=from_user, chat_id=from_id)}\n"
                f"To: {format_chat_label(title=to_title, username=to_user, chat_id=to_id)}",
                reply_markup=InlineKeyboardMarkup(buttons)
            )

    ok, reason = await _start_live_watcher(
        bot, user_id, from_id, to_id,
        conn_name=conn_name,
        from_title=from_title,
        to_title=to_title,
        from_username=from_user,
        to_username=to_user,
        to_topic=to_topic,
        force_download=force_download,
    )
    if not ok:
        if reason == "already_running":
            return await status.edit("Live watcher already running for this pair.")
        elif reason == "no_bot":
            return await status.edit("No bot/session configured. Use /settings first.")
        elif reason.startswith("no_conn:"):
            return await status.edit(f"Connection <code>{reason.split(':')[1]}</code> not found. Use /listconn.")
        else:
            return await status.edit(f"Failed to start: <code>{reason}</code>")

    conn_label = f"<code>{conn_name}</code>" if conn_name else "active connection"
    topic_note = f" · topic <code>{to_topic}</code>" if to_topic else ""
    mode_note = "download" if force_download else "auto"
    await status.edit(
        f"✅ <b>Live forward started</b>\n\n"
        f"From: {format_chat_label(title=from_title, username=from_user, chat_id=from_id)}\n"
        f"To: {format_chat_label(title=to_title, username=to_user, chat_id=to_id)}{topic_note}\n"
        f"Connection: {conn_label}\n"
        f"Mode: <code>{mode_note}</code> (restricted-safe)\n\n"
        f"No forward tag. Auto-restarts on crash.\n"
        f"Use /lives or /stoplive to manage. You can start multiple lives."
    )


@Client.on_message(filters.private & filters.command("cancel"))
async def cancel_pending_live(bot, message):
    user_id = message.from_user.id
    cleared = False
    if user_id in PENDING_LIVE:
        PENDING_LIVE.pop(user_id, None)
        PENDING_LIVE_META.pop(user_id, None)
        cleared = True
    try:
        from plugins.connections import PENDING_SCHEDULE
        if user_id in PENDING_SCHEDULE:
            PENDING_SCHEDULE.pop(user_id, None)
            cleared = True
    except Exception:
        pass
    if cleared:
        return await message.reply("✅ Setup cancelled.")


@Client.on_message(filters.private & ~filters.command([
    "start", "fwd", "forward", "settings", "reset", "status",
    "live", "stoplive", "lives", "cancel", "addconn", "listconn", "useconn",
    "delconn", "schedule", "listschedule", "cancelschedule", "help", "filters", "tasks",
    "broadcast", "resetall", "unequify", "save", "batchsave", "savelink", "cancelsave"
]))
async def live_guided_flow(bot, message: Message):
    """Guided /live multi-step conversation."""
    user_id = message.from_user.id

    try:
        from plugins.connections import PENDING_SCHEDULE
        if user_id in PENDING_SCHEDULE:
            return
    except Exception:
        pass

    if user_id not in PENDING_LIVE:
        return

    state = PENDING_LIVE[user_id]
    step = state.get("step")

    client, err = await get_client_for_user(user_id, None)
    if not client:
        PENDING_LIVE.pop(user_id, None)
        return await message.reply(err)

    try:
        if step == "source":
            chat, e = await resolve_chat(client, message)
            if not chat:
                return await message.reply(f"❌ {e}\n\nTry again or /cancel")
            state["from_id"] = chat.id
            state["from_title"] = chat.title or getattr(chat, "first_name", str(chat.id))
            state["from_username"] = chat.username
            state["step"] = "target"
            return await message.reply(
                f"✅ Source: {format_chat_label(chat)}\n\n"
                f"<b>Step 2/3:</b> Send the <b>TARGET</b> chat\n"
                f"(username / ID / link / forward)\n"
                f"Forum topic: <code>-100CHAT/TOPIC_ID</code>\n\n"
                f"/cancel — cancel"
            )

        if step == "target":
            from .transfer import parse_dest
            to_topic = None
            resolve_q = message
            if message.text:
                dest_ref, to_topic = parse_dest(message.text.strip())
                if dest_ref is not None and to_topic is not None:
                    resolve_q = dest_ref
            chat, e = await resolve_chat(client, resolve_q)
            if not chat:
                return await message.reply(f"❌ {e}\n\nTry again or /cancel")
            state["to_id"] = chat.id
            state["to_title"] = chat.title or getattr(chat, "first_name", str(chat.id))
            state["to_username"] = chat.username
            state["to_topic"] = to_topic
            state["step"] = "mode"
            topic_note = f" · topic <code>{to_topic}</code>" if to_topic else ""
            return await message.reply(
                f"✅ Target: {format_chat_label(chat)}{topic_note}\n\n"
                f"<b>Step 3/3:</b> Transfer mode\n"
                f"• <code>auto</code> — copy then download if restricted\n"
                f"• <code>download</code> — always download+reupload\n\n"
                f"Send <code>auto</code> or <code>download</code>"
            )

        if step == "mode":
            text = (message.text or "").strip().lower()
            if text not in ("auto", "download", "1", "2"):
                return await message.reply("Send <code>auto</code> or <code>download</code>")
            force_download = text in ("download", "2")
            PENDING_LIVE.pop(user_id, None)

            from_id = state["from_id"]
            to_id = state["to_id"]
            to_topic = state.get("to_topic")

            from plugins.connections import get_conn_for
            live_conns = await get_conn_for(user_id, "live")
            if live_conns:
                buttons = [[InlineKeyboardButton(
                    f"@{c['bot_data'].get('username','?')} ({c['name']})",
                    callback_data=f"live_conn#{from_id}#{to_id}#{c['name']}#{user_id}"
                )] for c in live_conns]
                buttons.append([InlineKeyboardButton(
                    "🔄 Use Active Connection",
                    callback_data=f"live_conn#{from_id}#{to_id}#default#{user_id}"
                )])
                PENDING_LIVE_META[user_id] = {
                    "from_title": state["from_title"], "to_title": state["to_title"],
                    "from_username": state.get("from_username"), "to_username": state.get("to_username"),
                    "to_topic": to_topic, "force_download": force_download,
                }
                return await message.reply(
                    f"<b>Select connection:</b>\n\n"
                    f"From: {format_chat_label(title=state['from_title'], username=state.get('from_username'), chat_id=from_id)}\n"
                    f"To: {format_chat_label(title=state['to_title'], username=state.get('to_username'), chat_id=to_id)}",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )

            ok, reason = await _start_live_watcher(
                bot, user_id, from_id, to_id,
                from_title=state["from_title"],
                to_title=state["to_title"],
                from_username=state.get("from_username"),
                to_username=state.get("to_username"),
                to_topic=to_topic,
                force_download=force_download,
            )
            if not ok:
                return await message.reply(f"Failed: <code>{reason}</code>")
            mode_note = "download" if force_download else "auto"
            topic_note = f" · topic <code>{to_topic}</code>" if to_topic else ""
            return await message.reply(
                f"✅ <b>Live forward started</b>\n\n"
                f"From: {format_chat_label(title=state['from_title'], username=state.get('from_username'), chat_id=from_id)}\n"
                f"To: {format_chat_label(title=state['to_title'], username=state.get('to_username'), chat_id=to_id)}{topic_note}\n"
                f"Mode: <code>{mode_note}</code>\n\n"
                f"Use /lives to manage multiple lives."
            )
    finally:
        try:
            await client.stop()
        except Exception:
            pass


@Client.on_callback_query(filters.regex(r'^live_conn#'))
async def live_conn_picker(bot, query):
    parts = query.data.split("#")
    _, from_id, to_id, conn_name, uid = parts
    if int(uid) != query.from_user.id:
        return await query.answer("Not your request.", show_alert=True)
    user_id = int(uid)
    conn_name = None if conn_name == "default" else conn_name

    meta = PENDING_LIVE_META.pop(user_id, {}) or {}
    ok, reason = await _start_live_watcher(
        bot, user_id, int(from_id), int(to_id),
        conn_name=conn_name,
        from_title=meta.get("from_title"),
        to_title=meta.get("to_title"),
        from_username=meta.get("from_username"),
        to_username=meta.get("to_username"),
        to_topic=meta.get("to_topic"),
        force_download=bool(meta.get("force_download")),
    )
    if not ok:
        return await query.answer(f"Failed: {reason}", show_alert=True)
    conn_label = conn_name or "active"
    mode_note = "download" if meta.get("force_download") else "auto"
    topic_note = f" · topic <code>{meta.get('to_topic')}</code>" if meta.get("to_topic") else ""
    await query.message.edit(
        f"✅ <b>Live forward started</b>\n"
        f"From: {format_chat_label(title=meta.get('from_title'), username=meta.get('from_username'), chat_id=int(from_id))}\n"
        f"To: {format_chat_label(title=meta.get('to_title'), username=meta.get('to_username'), chat_id=int(to_id))}{topic_note}\n"
        f"Connection: <code>{conn_label}</code>\n"
        f"Mode: <code>{mode_note}</code>"
    )


@Client.on_message(filters.private & filters.command(["stoplive", "lives"]))
async def stop_live(bot, message):
    """
    /lives or /stoplive          → list with titles + stop buttons
    /stoplive <from> <to>        → stop pair (username/link/ID ok)
    """
    user_id = message.from_user.id
    args = message.text.split(maxsplit=2)
    cmd = args[0].split("@")[0].lower()

    if len(args) == 1 or cmd.endswith("lives"):
        user_watchers = [
            (k, v) for k, v in temp.LIVE_WATCHERS.items()
            if isinstance(k, tuple) and len(k) == 2 and v.get("user_id") == user_id
        ]
        if not user_watchers:
            db_watchers = [w async for w in await db.get_all_live_watchers(user_id)]
            if not db_watchers:
                return await message.reply("No live watchers running.\nUse /live to start one (supports multiple).")
            text = "<b>📋 Live Watchers (saved, may need restore):</b>\n\n"
            buttons = []
            for i, w in enumerate(db_watchers, 1):
                fid, tid = w["from_id"], w["to_id"]
                text += f"{i}. {_live_label(fid, tid, w)}\n\n"
                buttons.append([InlineKeyboardButton(
                    f"🔴 Stop {i}", callback_data=f"stoplive#{fid}#{tid}#{user_id}"
                )])
            buttons.append([InlineKeyboardButton("🔴 Stop All", callback_data=f"stoplive_all#{user_id}")])
            return await message.reply(text, reply_markup=InlineKeyboardMarkup(buttons))

        buttons = []
        text = f"<b>📋 Active Live Watchers ({len(user_watchers)}):</b>\n\n"
        for i, ((fid, tid), meta) in enumerate(user_watchers, 1):
            text += f"{i}. {_live_label(fid, tid, meta)}\n"
            text += f"   Conn: <code>{meta.get('conn_name', 'default')}</code>\n\n"
            buttons.append([InlineKeyboardButton(
                f"🔴 Stop {i}", callback_data=f"stoplive#{fid}#{tid}#{user_id}"
            )])
        buttons.append([InlineKeyboardButton("🔴 Stop All", callback_data=f"stoplive_all#{user_id}")])
        return await message.reply(text, reply_markup=InlineKeyboardMarkup(buttons))

    if len(args) != 3:
        return await message.reply(
            "Usage:\n"
            "• <code>/lives</code> — list all\n"
            "• <code>/stoplive</code> — list all\n"
            "• <code>/stoplive source target</code> — stop one pair"
        )

    client, err = await get_client_for_user(user_id, None)
    if not client:
        try:
            key = (int(args[1]), int(args[2]))
        except ValueError:
            return await message.reply(err)
    else:
        try:
            from_chat, e1 = await resolve_chat(client, args[1])
            to_chat, e2 = await resolve_chat(client, args[2])
            if not from_chat:
                return await message.reply(f"Source: {e1}")
            if not to_chat:
                return await message.reply(f"Target: {e2}")
            key = (from_chat.id, to_chat.id)
        finally:
            try:
                await client.stop()
            except Exception:
                pass

    if key not in temp.LIVE_WATCHERS:
        await db.remove_live_watcher(user_id, key[0], key[1])
        return await message.reply("No live watcher found for that pair (removed from DB if present).")

    temp.LIVE_WATCHERS.pop(key, None)
    await db.remove_live_watcher(user_id, key[0], key[1])
    await message.reply(f"✅ Live watcher stopped.\n<code>{key[0]}</code> → <code>{key[1]}</code>")


@Client.on_callback_query(filters.regex(r'^stoplive#'))
async def stoplive_btn(bot, query):
    _, from_id, to_id, uid = query.data.split("#")
    if int(uid) != query.from_user.id:
        return await query.answer("Not your watcher.", show_alert=True)
    key = (int(from_id), int(to_id))
    meta = temp.LIVE_WATCHERS.pop(key, {}) or {}
    await db.remove_live_watcher(int(uid), int(from_id), int(to_id))
    await query.answer("Stopped.", show_alert=True)
    await query.message.edit(
        f"🔴 Stopped:\n{_live_label(int(from_id), int(to_id), meta)}"
    )


@Client.on_callback_query(filters.regex(r'^stoplive_all#'))
async def stoplive_all_btn(bot, query):
    _, uid = query.data.split("#")
    if int(uid) != query.from_user.id:
        return await query.answer("Not your watchers.", show_alert=True)
    user_id = int(uid)
    stopped = 0
    for key in list(temp.LIVE_WATCHERS.keys()):
        if isinstance(key, tuple) and len(key) == 2 and temp.LIVE_WATCHERS.get(key, {}).get("user_id") == user_id:
            temp.LIVE_WATCHERS.pop(key, None)
            await db.remove_live_watcher(user_id, key[0], key[1])
            stopped += 1
    await query.answer(f"Stopped {stopped} watcher(s).", show_alert=True)
    await query.message.edit(f"🔴 All <b>{stopped}</b> live watcher(s) stopped.")


async def restore_live_watchers(bot):
    """Called on bot startup — restores live watchers saved in DB."""
    try:
        watchers = await db.get_all_live_watchers()
        count = 0
        async for w in watchers:
            user_id = w["user_id"]
            from_id = w["from_id"]
            to_id = w["to_id"]
            conn_name = w.get("conn_name")
            key = (from_id, to_id)
            if key in temp.LIVE_WATCHERS:
                continue
            if conn_name:
                conn = await db.get_connection(user_id, conn_name)
                _bot_data = conn["bot_data"] if conn else await db.get_bot(user_id)
            else:
                _bot_data = await db.get_bot(user_id)
            if not _bot_data:
                continue
            try:
                client = await start_clone_bot(CLIENT.client(_bot_data))
                to_topic = w.get("to_topic")
                force_download = bool(w.get("force_download"))
                temp.LIVE_WATCHERS[key] = {
                    "client": client,
                    "user_id": user_id,
                    "task_filters": w.get("task_filters"),
                    "conn_name": conn_name or "default",
                    "from_title": w.get("from_title") or str(from_id),
                    "to_title": w.get("to_title") or str(to_id),
                    "from_username": w.get("from_username"),
                    "to_username": w.get("to_username"),
                    "to_topic": to_topic,
                    "force_download": force_download,
                }
                asyncio.create_task(
                    _live_watcher(
                        client, from_id, to_id, user_id, bot,
                        auto_restart=True, to_topic=to_topic, force_download=force_download,
                    )
                )
                count += 1
            except Exception as e:
                logger.error(f"[AutoRestart] Failed to restore watcher {from_id}->{to_id}: {e}")
        if count:
            logger.info(f"[AutoRestart] Restored {count} live watcher(s) from DB.")
    except Exception as e:
        logger.error(f"[AutoRestart] restore_live_watchers error: {e}")
