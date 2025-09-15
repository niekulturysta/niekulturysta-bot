import asyncio
import sys
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

from settings import settings
from bot.handlers import router as main_router
from db import ensure_schema

async def run_polling():
    # logi do diagnozy
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

    # za każdym razem tworzymy czystą bazę
    await ensure_schema()

    # ⬇️ zmiana: usunięty parse_mode="HTML"
    bot = Bot(settings.bot_token)

    # usuń webhook (inaczej polling nie dostanie update’ów)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logging.info("Webhook deleted (drop_pending_updates=True).")
    except Exception as e:
        logging.warning(f"delete_webhook error: {e}")

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(main_router)

    logging.info("Starting polling…")
    await dp.start_polling(bot)


async def set_webhook():
    if not settings.webhook_url:
        print("WEBHOOK_URL is empty in .env — set it first.")
        return

    bot = Bot(settings.bot_token)
    url = settings.webhook_url.rstrip('/') + f"/webhook/{settings.webhook_secret}"

    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    await bot.set_webhook(url)
    print("Webhook set to:", url)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python manage.py [polling|set-webhook]")
        sys.exit(1)

    cmd = sys.argv[1].lower()
    if cmd == "polling":
        asyncio.run(run_polling())
    elif cmd == "set-webhook":
        asyncio.run(set_webhook())
    else:
        print("Unknown command:", cmd)
