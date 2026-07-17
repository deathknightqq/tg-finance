"""Пара (спека §6, чанк 7): инвайт, выборочный шаринг, матчинг переводов.

- Связка по инвайт-коду, соло-режим остаётся полноценным дефолтом.
- Шаринг выборочный: партнёр видит только расшаренные категории.
- Матчинг взаимных переводов: встречные знаки, равная сумма, дата ±1 день,
  взаимные контрагенты. Полное совпадение — авто-intrafamily (вылетает из
  статистики обоих). Частичное — вопрос с кнопкой, молча не схлопываем.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from finbot.models import (
    Category,
    CategoryShare,
    Invite,
    MatchDecline,
    Transaction,
    User,
)

_TRANSFER_OPS = ("transfer", "topup")


class CoupleError(Exception):
    """Ошибка связки пары с человекочитаемым текстом."""


# --- инвайт и связка ----------------------------------------------------------


def create_invite(session: Session, user: User) -> str:
    if user.couple_id is not None:
        raise CoupleError("Ты уже в паре — второй раз связаться нельзя.")
    code = secrets.token_hex(3).upper()  # 6 символов
    session.add(Invite(code=code, user_id=user.id))
    session.commit()
    return code


def join_couple(session: Session, user: User, code: str) -> User:
    """Возвращает партнёра. Куплу присваивается id пригласившего."""
    invite = session.get(Invite, code.strip().upper())
    if invite is None:
        raise CoupleError("Код не найден. Проверь и пришли ещё раз: /join КОД")
    if invite.user_id == user.id:
        raise CoupleError("Это твой собственный код — его вводит партнёр.")
    if user.couple_id is not None:
        raise CoupleError("Ты уже в паре.")
    inviter = session.get(User, invite.user_id)
    if inviter.couple_id is not None:
        raise CoupleError("Автор кода уже в паре.")
    inviter.couple_id = inviter.id
    user.couple_id = inviter.id
    session.delete(invite)
    session.commit()
    return inviter


def partner_of(session: Session, user: User) -> User | None:
    if user.couple_id is None:
        return None
    return session.scalar(
        select(User).where(User.couple_id == user.couple_id, User.id != user.id)
    )


# --- выборочный шаринг --------------------------------------------------------


def shared_category_ids(session: Session, user: User) -> set[int]:
    return set(
        session.scalars(
            select(CategoryShare.category_id).where(
                CategoryShare.user_id == user.id
            )
        )
    )


def toggle_share(session: Session, user: User, category_id: int) -> bool:
    """Переключает шаринг категории. Возвращает новое состояние (True = шарится)."""
    row = session.get(CategoryShare, (user.id, category_id))
    if row is None:
        session.add(CategoryShare(user_id=user.id, category_id=category_id))
        session.commit()
        return True
    session.delete(row)
    session.commit()
    return False


# --- матчинг взаимных переводов -----------------------------------------------


@dataclass(frozen=True)
class MatchCandidate:
    my_tx_id: int
    partner_tx_id: int
    date: str  # готовые строки для карточки
    amount: str
    partner_name: str


@dataclass(frozen=True)
class MatchResult:
    auto_matched: int
    candidates: list[MatchCandidate]


def _fmt_kzt(tiyn: int) -> str:
    sign = "−" if tiyn < 0 else "+"
    kzt, rem = divmod(abs(tiyn), 100)
    return f"{sign}{kzt:,}".replace(",", " ") + f",{rem:02d} ₸"


def _name_matches(counterparty_raw: str, person: User) -> bool:
    """«Аружан К.» ↔ партнёр с именем «Аружан»."""
    first = person.name.strip().split()[0].casefold() if person.name else ""
    return bool(first) and counterparty_raw.strip().casefold().startswith(first)


def _link(a: Transaction, b: Transaction) -> None:
    a.matched_tx_id = b.id
    b.matched_tx_id = a.id
    a.ownership = "intrafamily"
    b.ownership = "intrafamily"


def match_mutual_transfers(session: Session, user: User) -> MatchResult:
    """Ищет встречные переводы пары. Полное совпадение — авто, частичное — вопрос."""
    partner = partner_of(session, user)
    if partner is None:
        return MatchResult(auto_matched=0, candidates=[])

    def unmatched(u: User) -> list[Transaction]:
        return list(
            session.scalars(
                select(Transaction).where(
                    Transaction.user_id == u.id,
                    Transaction.matched_tx_id.is_(None),
                    Transaction.op_type.in_(_TRANSFER_OPS),
                )
            )
        )

    declined = {
        (d.tx_id, d.other_tx_id)
        for d in session.scalars(select(MatchDecline))
    }
    partner_txs = unmatched(partner)
    auto = 0
    candidates: list[MatchCandidate] = []
    used_partner: set[int] = set()
    for my in unmatched(user):
        for их in partner_txs:
            if их.id in used_partner or их.matched_tx_id is not None:
                continue
            if my.amount != -их.amount:
                continue
            if abs((my.date - их.date).days) > 1:
                continue
            if (my.id, их.id) in declined:
                continue
            names_mutual = _name_matches(my.counterparty_raw, partner) and (
                _name_matches(их.counterparty_raw, user)
            )
            if names_mutual:
                _link(my, их)
                used_partner.add(их.id)
                auto += 1
            else:
                candidates.append(
                    MatchCandidate(
                        my_tx_id=my.id,
                        partner_tx_id=их.id,
                        date=f"{my.date:%d.%m.%y}",
                        amount=_fmt_kzt(my.amount),
                        partner_name=partner.name,
                    )
                )
                used_partner.add(их.id)
            break
    session.commit()
    return MatchResult(auto_matched=auto, candidates=candidates)


def confirm_match(
    session: Session, user: User, my_tx_id: int, partner_tx_id: int, yes: bool
) -> None:
    my = session.get(Transaction, my_tx_id)
    их = session.get(Transaction, partner_tx_id)
    if my is None or их is None or my.user_id != user.id:
        raise ValueError("Пара транзакций не найдена")
    if yes:
        _link(my, их)
    elif session.get(MatchDecline, (my_tx_id, partner_tx_id)) is None:
        session.add(MatchDecline(tx_id=my_tx_id, other_tx_id=partner_tx_id))
    session.commit()


# --- совместный отчёт ---------------------------------------------------------


def partner_shared_totals(
    session: Session, user: User, start, end
) -> list[tuple[str, int]] | None:
    """Итоги партнёра по расшаренным категориям за период (None — пары нет)."""
    partner = partner_of(session, user)
    if partner is None:
        return None
    shared = shared_category_ids(session, partner)
    if not shared:
        return []
    totals: dict[int, int] = {}
    for t in session.scalars(
        select(Transaction).where(
            Transaction.user_id == partner.id,
            Transaction.date.between(start, end),
            Transaction.ownership == "mine",
            Transaction.netted_with_id.is_(None),
            Transaction.category_id.in_(shared),
            Transaction.amount < 0,
        )
    ):
        totals[t.category_id] = totals.get(t.category_id, 0) + t.amount
    names = {
        c.id: c.name
        for c in session.scalars(
            select(Category).where(Category.id.in_(totals.keys()))
        )
    }
    return sorted(
        ((names[cid], total) for cid, total in totals.items()),
        key=lambda pair: pair[1],
    )
