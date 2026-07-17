"""Подключение к базе. SQLite сейчас, Postgres потом — заменой DATABASE_URL."""

from __future__ import annotations

import os

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from finbot.models import SEED_CATEGORIES, Base, Category

DEFAULT_URL = "sqlite:///finbot.db"


def make_engine(url: str | None = None):
    url = url or os.getenv("DATABASE_URL", DEFAULT_URL)
    kwargs = {}
    if url.startswith("sqlite"):
        # два юзера могут жать кнопки одновременно — ждём блокировку, не падаем
        kwargs["connect_args"] = {"timeout": 30}
    return create_engine(url, **kwargs)


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


# Колонки, добавленные после первых релизов: create_all не трогает
# существующие таблицы, поэтому досоздаём их через ALTER TABLE.
_MIGRATIONS: dict[str, dict[str, str]] = {
    "counterparties": {
        "category_id_in": "INTEGER REFERENCES categories(id)",
        "default_ownership_in": "VARCHAR(16)",
    },
    "question_queue": {
        "direction": "VARCHAR(3) NOT NULL DEFAULT 'all'",
        "qtype": "VARCHAR(12) NOT NULL DEFAULT 'category'",
    },
}


def _migrate(engine) -> None:
    with engine.begin() as conn:
        for table, columns in _MIGRATIONS.items():
            existing = {
                row[1]
                for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")
            }
            if not existing:  # таблицы ещё нет — её создаст create_all
                continue
            for column, ddl in columns.items():
                if column not in existing:
                    conn.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
                    )


def init_db(engine) -> None:
    """Создаёт таблицы, мигрирует схему и досеивает сид-категории (идемпотентно)."""
    _migrate(engine)
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
