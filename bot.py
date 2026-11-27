import asyncio
import logging
import logging.config
from database import db 
from config import Config  
from pyrogram import Client, __version__
from pyrogram.raw.all import layer 
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait 
from pyrogram.types import BotCommand

# Setup logging (need logging.conf file or it fall back)
try:
    logging.config.fileConfig('logging.conf')
except Exception:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

logging.getLogger().setLevel(logging.INFO)
logging.getLogger("pyrogram").setLevel(logging.ERROR)
logging.getLogger("pyrogram.session.session").setLevel(logging.WARNING)
logging.getLogger("pyrogram.connection").setLevel(logging.WARNING)


class Bot(Client): 
    def __init__(self):
        super().__init__(
            name=Config.BOT_SESSION,
            api_hash=Config.API_HASH,
            api_id=Config.API_ID,
            plugins={
                "root": "plugins"
            },
            workers=50,
            bot_token=Config.BOT_TOKEN
        )
        self.log = logging

    async def start(self):
        await super().start()
        me = await self.get_me()
        
        logging.info(
            f"{me.first_name} with pyrogram v{__version__} (Layer {layer}) "
            f"started on @{me.username}."
        )
        
        self.id = me.id
        self.username = me.username
        self.first_name = me.first_name
        self.set_parse_mode(ParseMode.DEFAULT)
        
        # ── SINGULAR BOT COMMANDS (clean menu in Telegram) ──
        try:
            await self.set_bot_commands([
                BotCommand("start", "Main menu"),
                BotCommand("menu", "Main menu"),
                BotCommand("help", "Help"),
                BotCommand("tasks", "Manage running tasks + filters"),
                BotCommand("status", "All jobs: batch · live · schedule"),
                BotCommand("forward", "Multi source → multi target"),
                BotCommand("fwd", "Multi source → multi target"),
                BotCommand("live", "Live mirror"),
                BotCommand("lives", "List live watchers"),
                BotCommand("stoplive", "Stop live watchers"),
                BotCommand("schedule", "Create schedule"),
                BotCommand("listschedule", "List schedules"),
                BotCommand("cancelschedule", "Cancel schedule"),
                BotCommand("save", "Save restricted post"),
                BotCommand("batchsave", "Batch save restricted"),
                BotCommand("addconn", "Save connection"),
                BotCommand("listconn", "List connections"),
                BotCommand("useconn", "Use connection"),
                BotCommand("settings", "Settings"),
                BotCommand("unequify", "Remove duplicates"),
                BotCommand("cancel", "Cancel batch jobs"),
            ])
            logging.info("Bot command menu set")
        except Exception as e:
            logging.warning(f"Could not set bot commands: {e}")

        text = "**๏[-ิ_•ิ]๏ bot restarted !**"
        logging.info(text)

        # Check if database URI is default broken one
        if not Config.DATABASE_URI or "mongodb+srv://chhjgjkkjhkjhkjh@cluster0.xowzpr4.mongodb.net/" in Config.DATABASE_URI:
            logging.error("You have not set the DATABASE environment variable correctly. Bot may not function.")
            # Continue anyway so web server can start

        # Send restart message to users who had pending forwards
        try:
            success = failed = 0
            users = await db.get_all_frwd()
            async for user in users:
                chat_id = user['user_id']
                try:
                    await self.send_message(chat_id, text)
                    success += 1
                except FloodWait as e:
                    await asyncio.sleep(e.value + 1)
                    try:
                        await self.send_message(chat_id, text)
                        success += 1
                    except Exception:
                        failed += 1
                except Exception:
                    failed += 1

            if (success + failed) != 0:
                await db.rmve_frwd(all=True)
                logging.info(f"Restart message status → success: {success} | failed: {failed}")
        except Exception as e:
            logging.error(f"Failed to send restart messages or connect to DB: {e}")

    async def stop(self, *args):
        msg = f"@{self.username} stopped. Bye."
        await super().stop()
        logging.info(msg)


# For direct run (if not using main.py)
if __name__ == "__main__":
    Bot().run()