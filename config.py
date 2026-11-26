from os import environ 

class Config:
    API_ID = environ.get("API_ID")
    API_HASH = environ.get("API_HASH")
    BOT_TOKEN = environ.get("BOT_TOKEN") 
    BOT_SESSION = environ.get("BOT_SESSION", "bot") 
    DATABASE_URI = environ.get("DATABASE","")
    DATABASE_NAME = environ.get("DATABASE_NAME", "forward-bot")
    BOT_OWNER_ID = [int(id) for id in environ.get("BOT_OWNER_ID", '').split()]

class temp(object): 
    lock = {}         # user_id -> list of active forward_ids (multi-task)
    CANCEL = {}       # forward_id -> bool
    forwardings = 0
    BANNED_USERS = []
    IS_FRWD_CHAT = []
    LIVE_WATCHERS = {}  # (from_chat_id, to_chat_id) -> client instance

