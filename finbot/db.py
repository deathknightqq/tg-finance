"""Подключение к базе. SQLite сейчас, Postgres потом — заменой DATABASE_URL."""

from __future__ import annotations

import os

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from finbot.models import SEED_CATEGORIES, Base, Category

DEFAULT_URL = "sqlite:///finbot.db"


def make_engine(url: str | None = None):
    return create_engine(url or os.getenv("DATABASE_URL", DEFAULT_URL))


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def init_db(engine) -> None:
    """Создаёт таблицы и досеивает сид-категории (идемпотентно)."""
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        existing = set(
            session.scalars(
                select(Category.name).where(Category.user_id.is_(None))
            )
        )
        for name in SEED_CATEGORIES:
            if name not in existing:
                session.add(Category(user_id=None, name=name))
        session.commit()
