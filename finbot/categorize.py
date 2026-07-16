"""Категоризация транзакций (спека §5.7–5.8).

Порядок авторазметки: сначала обученный словарь counterparties, затем
сид-правила по подстрокам. Неопознанное — в question_queue с весом
Σ|сумма| × частота. Повторный вопрос про того же контрагента — баг.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from finbot.models import (
    Category,
    Counterparty,
    QuestionQueue,
    Transaction,
    User,
)

# Сид-правила: подстрока (casefold) → имя категории из SEED_CATEGORIES.
# Порядок важен: первое совпадение выигрывает.
SEED_RULES: list[tuple[str, str]] = [
    ("yandex.go", "транспорт"),
    ("yandex.zapravki", "транспорт"),
    ("avtobys", "транспорт"),
    ("onay", "транспорт"),
    ("yandex.eda", "кафе и доставка"),
    ("yandex.lavka", "кафе и доставка"),
    ("wolt", "кафе и доставка"),
    ("glovo", "кафе и доставка"),
    ("coffee", "кафе и доставка"),
    ("yandex.plus", "подписки"),
    ("apple.com", "подписки"),
    ("google one", "подписки"),
    ("netflix", "подписки"),
    ("spotify", "подписки"),
    ("anthropic", "подписки"),
    ("beeline", "связь"),
    ("kcell", "связь"),
    ("tele2", "связь"),
    ("activ", "связь"),
    ("аптека", "аптека"),
    ("фарм", "аптека"),
    ("банкомат", "наличка"),
    ("снятие", "наличка"),
    ("ерц", "коммуналка"),
]


def normalize_name(raw: str) -> str:
    """Нормализация имени контрагента: casefold + схлопнутые пробелы."""
    return re.sub(r"\s+", " ", raw.strip()).casefold()


def seed_category_name(name_normalized: str, op_type: str) -> str | None:
    if op_type == "withdrawal":
        return "наличка"
    for substring, category in SEED_RULES:
        if substring in name_normalized:
            return category
    return None


@dataclass(frozen=True)
class AutoCatResult:
    assigned: int  # транзакций размечено автоматически
    queued: int  # контрагентов ушло в очередь вопросов


@dataclass(frozen=True)
class QuestionView:
    queue_id: int
    counterparty_id: int
    display_name: str
    examples: list[str]  # готовые строки «14.07 − 1 040,00 ₸»
    tx_count: int


@dataclass(frozen=True)
class AnswerResult:
    display_name: str
    category_name: str
    affected: int  # сколько транзакций разметилось этим ответом


def _fmt_kzt(tiyn: int) -> str:
    kzt, rem = divmod(abs(tiyn), 100)
    sign = "−" if tiyn < 0 else "+"
    return f"{sign}{kzt:,}".replace(",", " ") + f",{rem:02d} ₸"


def _category_by_name(session: Session, user: User, name: str) -> Category | None:
    return session.scalar(
        select(Category).where(
            Category.name == name,
            (Category.user_id.is_(None)) | (Category.user_id == user.id),
        )
    )


def _find_counterparty(
    session: Session, user: User, name_normalized: str
) -> Counterparty | None:
    scope = Counterparty.user_id == user.id
    if user.couple_id is not None:
        scope = scope | (Counterparty.couple_id == user.couple_id)
    return session.scalar(
        select(Counterparty).where(
            Counterparty.name_normalized == name_normalized, scope
        )
    )


def _apply_counterparty(tx: Transaction, cp: Counterparty) -> None:
    tx.counterparty_id = cp.id
    tx.category_id = cp.category_id
    tx.ownership = cp.default_ownership


def autocategorize(session: Session, user: User) -> AutoCatResult:
    """Разметить все неразмеченные транзакции юзера, остальное — в очередь."""
    todo = list(
        session.scalars(
            select(Transaction).where(
                Transaction.user_id == user.id,
                Transaction.counterparty_id.is_(None),
            )
        )
    )
    assigned = 0
    unknown: dict[str, list[Transaction]] = {}
    for tx in todo:
        name = normalize_name(tx.counterparty_raw)
        cp = _find_counterparty(session, user, name)
        if cp is None:
            seed_cat = seed_category_name(name, tx.op_type)
            if seed_cat is not None:
                category = _category_by_name(session, user, seed_cat)
                cp = Counterparty(
                    user_id=user.id,
                    name_normalized=name,
                    category_id=category.id if category else None,
                    default_ownership="mine",
                )
                session.add(cp)
                session.flush()
        if cp is not None:
            _apply_counterparty(tx, cp)
            if cp.category_id is not None:
                assigned += 1
            continue
        unknown.setdefault(name, []).append(tx)

    queued = 0
    for name, txs in unknown.items():
        cp = Counterparty(
            user_id=user.id, name_normalized=name, default_ownership="mine"
        )
        session.add(cp)
        session.flush()
        for tx in txs:
            tx.counterparty_id = cp.id
        session.add(
            QuestionQueue(
                user_id=user.id,
                counterparty_id=cp.id,
                weight=sum(abs(t.amount) for t in txs) * len(txs),
                status="pending",
            )
        )
        queued += 1

    _refresh_weights(session, user)
    session.commit()
    return AutoCatResult(assigned=assigned, queued=queued)


def _refresh_weights(session: Session, user: User) -> None:
    """Пересчитать веса pending-вопросов по всем транзакциям контрагента."""
    pending = list(
        session.scalars(
            select(QuestionQueue).where(
                QuestionQueue.user_id == user.id,
                QuestionQueue.status == "pending",
            )
        )
    )
    for q in pending:
        txs = list(
            session.scalars(
                select(Transaction).where(
                    Transaction.user_id == user.id,
                    Transaction.counterparty_id == q.counterparty_id,
                )
            )
        )
        q.weight = sum(abs(t.amount) for t in txs) * len(txs)


def pending_count(session: Session, user: User) -> int:
    return len(
        list(
            session.scalars(
                select(QuestionQueue.id).where(
                    QuestionQueue.user_id == user.id,
                    QuestionQueue.status == "pending",
                )
            )
        )
    )


def next_questions(
    session: Session, user: User, limit: int = 5
) -> list[QuestionView]:
    """Топ-N самых весомых вопросов очереди."""
    rows = list(
        session.scalars(
            select(QuestionQueue)
            .where(
                QuestionQueue.user_id == user.id,
                QuestionQueue.status == "pending",
            )
            .order_by(QuestionQueue.weight.desc())
            .limit(limit)
        )
    )
    views = []
    for q in rows:
        txs = list(
            session.scalars(
                select(Transaction)
                .where(
                    Transaction.user_id == user.id,
                    Transaction.counterparty_id == q.counterparty_id,
                )
                .order_by(Transaction.date.desc())
            )
        )
        views.append(
            QuestionView(
                queue_id=q.id,
                counterparty_id=q.counterparty_id,
                display_name=txs[0].counterparty_raw if txs else "?",
                examples=[
                    f"{t.date:%d.%m} {_fmt_kzt(t.amount)}" for t in txs[:3]
                ],
                tx_count=len(txs),
            )
        )
    return views


def list_categories(session: Session, user: User) -> list[Category]:
    """Сид-набор + собственные категории юзера."""
    return list(
        session.scalars(
            select(Category)
            .where((Category.user_id.is_(None)) | (Category.user_id == user.id))
            .order_by(Category.id)
        )
    )


def apply_answer(
    session: Session,
    user: User,
    queue_id: int,
    *,
    category_id: int | None = None,
    transit: bool = False,
    custom_name: str | None = None,
) -> AnswerResult:
    """Ответ юзера: пишется в counterparty и применяется ко всем его транзакциям."""
    q = session.get(QuestionQueue, queue_id)
    if q is None or q.user_id != user.id:
        raise ValueError(f"Вопрос {queue_id} не найден")
    cp = session.get(Counterparty, q.counterparty_id)

    if transit:
        category = _category_by_name(session, user, "транзит")
        cp.default_ownership = "transit"
    elif custom_name is not None:
        category = Category(user_id=user.id, name=custom_name.strip())
        session.add(category)
        session.flush()
    else:
        category = session.get(Category, category_id)
        if category is None:
            raise ValueError(f"Категория {category_id} не найдена")

    cp.category_id = category.id if category else None
    q.status = "answered"

    txs = list(
        session.scalars(
            select(Transaction).where(
                Transaction.user_id == user.id,
                Transaction.counterparty_id == cp.id,
            )
        )
    )
    for tx in txs:
        _apply_counterparty(tx, cp)
    session.commit()
    return AnswerResult(
        display_name=txs[0].counterparty_raw if txs else cp.name_normalized,
        category_name=category.name if category else "?",
        affected=len(txs),
    )
