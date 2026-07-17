"""Пары списание+возврат (спека §5.6).

Признаки пары: тот же counterparty_raw, одинаковая |сумма|, противоположные
знаки, окно ±2 дня. При первой встрече пары по контрагенту — вопрос юзеру
(«схлопнуть / показывать отдельно»), ответ сохраняется в netting_rule
и больше не спрашивается. Схлопнутые пары связываются netted_with_id
и исключаются из статистики.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from finbot.models import Counterparty, QuestionQueue, Transaction, User

# Окна подобраны по реальным кейсам (шире спековых ±2 дней — фидбек юзера):
# возврат за покупку приходит в течение недели, долги между людьми
# возвращают и через несколько недель.
PURCHASE_WINDOW_DAYS = 7
PERSON_WINDOW_DAYS = 30
_PERSON_OPS = ("transfer", "topup")


def _pair_window(a: Transaction, b: Transaction) -> int:
    if a.op_type in _PERSON_OPS and b.op_type in _PERSON_OPS:
        return PERSON_WINDOW_DAYS
    return PURCHASE_WINDOW_DAYS


@dataclass(frozen=True)
class NettingScan:
    collapsed: int  # пар схлопнуто по уже известным правилам
    questions: int  # новых вопросов задано


def find_pairs(txs: list[Transaction]) -> list[tuple[Transaction, Transaction]]:
    """Жадный матчинг пар среди транзакций ОДНОГО контрагента."""
    unnetted = [t for t in txs if t.netted_with_id is None]
    by_abs: dict[int, list[Transaction]] = {}
    for t in unnetted:
        by_abs.setdefault(abs(t.amount), []).append(t)
    pairs = []
    for group in by_abs.values():
        pos = sorted((t for t in group if t.amount > 0), key=lambda t: t.date)
        neg = sorted((t for t in group if t.amount < 0), key=lambda t: t.date)
        used: set[int] = set()
        for p in pos:
            for n in neg:
                if id(n) in used:
                    continue
                if abs((p.date - n.date).days) <= _pair_window(p, n):
                    pairs.append((p, n))
                    used.add(id(n))
                    break
    return pairs


def _txs_by_counterparty(
    session: Session, user: User
) -> dict[int, list[Transaction]]:
    grouped: dict[int, list[Transaction]] = {}
    for t in session.scalars(
        select(Transaction).where(
            Transaction.user_id == user.id,
            Transaction.counterparty_id.is_not(None),
        )
    ):
        grouped.setdefault(t.counterparty_id, []).append(t)
    return grouped


def _collapse(pair: tuple[Transaction, Transaction]) -> None:
    p, n = pair
    p.netted_with_id = n.id
    n.netted_with_id = p.id


def scan_pairs(session: Session, user: User) -> NettingScan:
    """Ищет пары по всем контрагентам юзера.

    Известное правило применяется молча; неизвестное — вопрос в очередь
    (один на контрагента, при первой встрече пары).
    """
    collapsed = 0
    questions = 0
    for cp_id, txs in _txs_by_counterparty(session, user).items():
        pairs = find_pairs(txs)
        if not pairs:
            continue
        cp = session.get(Counterparty, cp_id)
        if cp.netting_rule == "collapse":
            for pair in pairs:
                _collapse(pair)
                collapsed += 1
        elif cp.netting_rule == "show":
            continue
        else:
            asked = session.scalar(
                select(QuestionQueue.id).where(
                    QuestionQueue.user_id == user.id,
                    QuestionQueue.counterparty_id == cp_id,
                    QuestionQueue.qtype == "netting",
                    QuestionQueue.status.in_(("pending", "skipped")),
                )
            )
            if asked is None:
                session.add(
                    QuestionQueue(
                        user_id=user.id,
                        counterparty_id=cp_id,
                        weight=sum(abs(p.amount) for p, _ in pairs) * len(pairs),
                        status="pending",
                        qtype="netting",
                    )
                )
                questions += 1
    session.commit()
    return NettingScan(collapsed=collapsed, questions=questions)


def apply_netting_answer(
    session: Session, user: User, queue_id: int, rule: str
) -> tuple[str, int]:
    """Ответ юзера («collapse» / «show»). Возвращает (имя, схлопнуто пар)."""
    if rule not in ("collapse", "show"):
        raise ValueError(f"Неизвестное правило неттинга: {rule!r}")
    q = session.get(QuestionQueue, queue_id)
    if q is None or q.user_id != user.id or q.qtype != "netting":
        raise ValueError(f"Вопрос неттинга {queue_id} не найден")
    cp = session.get(Counterparty, q.counterparty_id)
    cp.netting_rule = rule
    q.status = "answered"

    txs = list(
        session.scalars(
            select(Transaction).where(
                Transaction.user_id == user.id,
                Transaction.counterparty_id == cp.id,
            )
        )
    )
    collapsed = 0
    if rule == "collapse":
        for pair in find_pairs(txs):
            _collapse(pair)
            collapsed += 1
    session.commit()
    name = txs[0].counterparty_raw if txs else cp.name_normalized
    return name, collapsed
