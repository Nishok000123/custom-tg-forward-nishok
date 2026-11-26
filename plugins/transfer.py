"""
Shared restricted transfer: copy first, then download+reupload.
Topic support: dest as chat_id or "chat_id/topic_id".
"""
import os
import re
import asyncio
import logging
import tempfile
from typing import Optional, Tuple, Any, Callable, Awaitable

from pyrogram.errors import FloodWait

logger = logging.getLogger(__name__)

# -100123/45  or  -100123  or  @user/45
_DEST_RE = re.compile(
    r"^(?P<chat>-?\d+|@?[a-zA-Z][\w\d_]{2,})(?:/(?P<topic>\d+))?$"
)


def parse_dest(raw) -> Tuple[Any, Optional[int]]:
    """
    Parse target into (chat_id_or_username, topic_id|None).
    Accepts int, str chat id, or "chat/topic".
    """
    if raw is None:
        return None, None
    if isinstance(raw, int):
        return raw, None
    text = str(raw).strip()
    # t.me/c/ID/TOPIC style already handled elsewhere; here chat/topic only
    m = _DEST_RE.match(text.replace(" ", ""))
    if not m:
        # try plain int
        try:
            return int(text), None
        except ValueError:
            return text, None
    chat = m.group("chat")
    topic = int(m.group("topic")) if m.group("topic") else None
    if chat.lstrip("-").isdigit():
        chat = int(chat)
    elif chat.startswith("@"):
        chat = chat[1:]
    return chat, topic


def format_dest(chat_id, topic_id=None) -> str:
    if topic_id:
        return f"{chat_id}/{topic_id}"
    return str(chat_id)


async def _send_file(
    uploader,
    dest_chat,
    path: str,
    msg,
    caption=None,
    reply_markup=None,
    protect_content=None,
    message_thread_id=None,
):
    kw = {}
    if message_thread_id:
        kw["message_thread_id"] = message_thread_id
    if reply_markup is not None:
        kw["reply_markup"] = reply_markup
    if protect_content is not None:
        kw["protect_content"] = protect_content

    cap_kw = {**kw}
    if caption is not None:
        cap_kw["caption"] = caption

    if msg.photo:
        return await uploader.send_photo(dest_chat, path, **cap_kw)
    if msg.video:
        return await uploader.send_video(
            dest_chat, path,
            duration=getattr(msg.video, "duration", 0) or 0,
            width=getattr(msg.video, "width", 0) or 0,
            height=getattr(msg.video, "height", 0) or 0,
            **cap_kw,
        )
    if msg.animation:
        return await uploader.send_animation(dest_chat, path, **cap_kw)
    if msg.audio:
        return await uploader.send_audio(
            dest_chat, path,
            duration=getattr(msg.audio, "duration", 0) or 0,
            performer=getattr(msg.audio, "performer", None),
            title=getattr(msg.audio, "title", None),
            **cap_kw,
        )
    if msg.voice:
        return await uploader.send_voice(dest_chat, path, **cap_kw)
    if msg.video_note:
        return await uploader.send_video_note(dest_chat, path, **kw)
    if msg.sticker:
        return await uploader.send_sticker(dest_chat, path, **kw)
    if msg.document:
        return await uploader.send_document(
            dest_chat, path,
            file_name=getattr(msg.document, "file_name", None),
            **cap_kw,
        )
    return await uploader.send_document(dest_chat, path, **cap_kw)


