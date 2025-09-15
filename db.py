from typing import Optional, List
from datetime import datetime

from sqlalchemy import (
    Integer, String, Text, ForeignKey, DateTime, Boolean, Float, func, JSON, text
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine

from settings import settings


class Base(AsyncAttrs, DeclarativeBase):
    pass


engine = create_async_engine(settings.database_url, echo=False, future=True)
Session = async_sessionmaker(engine, expire_on_commit=False)


# ========== MODELE ==========
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(64))
    first_name: Mapped[Optional[str]] = mapped_column(String(64))
    last_name: Mapped[Optional[str]] = mapped_column(String(64))
    profile: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    checkins: Mapped[List["Checkin"]] = relationship(
        back_populates="user", cascade="all,delete-orphan"
    )
    reminders: Mapped[List["Reminder"]] = relationship(
        back_populates="user", cascade="all,delete-orphan"
    )


class Checkin(Base):
    __tablename__ = "checkins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    weight_kg: Mapped[Optional[float]] = mapped_column(Float)
    note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="checkins")


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    chat_id: Mapped[int] = mapped_column(index=True)
    text: Mapped[str] = mapped_column(Text)
    cron: Mapped[Optional[str]] = mapped_column(String(64))  # e.g. "0 9 * * *"
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # <<< TO JEST KLUCZOWE: relacja zwrotna istnieje i nazywa się 'user' >>>
    user: Mapped["User"] = relationship(back_populates="reminders")


class Doc(Base):
    __tablename__ = "docs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))  # 'ebook' | 'recipe' | 'faq'
    title: Mapped[str] = mapped_column(String(256))
    meta: Mapped[Optional[dict]] = mapped_column(JSON)
    content: Mapped[str] = mapped_column(Text)


# ========== SCHEMAT / FTS ==========
async def ensure_schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Tylko dla SQLite – tabela pełnotekstowa FTS
        if settings.database_url.startswith("sqlite"):
            await conn.exec_driver_sql(
                "CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(title, content, content_rowid='id');"
            )
            # wypełnij FTS tym, czego jeszcze nie ma
            await conn.exec_driver_sql(
                """
                INSERT INTO docs_fts(rowid, title, content)
                SELECT d.id, d.title, d.content
                FROM docs d
                WHERE NOT EXISTS (SELECT 1 FROM docs_fts f WHERE f.rowid = d.id);
                """
            )
