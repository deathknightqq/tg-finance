"""Модель данных по спеке §4. Деньги — тиыны как signed integer, не float."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ownership: mine | transit | intrafamily | unassigned
# op_type: purchase | topup | transfer | withdrawal
# netting_rule: collapse | show | None (ещё не спрашивали)

SEED_CATEGORIES = [
    "продукты", "кафе и доставка", "транспорт", "подписки", "связь",
    "аптека", "коммуналка", "дом/быт", "наличка", "транзит", "семья",
    "развлечения", "прочее",
]


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    couple_id: Mapped[int | None] = mapped_column(default=None)  # NULL — соло-режим


class Statement(Base):
    __tablename__ = "statements"
    __table_args__ = (UniqueConstraint("user_id", "file_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    period_start: Mapped[date]
    period_end: Mapped[date]
    opening_balance: Mapped[int]  # тиыны
    closing_balance: Mapped[int]  # тиыны
    file_hash: Mapped[str] = mapped_column(String(64))
    uploaded_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )

    transactions: Mapped[list[Transaction]] = relationship(back_populates="statement")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey("statements.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    date: Mapped[date] = mapped_column(index=True)
    amount: Mapped[int]  # тиыны, со знаком
    op_type: Mapped[str] = mapped_column(String(16))
    counterparty_raw: Mapped[str] = mapped_column(String(200))
    counterparty_id: Mapped[int | None] = mapped_column(
        ForeignKey("counterparties.id"), default=None
    )
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id"), default=None
    )
    ownership: Mapped[str] = mapped_column(String(16), default="unassigned")
    matched_tx_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id"), default=None
    )
    netted_with_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id"), default=None
    )
    currency_note: Mapped[str | None] = mapped_column(String(100), default=None)

    statement: Mapped[Statement] = relationship(back_populates="transactions")


class Counterparty(Base):
    """Сердце системы: сюда пишутся ответы юзера.

    Scope — либо пара (couple_id), либо один юзер (user_id): ровно одно из двух.
    Повторный вопрос про того же контрагента — баг.
    """

    __tablename__ = "counterparties"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    couple_id: Mapped[int | None] = mapped_column(default=None)
    name_normalized: Mapped[str] = mapped_column(String(200), index=True)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id"), default=None
    )
    default_ownership: Mapped[str] = mapped_column(String(16), default="mine")
    netting_rule: Mapped[str | None] = mapped_column(String(16), default=None)


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), default=None
    )  # NULL — сид-набор
    name: Mapped[str] = mapped_column(String(100))
    is_shared: Mapped[bool] = mapped_column(default=False)


class Budget(Base):
    __tablename__ = "budgets"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    month: Mapped[str] = mapped_column(String(7), primary_key=True)  # "2026-07"
    amount: Mapped[int]  # тиыны


class QuestionQueue(Base):
    __tablename__ = "question_queue"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    counterparty_id: Mapped[int] = mapped_column(ForeignKey("counterparties.id"))
    weight: Mapped[int]  # Σ|сумма| × частота, тиыны
    status: Mapped[str] = mapped_column(String(16), default="pending")
