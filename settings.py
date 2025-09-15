from pydantic import BaseModel
from dotenv import load_dotenv
import os

load_dotenv(override=True)

class Settings(BaseModel):
    bot_token: str = os.getenv("BOT_TOKEN", "")
    webhook_url: str = os.getenv("WEBHOOK_URL", "")
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "secret-path")
    database_url: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./app.db")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    owner_tg_id: int = int(os.getenv("OWNER_TG_ID", "0"))

settings = Settings()
