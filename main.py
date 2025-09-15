import asyncio
from fastapi import FastAPI, Request
from settings import settings
from aiogram import Bot, Dispatcher, types
from bot.handlers import router as main_router
from db import ensure_schema
from scheduler import scheduler_loop

app = FastAPI(title="Niekulturysta22 Bot")

# ⬇️ zmiana: usunięty parse_mode="HTML"
bot = Bot(settings.bot_token)
dp = Dispatcher()
dp.include_router(main_router)

@app.on_event("startup")
async def on_startup():
    await ensure_schema()
    # Set webhook (idempotent)
    if settings.webhook_url:
        url = settings.webhook_url.rstrip('/') + f"/webhook/{settings.webhook_secret}"
        try:
            await bot.set_webhook(url)
        except Exception as e:
            print("Webhook set failed:", e)
    # Fire scheduler background task
    asyncio.create_task(scheduler_loop(bot))

@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request):
    if secret != settings.webhook_secret:
        return {"ok": False}
    data = await request.json()
    update = types.Update(**data)
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/healthz")
async def healthz():
    me = await bot.get_me()
    return {"ok": True, "bot": me.username}