async def transfer_message(
    client,
    msg,
    dest_chat,
    *,
    from_chat_id=None,
    message_id=None,
    topic_id: Optional[int] = None,
    caption=None,
    reply_markup=None,
    protect_content=None,
    uploader=None,
    force_download: bool = False,
    status_cb: Optional[Callable[[str], Awaitable[None]]] = None,
) -> Tuple[bool, str]:
    """
    Transfer one message to dest.
    - force_download=False (default): try copy, fallback download+reupload
    - force_download=True: always download+reupload (restricted-safe schedule mode)

    client = session that can READ source (usually userbot)
    uploader = who UPLOADS (bot preferred); defaults to client
    """
    uploader = uploader or client
    from_chat = from_chat_id if from_chat_id is not None else (msg.chat.id if msg else None)
    mid = message_id if message_id is not None else (msg.id if msg else None)
    thread_kw = {"message_thread_id": topic_id} if topic_id else {}

    # Resolve caption from msg if not given
    if caption is None and msg is not None:
        if msg.caption:
            try:
                caption = msg.caption.html
            except Exception:
                caption = msg.caption
        elif msg.text and not msg.media:
            try:
                caption = msg.text.html
            except Exception:
                caption = msg.text

    async def _status(t: str):
        if status_cb:
            try:
                await status_cb(t)
            except Exception:
                pass

    # ── path A: copy (unless force download) ─────────────────────────────────
    if not force_download and mid is not None and from_chat is not None:
        try:
            copy_kw = {
                "chat_id": dest_chat,
                "from_chat_id": from_chat,
                "message_id": mid,
                **thread_kw,
            }
            if caption is not None:
                copy_kw["caption"] = caption
            if reply_markup is not None:
                copy_kw["reply_markup"] = reply_markup
            if protect_content is not None:
                copy_kw["protect_content"] = protect_content
            await client.copy_message(**copy_kw)
            return True, "copied"
        except FloodWait as e:
            await asyncio.sleep(e.value)
            try:
                await client.copy_message(
                    chat_id=dest_chat,
                    from_chat_id=from_chat,
                    message_id=mid,
                    caption=caption,
                    reply_markup=reply_markup,
                    protect_content=protect_content,
                    **thread_kw,
                )
                return True, "copied"
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"copy failed msg {mid}: {e}")

    # Need full message object for download path
    if msg is None and mid is not None and from_chat is not None:
        try:
            msg = await client.get_messages(from_chat, mid)
        except Exception as e:
            return False, f"fetch failed: {e}"
        if not msg or getattr(msg, "empty", False):
            return False, "empty message"

    if msg is None:
        return False, "no message"

    # text only
    if msg.text and not msg.media:
        try:
            await uploader.send_message(
                dest_chat,
                msg.text.html if hasattr(msg.text, "html") else msg.text,
                reply_markup=reply_markup,
                protect_content=protect_content,
                **thread_kw,
            )
            return True, "text"
        except Exception as e:
            try:
                await client.send_message(
                    dest_chat, msg.text, **thread_kw
                )
                return True, "text-client"
            except Exception as e2:
                return False, f"{e} | {e2}"

    if not msg.media:
        # non-media non-text (service etc.)
        try:
            await client.copy_message(
                chat_id=dest_chat,
                from_chat_id=from_chat or msg.chat.id,
                message_id=msg.id,
                **thread_kw,
            )
            return True, "copied"
        except Exception as e:
            return False, str(e)

    # ── path B: download + reupload ──────────────────────────────────────────
    tmpdir = tempfile.mkdtemp(prefix="xfer_")
    path = None
    try:
        await _status("⬇️ Downloading restricted…")
        path = await client.download_media(msg, file_name=os.path.join(tmpdir, "file"))
        if not path or not os.path.exists(path):
            return False, "download failed"

        await _status("⬆️ Uploading…")
        try:
            await _send_file(
                uploader, dest_chat, path, msg,
                caption=caption,
                reply_markup=reply_markup,
                protect_content=protect_content,
                message_thread_id=topic_id,
            )
            return True, "downloaded"
        except Exception as e:
            # fallback: upload with reader client
            try:
                await _send_file(
                    client, dest_chat, path, msg,
                    caption=caption,
                    reply_markup=reply_markup,
                    protect_content=protect_content,
                    message_thread_id=topic_id,
                )
                return True, "userbot-upload"
            except Exception as e2:
                return False, f"{e} | {e2}"
    except FloodWait as e:
        await asyncio.sleep(e.value)
        return False, f"FloodWait {e.value}s"
    except Exception as e:
        return False, str(e)
    finally:
        try:
            if path and os.path.exists(path):
                os.remove(path)
            if os.path.isdir(tmpdir):
                for f in os.listdir(tmpdir):
                    try:
                        os.remove(os.path.join(tmpdir, f))
                    except Exception:
                        pass
                try:
                    os.rmdir(tmpdir)
                except Exception:
                    pass
        except Exception:
            pass
