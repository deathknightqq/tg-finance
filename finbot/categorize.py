"""Категоризация транзакций (спека §5.7–5.8).

Порядок авторазметки: сначала обученный словарь counterparties, затем
сид-правила по подстрокам. Неопознанное — в question_queue с весом
Σ|сумма| × частота. Повторный вопрос про того же контрагента — баг.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import case, func, select
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
    direction: str = "all"  # all | in | out
    qtype: str = "category"  # category | netting


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
    """Входящие берут in-разметку, если она задана; иначе основную."""
    tx.counterparty_id = cp.id
    if tx.amount > 0 and cp.default_ownership_in is not None:
        tx.category_id = cp.category_id_in
        tx.ownership = cp.default_ownership_in
    else:
        tx.category_id = cp.category_id
        tx.ownership = cp.default_ownership


def _tx_matches_direction(tx: Transaction, direction: str) -> bool:
    if direction == "in":
        return tx.amount > 0
    if direction == "out":
        return tx.amount <= 0
    return True


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
        # Смешанные знаки — два отдельных вопроса (исходящие и входящие):
        # у транзитного контрагента входящие могут быть зарплатой.
        has_out = any(t.amount <= 0 for t in txs)
        has_in = any(t.amount > 0 for t in txs)
        directions = ["out", "in"] if (has_out and has_in) else ["all"]
        for direction in directions:
            session.add(
                QuestionQueue(
                    user_id=user.id,
                    counterparty_id=cp.id,
                    weight=0,  # ниже пересчитает _refresh_weights
                    status="pending",
                    direction=direction,
                )
            )
            queued += 1

    _split_mixed_questions(session, user)
    _refresh_weights(session, user)
    session.commit()
    return AutoCatResult(assigned=assigned, queued=queued)


def _split_mixed_questions(session: Session, user: User) -> None:
    """Old-данные: вопрос direction=all по смешанному контрагенту → out + in."""
    for q in list(
        session.scalars(
            select(QuestionQueue).where(
                QuestionQueue.user_id == user.id,
                QuestionQueue.status.in_(("pending", "skipped")),
                QuestionQueue.qtype == "category",
                QuestionQueue.direction == "all",
            )
        )
    ):
        txs = list(
            session.scalars(
                select(Transaction).where(
                    Transaction.user_id == user.id,
                    Transaction.counterparty_id == q.counterparty_id,
                )
            )
        )
        if any(t.amount > 0 for t in txs) and any(t.amount <= 0 for t in txs):
            q.direction = "out"
            session.add(
                QuestionQueue(
                    user_id=user.id,
                    counterparty_id=q.counterparty_id,
                    weight=q.weight,
                    status=q.status,
                    direction="in",
                )
            )


def _refresh_weights(session: Session, user: User) -> None:
    """Пересчитать веса pending-вопросов по всем транзакциям контрагента."""
    pending = list(
        session.scalars(
            select(QuestionQueue).where(
                QuestionQueue.user_id == user.id,
                QuestionQueue.status.in_(("pending", "skipped")),
                QuestionQueue.qtype == "category",
            )
        )
    )
    for q in pending:
        txs = [
            t
            for t in session.scalars(
                select(Transaction).where(
                    Transaction.user_id == user.id,
                    Transaction.counterparty_id == q.counterparty_id,
                )
            )
            if _tx_matches_direction(t, q.direction)
        ]
        q.weight = sum(abs(t.amount) for t in txs) * len(txs)


def pending_count(session: Session, user: User) -> int:
    return len(
        list(
            session.scalars(
                select(QuestionQueue.id).where(
                    QuestionQueue.user_id == user.id,
                    QuestionQueue.status.in_(("pending", "skipped")),
                )
            )
        )
    )


def next_questions(
    session: Session, user: User, limit: int = 5
) -> list[QuestionView]:
    """Топ-N вопросов: сначала свежие по весу, отложенные («потом») — в конце."""
    rows = list(
        session.scalars(
            select(QuestionQueue)
            .where(
                QuestionQueue.user_id == user.id,
                QuestionQueue.status.in_(("pending", "skipped")),
            )
            .order_by(
                case((QuestionQueue.status == "pending", 0), else_=1),
                QuestionQueue.weight.desc(),
            )
            .limit(limit)
        )
    )
    views = []
    for q in rows:
        all_txs = list(
            session.scalars(
                select(Transaction)
                .where(
                    Transaction.user_id == user.id,
                    Transaction.counterparty_id == q.counterparty_id,
                )
                .order_by(Transaction.date.desc())
            )
        )
        display = all_txs[0].counterparty_raw if all_txs else "?"
        if q.qtype == "netting":
            from finbot.netting import find_pairs

            pairs = find_pairs(all_txs)
            examples = [
                f"{p.date:%d.%m} {_fmt_kzt(p.amount)} ↔ "
                f"{n.date:%d.%m} {_fmt_kzt(n.amount)}"
                for p, n in pairs[:3]
            ]
            count = len(pairs)
        else:
            txs = [t for t in all_txs if _tx_matches_direction(t, q.direction)]
            examples = [f"{t.date:%d.%m} {_fmt_kzt(t.amount)}" for t in txs[:3]]
            count = len(txs)
        views.append(
            QuestionView(
                queue_id=q.id,
                counterparty_id=q.counterparty_id,
                display_name=display,
                examples=examples,
                tx_count=count,
                direction=q.direction,
                qtype=q.qtype,
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


# Для входящих денег расходные категории — шум; оставляем осмысленное.
_IN_SEED_CATEGORIES = ("доход", "семья", "наличка", "прочее")


def categories_for_question(
    session: Session, user: User, direction: str
) -> list[tuple[int, str]]:
    """(id, name) для клавиатуры вопроса: по направлению, частые — первыми.

    «транзит» не включается — у него своя кнопка. Свои категории юзера
    показываются всегда.
    """
    cats = list_categories(session, user)
    usage: dict[int, int] = dict(
        session.execute(
            select(Transaction.category_id, func.count())
            .where(
                Transaction.user_id == user.id,
                Transaction.category_id.is_not(None),
            )
            .group_by(Transaction.category_id)
        ).all()
    )
    if direction == "in":
        pool = [
            c
            for c in cats
            if c.user_id == user.id or c.name in _IN_SEED_CATEGORIES
        ]
    else:
        pool = [c for c in cats if c.name not in ("доход", "транзит")]
    pool.sort(key=lambda c: -usage.get(c.id, 0))  # sort стабилен: при равенстве
    return [(c.id, c.name) for c in pool]  # сохраняется сид-порядок


def skip_question(session: Session, user: User, queue_id: int) -> str:
    """«Потом»: вопрос уходит в конец очереди, но не исчезает из /unsorted."""
    q = session.get(QuestionQueue, queue_id)
    if q is None or q.user_id != user.id:
        raise ValueError(f"Вопрос {queue_id} не найден")
    if q.status == "answered":
        raise ValueError(f"Вопрос {queue_id} уже отвечен")
    q.status = "skipped"
    cp = session.get(Counterparty, q.counterparty_id)
    tx = session.scalar(
        select(Transaction).where(Transaction.counterparty_id == cp.id).limit(1)
    )
    session.commit()
    return tx.counterparty_raw if tx else cp.name_normalized


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
    elif custom_name is not None:
        category = Category(user_id=user.id, name=custom_name.strip())
        session.add(category)
        session.flush()
    else:
        category = session.get(Category, category_id)
        if category is None:
            raise ValueError(f"Категория {category_id} не найдена")

    ownership = "transit" if transit else "mine"
    if q.direction == "in":
        cp.category_id_in = category.id if category else None
        cp.default_ownership_in = ownership
    else:
        cp.category_id = category.id if category else None
        cp.default_ownership = ownership
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
    affected = sum(1 for t in txs if _tx_matches_direction(t, q.direction))
    session.commit()
    return AnswerResult(
        display_name=txs[0].counterparty_raw if txs else cp.name_normalized,
        category_name=category.name if category else "?",
        affected=affected,
    )
