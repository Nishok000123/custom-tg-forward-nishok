from os import environ 
from config import Config
import motor.motor_asyncio
from pymongo import MongoClient

async def mongodb_version():
    x = MongoClient(Config.DATABASE_URI)
    mongodb_version = x.server_info()['version']
    return mongodb_version

class Database:
    
    def __init__(self, uri, database_name):
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self._client[database_name]
        self.bot = self.db.bots
        self.col = self.db.users
        self.nfy = self.db.notify
        self.chl = self.db.channels 
        self.live = self.db.live_watchers
        self.conn = self.db.connections
        self.sched = self.db.schedules
        
    def new_user(self, id, name):
        return dict(
            id = id,
            name = name,
            ban_status=dict(
                is_banned=False,
                ban_reason="",
            ),
        )
      
    async def add_user(self, id, name):
        user = self.new_user(id, name)
        await self.col.insert_one(user)
    
    async def is_user_exist(self, id):
        user = await self.col.find_one({'id':int(id)})
        return bool(user)
    
    async def total_users_bots_count(self):
        bcount = await self.bot.count_documents({})
        count = await self.col.count_documents({})
        return count, bcount

    async def total_channels(self):
        count = await self.chl.count_documents({})
        return count
    
    async def remove_ban(self, id):
        ban_status = dict(
            is_banned=False,
            ban_reason=''
        )
        await self.col.update_one({'id': id}, {'$set': {'ban_status': ban_status}})
    
    async def ban_user(self, user_id, ban_reason="No Reason"):
        ban_status = dict(
            is_banned=True,
            ban_reason=ban_reason
        )
        await self.col.update_one({'id': user_id}, {'$set': {'ban_status': ban_status}})

    async def get_ban_status(self, id):
        default = dict(
            is_banned=False,
            ban_reason=''
        )
        user = await self.col.find_one({'id':int(id)})
        if not user:
            return default
        return user.get('ban_status', default)

    async def get_all_users(self):
        return self.col.find({})
    
    async def delete_user(self, user_id):
        await self.col.delete_many({'id': int(user_id)})
 
    async def get_banned(self):
        users = self.col.find({'ban_status.is_banned': True})
        b_users = [user['id'] async for user in users]
        return b_users

    async def update_configs(self, id, configs):
        await self.col.update_one({'id': int(id)}, {'$set': {'configs': configs}})
         
    async def get_configs(self, id):
        default = {
            'caption': None,
            'duplicate': True,
            'forward_tag': False,
            'file_size': 0,
            'size_limit': None,
            'extension': None,
            'keywords': None,
            'protect': None,
            'button': None,
            'db_uri': None,
            'filters': {
               'poll': True,
               'text': True,
               'audio': True,
               'voice': True,
               'video': True,
               'photo': True,
               'document': True,
               'animation': True,
               'sticker': True
            }
        }
        user = await self.col.find_one({'id':int(id)})
        if user:
            # merge with defaults so configs saved by older versions never miss keys
            stored = user.get('configs') or {}
            merged = {**default, **stored}
            merged['filters'] = {**default['filters'], **(stored.get('filters') or {})}
            return merged
        return default 
       
    async def add_bot(self, datas):
       if not await self.is_bot_exist(datas['user_id']):
          await self.bot.insert_one(datas)
    
    async def remove_bot(self, user_id):
       await self.bot.delete_many({'user_id': int(user_id)})
      
    async def get_bot(self, user_id: int):
       bot = await self.bot.find_one({'user_id': user_id})
       return bot if bot else None
                                          
    async def is_bot_exist(self, user_id):
       bot = await self.bot.find_one({'user_id': user_id})
       return bool(bot)
                                          
    async def in_channel(self, user_id: int, chat_id: int) -> bool:
       channel = await self.chl.find_one({"user_id": int(user_id), "chat_id": int(chat_id)})
       return bool(channel)
    
    async def add_channel(self, user_id: int, chat_id: int, title, username, topic_id: int = None):
       channel = await self.in_channel(user_id, chat_id)
       if channel:
         # allow updating topic on existing channel
         if topic_id is not None:
            await self.chl.update_one(
               {"user_id": int(user_id), "chat_id": int(chat_id)},
               {"$set": {"topic_id": topic_id, "title": title, "username": username}}
            )
            return True
         return False
       return await self.chl.insert_one({
          "user_id": user_id, "chat_id": chat_id, "title": title,
          "username": username, "topic_id": topic_id
       })
    
    async def remove_channel(self, user_id: int, chat_id: int):
       channel = await self.in_channel(user_id, chat_id )
       if not channel:
         return False
       return await self.chl.delete_many({"user_id": int(user_id), "chat_id": int(chat_id)})
    
    async def get_channel_details(self, user_id: int, chat_id: int):
       return await self.chl.find_one({"user_id": int(user_id), "chat_id": int(chat_id)})
       
    async def get_user_channels(self, user_id: int):
       channels = self.chl.find({"user_id": int(user_id)})
       return [channel async for channel in channels]
     
    async def get_filters(self, user_id):
       filters = []
       filter = (await self.get_configs(user_id))['filters']
       for k, v in filter.items():
          if v == False:
            filters.append(str(k))
       return filters
              
    async def add_frwd(self, user_id):
       return await self.nfy.insert_one({'user_id': int(user_id)})
    
    async def rmve_frwd(self, user_id=0, all=False):
       data = {} if all else {'user_id': int(user_id)}
       return await self.nfy.delete_many(data)
    
    async def get_all_frwd(self):
       return self.nfy.find({})
     
    # ── Named Connections ──────────────────────────────────────────────────────
    async def save_connection(self, user_id: int, name: str, bot_data: dict, conn_type: str = "all"):
        await self.conn.update_one(
            {"user_id": user_id, "name": name},
            {"$set": {"user_id": user_id, "name": name, "bot_data": bot_data, "conn_type": conn_type}},
            upsert=True
        )

    async def get_connection(self, user_id: int, name: str):
        return await self.conn.find_one({"user_id": user_id, "name": name})

    async def get_all_connections(self, user_id: int):
        return self.conn.find({"user_id": user_id})

    async def delete_connection(self, user_id: int, name: str):
        await self.conn.delete_many({"user_id": user_id, "name": name})

    # ── Schedules ──────────────────────────────────────────────────────────────
    async def save_schedule(self, user_id: int, sched_id: str, data: dict):
        await self.sched.update_one(
            {"user_id": user_id, "sched_id": sched_id},
            {"$set": {**data, "user_id": user_id, "sched_id": sched_id}},
            upsert=True
        )

    async def get_all_schedules(self, user_id: int = None):
        query = {"user_id": user_id} if user_id else {}
        return self.sched.find(query)

    async def delete_schedule(self, sched_id: str):
        await self.sched.delete_many({"sched_id": sched_id})

    # ── Live watchers (with titles) ────────────────────────────────────────────
    async def save_live_watcher(
        self,
        user_id: int,
        from_id: int,
        to_id: int,
        conn_name: str = None,
        from_title: str = None,
        to_title: str = None,
        from_username: str = None,
        to_username: str = None,
        to_topic: int = None,
        force_download: bool = False,
        task_filters: dict = None,
    ):
        key = {"user_id": user_id, "from_id": from_id, "to_id": to_id}
        data = {
            **key,
            "conn_name": conn_name,
            "from_title": from_title or str(from_id),
            "to_title": to_title or str(to_id),
            "from_username": from_username,
            "to_username": to_username,
            "to_topic": to_topic,
            "force_download": force_download,
        }
        if task_filters is not None:
            data["task_filters"] = task_filters
        await self.live.update_one(key, {"$set": data}, upsert=True)

    async def remove_live_watcher(self, user_id: int, from_id: int, to_id: int):
        await self.live.delete_many({"user_id": user_id, "from_id": from_id, "to_id": to_id})

    async def get_all_live_watchers(self, user_id: int = None):
        query = {"user_id": user_id} if user_id is not None else {}
        return self.live.find(query)

    async def get_live_watcher(self, user_id: int, from_id: int, to_id: int):
        return await self.live.find_one({"user_id": user_id, "from_id": from_id, "to_id": to_id})

db = Database(Config.DATABASE_URI, Config.DATABASE_NAME)