"""Загрузка распарсенной выписки в базу с дедупликацией (спека §5.5).

Два уровня защиты от дублей:
1. Хэш всего файла — против повторной загрузки того же PDF.
2. Мультимножество ключей (date, amount, op_type, counterparty_raw) с учётом
   кратности — против перекрывающихся периодов (недельная + месячная).
   Три одинаковых Avtobys по 110 ₸ в день — норма, не дубли.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from finbot.models import Statement, Transaction, User
from finbot.parser import ParsedStatement

TxKey = tuple  # (date, amount, op_type, counterparty_raw)


@dataclass(frozen=True)
class IngestResult:
    statement_id: int | None
    added: int
    duplicates_skipped: int
    duplicate_file: bool = False


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ingest_statement(
    session: Session,
    user: User,
    parsed: ParsedStatement,
    file_hash: str,
) -> IngestResult:
    """Кладёт выписку в базу. Возвращает отчёт: добавлено / пропущено дублей."""
    already = session.scalar(
        select(Statement.id).where(
            Statement.user_id == user.id, Statement.file_hash == file_hash
        )
    )
    if already is not None:
        return IngestResult(
            statement_id=None, added=0, duplicates_skipped=0, duplicate_file=True
        )

    header = parsed.header
    existing_keys: Counter[TxKey] = Counter(
        (row.date, row.amount, row.op_type, row.counterparty_raw)
        for row in session.scalars(
            select(Transaction).where(
                Transaction.user_id == user.id,
                Transaction.date.between(header.period_start, header.period_end),
            )
        )
    )

    statement = Statement(
        user_id=user.id,
        period_start=header.period_start,
        period_end=header.period_end,
        opening_balance=header.opening_balance,
        closing_balance=header.closing_balance,
        file_hash=file_hash,
    )
    session.add(statement)
    session.flush()

    added = 0
    skipped = 0
    for tx in parsed.transactions:
        key = (tx.date, tx.amount, tx.op_type, tx.counterparty_raw)
        if existing_keys[key] > 0:
            existing_keys[key] -= 1
            skipped += 1
            continue
        session.add(
            Transaction(
                statement_id=statement.id,
                user_id=user.id,
                date=tx.date,
                amount=tx.amount,
                op_type=tx.op_type,
                counterparty_raw=tx.counterparty_raw,
                currency_note=tx.currency_note,
            )
        )
        added += 1

    session.commit()
    return IngestResult(
        statement_id=statement.id, added=added, duplicates_skipped=skipped
    )
