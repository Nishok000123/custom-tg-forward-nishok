import time as tm
import re
import asyncio
from typing import Optional, Tuple, Union
from database import db 
from .test import parse_buttons
from pyrogram import Client
from pyrogram.types import Message, Chat
from pyrogram.errors import (
    UsernameNotOccupied, UsernameInvalid, PeerIdInvalid,
    ChannelPrivate, ChannelInvalid, InviteHashExpired,
    InviteHashInvalid, UserAlreadyParticipant, FloodWait
)

STATUS = {}

class STS:
    def __init__(self, id):
        self.id = id
        self.data = STATUS
    
    def verify(self):
        return self.data.get(self.id)
    
    def store(
        self, From, to, skip, limit, continuous=False, to_topic=None,
        force_download=False, from_list=None, to_list=None,
        task_filters=None,
    ):
        self.data[self.id] = {
            "FROM": From, 'TO': to, 'TO_TOPIC': to_topic,
            'TASK_FILTERS': task_filters,
            'force_download': force_download,
            'FROM_LIST': from_list,
            'TO_LIST': to_list,
            'total_files': 0, 'skip': skip, 'limit': limit,
            'fetched': skip, 'filtered': 0, 'deleted': 0, 'duplicate': 0,
            'total': limit, 'start': 0, 'continuous': continuous,
        }
        self.get(full=True)
        return STS(self.id)
        
    def get(self, value=None, full=False):
        values = self.data.get(self.id)
        if not full:
           return values.get(value)
        for k, v in values.items():
            setattr(self, k, v)
        return self

    def add(self, key=None, value=1, time=False):
        if time:
          return self.data[self.id].update({'start': tm.time()})
        self.data[self.id].update({key: self.get(key) + value}) 
    
    def divide(self, no, by):
       by = 1 if int(by) == 0 else by 
       return int(no) / by 
    
    async def get_data(self, user_id):
        bot = await db.get_bot(user_id)
        k, filters = self, await db.get_filters(user_id)
        size, configs = None, await db.get_configs(user_id)
        if configs['duplicate']:
           duplicate = [configs['db_uri'], self.TO]
        else:
           duplicate = False
        button = parse_buttons(configs['button'] if configs['button'] else '')
        if configs['file_size'] != 0:
            size = [configs['file_size'], configs['size_limit']]
        return bot, configs['caption'], configs['forward_tag'], {'chat_id': k.FROM, 'limit': k.limit, 'offset': k.skip, 'filters': filters,
                'keywords': configs['keywords'], 'media_size': size, 'extensions': configs['extension'], 'skip_duplicate': duplicate}, configs['protect'], button


# ── Smart Chat Resolver ───────────────────────────────────────────────────────

_TG_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.(?:me|dog))/(?:c/)?(?:joinchat/|\+)?([a-zA-Z0-9_\-]+)/?(?:(\d+))?",
    re.IGNORECASE
)
_USERNAME_RE = re.compile(r"^@?([a-zA-Z][\w\d]{3,31})$")
_NUMERIC_ID_RE = re.compile(r"^-?\d+$")


