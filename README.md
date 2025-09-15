# Niekulturysta22 – Telegram Bot (FastAPI + aiogram + OpenAI + SQLite)

MVP stack:
- **aiogram 3** (bot framework)
- **FastAPI** (webhook + health)
- **SQLite** (async) → later Postgres
- **OpenAI** (Responses via Chat Completions; optional local File Search/FTS)
- Minimal scheduler (background task) for reminders

## Quick start (dev – polling)
```bash
python -m venv .venv && . .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env  # fill BOT_TOKEN + OPENAI_API_KEY
python manage.py polling
```

## Webhook mode (prod-like)
- Public HTTPS URL required (e.g., domain or `ngrok http 8000`)
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
# Set webhook once:
python manage.py set-webhook
```
This sets Telegram webhook to: `${WEBHOOK_URL}/webhook/${WEBHOOK_SECRET}`.

## Ingest your knowledge base
Put your **ebook** and **recipes** into `data/` then run:
```bash
python scripts/ingest.py
```
This creates/updates a local FTS index in SQLite so the bot can retrieve snippets.

## Commands MVP
- `/start` – onboarding mini-interview, saves profile
- `/plan` – quick 3-step plan + makra/przepis + tip (uses KB)
- `/checkin` – zapis wagi/uwag
- `/raport` – tygodniowe podsumowanie
- `/powiadomienia` – ustaw/wyłącz przypomnienia

## Switch to Postgres later
Change `DATABASE_URL` in `.env`, e.g. `postgresql+asyncpg://user:pass@host/db`.
Run `scripts/ingest.py` again to rebuild tables if needed.
