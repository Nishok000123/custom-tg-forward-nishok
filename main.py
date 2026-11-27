import asyncio
import os
import logging
import aiohttp
from aiohttp import web
from pyrogram import idle
from bot import Bot
from plugins.regix import restore_live_watchers
from plugins.connections import restore_schedules

# Dashboard + public control API OFF by default (HF ban risk: public web UI).
# Set ENABLE_DASHBOARD=1 only if you need the web UI again (not recommended).
ENABLE_DASHBOARD = os.environ.get("ENABLE_DASHBOARD", "").strip().lower() in (
    "1", "true", "yes", "on"
)


# ── Minimal health only (keeps HF Space port alive) ───────────────────────────
async def handle_health(request):
    """No HTML, no bot name, no stats — just prove the process is up."""
    return web.Response(text="ok", content_type="text/plain")


async def handle_gone(request):
    return web.Response(text="Not Found", content_type="text/plain", status=404)


# ── Optional dashboard (disabled unless ENABLE_DASHBOARD=1) ───────────────────
def _build_dashboard_routes():
    """Lazy-import full API only when explicitly enabled."""
    import json
    import time
    from config import temp
    from database import db
    from plugins.connections import SCHEDULE_TASKS, _cancel_schedule

    START_TIME = time.time()
    API_TOKEN = os.environ.get("API_TOKEN", "")

    def get_uptime():
        elapsed = time.time() - START_TIME
        days, rem = divmod(elapsed, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        return f"{int(days)}d {int(hours)}h {int(minutes)}m {int(seconds)}s"

    def check_auth(request):
        # When dashboard enabled, require a strong token; if missing/short, refuse all API
        import hmac
        if not API_TOKEN or len(API_TOKEN) < 32:
            return False
        token = request.headers.get("X-API-Token", "")
        return hmac.compare_digest(token.encode(), API_TOKEN.encode())

    def unauthorized():
        return web.Response(
            text=json.dumps({"error": "Unauthorized"}),
            content_type="application/json",
            status=401,
        )

    def json_response(data, status=200):
        return web.Response(
            text=json.dumps(data),
            content_type="application/json",
            status=status,
        )

    async def handle_root(request):
        dashboard_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
        if os.path.exists(dashboard_path):
            with open(dashboard_path, "r", encoding="utf-8") as f:
                return web.Response(text=f.read(), content_type="text/html")
        return web.Response(text="ok", content_type="text/plain")

    async def api_status(request):
        if not check_auth(request):
            return unauthorized()
        return json_response({
            "status": "online",
            "uptime": get_uptime(),
            "live_watchers": len(temp.LIVE_WATCHERS),
            "batch_forwards": sum(len(v) for v in temp.lock.values()),
            "schedules": len(SCHEDULE_TASKS),
        })

    async def api_live_list(request):
        if not check_auth(request):
            return unauthorized()
        watchers = []
        for (from_id, to_id), info in temp.LIVE_WATCHERS.items():
            watchers.append({
                "from_id": from_id,
                "to_id": to_id,
                "user_id": info.get("user_id"),
                "conn_name": info.get("conn_name", "default"),
                "status": "running",
            })
        return json_response({"watchers": watchers})

    async def api_live_stop(request):
        if not check_auth(request):
            return unauthorized()
        try:
            body = await request.json()
            from_id = int(body["from_id"])
            to_id = int(body["to_id"])
        except Exception:
            return json_response({"error": "Invalid body"}, 400)
        key = (from_id, to_id)
        if key not in temp.LIVE_WATCHERS:
            return json_response({"error": "Watcher not found"}, 404)
        user_id = temp.LIVE_WATCHERS[key].get("user_id")
        temp.LIVE_WATCHERS.pop(key, None)
        await db.remove_live_watcher(user_id, from_id, to_id)
        return json_response({"ok": True})

    async def api_live_start(request):
        if not check_auth(request):
            return unauthorized()
        try:
            body = await request.json()
            from_id = int(body["from_id"])
            to_id = int(body["to_id"])
            conn_name = body.get("conn_name") or None
            user_id = int(body["user_id"])
        except Exception:
            return json_response({"error": "Invalid body"}, 400)
        from plugins.regix import _start_live_watcher
        ok, reason = await _start_live_watcher(
            request.app["bot"], user_id, from_id, to_id, conn_name=conn_name
        )
        if not ok:
            return json_response({"error": reason}, 400)
        return json_response({"ok": True})

    async def api_sched_list(request):
        if not check_auth(request):
            return unauthorized()
        scheds = []
        cursor = await db.get_all_schedules()
        async for s in cursor:
            scheds.append({
                "sched_id": s["sched_id"],
                "from_id": s["from_id"],
                "to_id": s["to_id"],
                "to_topic": s.get("to_topic"),
                "force_download": bool(s.get("force_download")),
                "from_title": s.get("from_title") or str(s["from_id"]),
                "to_title": s.get("to_title") or str(s["to_id"]),
                "interval_label": s.get("interval_label"),
                "last_forwarded_id": s.get("last_forwarded_id", s.get("from_msg", 1) - 1),
                "running": s["sched_id"] in SCHEDULE_TASKS,
            })
        return json_response({"schedules": scheds})

    async def api_sched_cancel(request):
        if not check_auth(request):
            return unauthorized()
        try:
            body = await request.json()
            sched_id = body["sched_id"]
        except Exception:
            return json_response({"error": "Need sched_id"}, 400)
        await _cancel_schedule(sched_id)
        return json_response({"ok": True})

    async def api_conn_list(request):
        if not check_auth(request):
            return unauthorized()
        user_id = request.rel_url.query.get("user_id")
        if not user_id:
            return json_response({"error": "Need user_id"}, 400)
        cursor = await db.get_all_connections(int(user_id))
        conns = []
        async for c in cursor:
            bd = c.get("bot_data", {})
            conns.append({
                "name": c["name"],
                "conn_type": c.get("conn_type", "all"),
                "username": bd.get("username", "unknown"),
                "is_bot": bd.get("is_bot", False),
            })
        return json_response({"connections": conns})

    async def api_conn_delete(request):
        if not check_auth(request):
            return unauthorized()
        try:
            body = await request.json()
            user_id = int(body["user_id"])
            name = body["name"]
        except Exception:
            return json_response({"error": "Need user_id and name"}, 400)
        await db.delete_connection(user_id, name)
        return json_response({"ok": True})

    async def api_batch_list(request):
        if not check_auth(request):
            return unauthorized()
        from plugins.utils import STS
        batches = []
        for user_id, fids in temp.lock.items():
            for fid in fids:
                if temp.CANCEL.get(fid):
                    continue
                sts = STS(fid)
                if sts.verify():
                    i = sts.get(full=True)
                    total = i.total if i.total else 1
                    pct = int((i.fetched or 0) * 100 / total)
                    batches.append({
                        "frwd_id": fid,
                        "user_id": user_id,
                        "fetched": i.fetched or 0,
                        "forwarded": i.total_files or 0,
                        "deleted": i.deleted or 0,
                        "total": total,
                        "percent": pct,
                    })
        return json_response({"batches": batches})

    async def api_batch_cancel(request):
        if not check_auth(request):
            return unauthorized()
        try:
            body = await request.json()
            frwd_id = body.get("frwd_id")
            user_id = body.get("user_id")
        except Exception:
            return json_response({"error": "Need frwd_id or user_id"}, 400)
        if frwd_id:
            temp.CANCEL[frwd_id] = True
            return json_response({"ok": True})
        if user_id:
            for fid in temp.lock.get(int(user_id), []):
                temp.CANCEL[fid] = True
            return json_response({"ok": True})
        return json_response({"error": "Need frwd_id or user_id"}, 400)

    return [
        web.get("/", handle_root),
        web.get("/api/status", api_status),
        web.get("/api/live", api_live_list),
        web.post("/api/live", api_live_start),
        web.delete("/api/live", api_live_stop),
        web.get("/api/schedules", api_sched_list),
        web.delete("/api/schedules", api_sched_cancel),
        web.get("/api/connections", api_conn_list),
        web.delete("/api/connections", api_conn_delete),
        web.get("/api/batch", api_batch_list),
        web.post("/api/batch/cancel", api_batch_cancel),
    ]


async def web_server(bot):
    app = web.Application()
    app["bot"] = bot

    if ENABLE_DASHBOARD:
        logging.warning(
            "ENABLE_DASHBOARD=1 — public web UI on. Set API_TOKEN secret. Ban risk if exposed."
        )
        app.add_routes(_build_dashboard_routes())
    else:
        # Health only — no dashboard, no control API, no bot stats on web
        app.add_routes([
            web.get("/", handle_health),
            web.get("/health", handle_health),
            web.get("/api/{tail:.*}", handle_gone),
            web.route("*", "/{tail:.*}", handle_gone),
        ])
        logging.info("Dashboard DISABLED (default). Telegram bot only. Web = health check.")

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 7860))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Web server on port {port} (dashboard={'ON' if ENABLE_DASHBOARD else 'OFF'})")


async def ping_server():
    while True:
        await asyncio.sleep(300)
        try:
            port = int(os.environ.get("PORT", 7860))
            url = f"http://127.0.0.1:{port}/health"
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    logging.info(f"Self-ping {url}: {resp.status}")
        except Exception as e:
            logging.error(f"Self-ping failed: {e}")


async def main():
    bot = Bot()
    await bot.start()
    try:
        from database import db as _db
        n = await _db.migrate_secrets()
        if n:
            logging.info(f"Encrypted/re-keyed {n} stored session/token record(s)")
    except Exception as e:
        logging.error(f"Secret migration failed: {e}")
    await restore_live_watchers(bot)
    await restore_schedules(bot)
    await web_server(bot)
    asyncio.create_task(ping_server())
    await idle()
    await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