async def resolve_chat(
    client: Client,
    query: Union[str, int, Message],
    *,
    join_if_invite: bool = False,
) -> Tuple[Optional[Chat], Optional[str]]:
    """
    Resolve chat from: ID, @username, t.me link, invite link, forward, or chat share.
    Returns: (Chat | None, error_message | None)
    """
    try:
        if isinstance(query, Message):
            msg = query
            if msg.forward_from_chat:
                return msg.forward_from_chat, None
            if msg.forward_from:
                return None, "This is a forward from a user, not a channel/group."
            if getattr(msg, "chat_shared", None):
                try:
                    chat = await client.get_chat(msg.chat_shared.chat_id)
                    return chat, None
                except Exception as e:
                    return None, f"Could not resolve shared chat: {e}"
            if msg.text:
                return await resolve_chat(client, msg.text.strip(), join_if_invite=join_if_invite)
            return None, "Send a username, link, chat ID, or forward a message from the chat."

        if isinstance(query, int) or (isinstance(query, str) and _NUMERIC_ID_RE.match(str(query).strip())):
            chat_id = int(query)
            try:
                chat = await client.get_chat(chat_id)
                return chat, None
            except (PeerIdInvalid, ChannelPrivate, ChannelInvalid, ValueError) as e:
                return None, (
                    f"Cannot access chat <code>{chat_id}</code>.\n"
                    f"• For private/restricted sources use a <b>userbot</b> that is a member.\n"
                    f"• Make sure the bot/userbot has joined the chat.\n"
                    f"Error: <code>{e}</code>"
                )
            except FloodWait as e:
                await asyncio.sleep(e.value)
                return await resolve_chat(client, chat_id, join_if_invite=join_if_invite)
            except Exception as e:
                return None, f"Failed to resolve ID <code>{chat_id}</code>: <code>{e}</code>"

        text = str(query).strip()

        m = _USERNAME_RE.match(text)
        if m:
            username = m.group(1)
            try:
                chat = await client.get_chat(username)
                return chat, None
            except (UsernameNotOccupied, UsernameInvalid):
                return None, f"Username @{username} not found."
            except (ChannelPrivate, PeerIdInvalid):
                return None, (
                    f"@{username} is private/restricted. "
                    f"Join it with your userbot first, then try again (or use numeric ID)."
                )
            except FloodWait as e:
                await asyncio.sleep(e.value)
                return await resolve_chat(client, text, join_if_invite=join_if_invite)
            except Exception as e:
                return None, f"Failed to resolve @{username}: <code>{e}</code>"

        link = _TG_LINK_RE.search(text)
        if link:
            part = link.group(1)

            if "/c/" in text.lower() and part.isdigit():
                chat_id = int(f"-100{part}")
                return await resolve_chat(client, chat_id, join_if_invite=join_if_invite)

            if "joinchat/" in text.lower() or "/+" in text or text.strip().startswith("+"):
                invite_hash = part
                try:
                    if join_if_invite:
                        try:
                            inv = text if text.startswith("http") else f"https://t.me/+{invite_hash}"
                            await client.join_chat(inv)
                        except UserAlreadyParticipant:
                            pass
                        except (InviteHashExpired, InviteHashInvalid) as e:
                            return None, f"Invite link invalid/expired: <code>{e}</code>"
                        except Exception:
                            pass
                    inv = text if text.startswith("http") else f"https://t.me/+{invite_hash}"
                    chat = await client.get_chat(inv)
                    return chat, None
                except Exception as e:
                    return None, (
                        f"Could not resolve invite link.\n"
                        f"Join the chat first with the userbot, then use @username or ID.\n"
                        f"<code>{e}</code>"
                    )

            try:
                chat = await client.get_chat(part)
                return chat, None
            except Exception as e:
                return None, f"Could not resolve link for <code>{part}</code>: <code>{e}</code>"

        return None, (
            "Could not understand that chat.\n\n"
            "Send one of:\n"
            "• <code>@channel</code> or <code>username</code>\n"
            "• <code>-1001234567890</code> (numeric ID)\n"
            "• <code>https://t.me/channel</code>\n"
            "• Forward any message from the chat\n"
            "• Invite link (userbot must be able to join)"
        )

    except FloodWait as e:
        await asyncio.sleep(e.value)
        return await resolve_chat(client, query, join_if_invite=join_if_invite)
    except Exception as e:
        return None, f"Unexpected error resolving chat: <code>{e}</code>"


def format_chat_label(chat: Chat = None, chat_id: int = None, title: str = None, username: str = None) -> str:
    if chat is not None:
        title = chat.title or getattr(chat, "first_name", None) or str(chat.id)
        username = chat.username
        chat_id = chat.id
    title = title or str(chat_id)
    uname = f" (@{username})" if username else ""
    return f"<b>{title}</b>{uname} · <code>{chat_id}</code>"


async def get_client_for_user(user_id: int, conn_name: str = None):
    """
    Start the user's bot/userbot client.
    Returns (client, bot_data) or (None, error_str)
    """
    from .test import CLIENT, start_clone_bot

    CLIENT_HELPER = CLIENT()
    if conn_name:
        conn = await db.get_connection(user_id, conn_name)
        if not conn:
            return None, f"Connection <code>{conn_name}</code> not found. Use /listconn."
        bot_data = conn["bot_data"]
        bot_data["user_id"] = user_id
    else:
        bot_data = await db.get_bot(user_id)
    if not bot_data:
        return None, "No bot/session configured. Use /settings first."

    try:
        client = await start_clone_bot(CLIENT_HELPER.client(bot_data))
        return client, bot_data
    except Exception as e:
        return None, f"Failed to start client: <code>{e}</code>"