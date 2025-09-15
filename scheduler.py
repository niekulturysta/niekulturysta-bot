import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from db import Session, Reminder
from aiogram import Bot

async def scheduler_loop(bot: Bot):
    while True:
        try:
            now = datetime.now(timezone.utc)
            async with Session() as s:
                q = await s.execute(select(Reminder).where(Reminder.active == True, Reminder.next_run_at <= now))
                due = q.scalars().all()
                for r in due:
                    try:
                        await bot.send_message(r.chat_id, r.text)
                    except Exception:
                        pass
                    # schedule next day at same time
                    r.next_run_at = r.next_run_at + timedelta(days=1)
                if due:
                    await s.commit()
        except Exception:
            pass
        await asyncio.sleep(15)  # tick every 15s
