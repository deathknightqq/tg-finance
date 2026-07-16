"""Пайплайн обработки присланного PDF: parse → ingest → текст ответа.

Чистые синхронные функции без aiogram — бот лишь тонкая обвязка вокруг них.
Сырой PDF живёт только в памяти (bytes) и нигде не сохраняется.
"""

from __future__ import annotations

import io

from sqlalchemy import select
from sqlalchemy.orm import Session

from finbot.ingest import file_sha256, ingest_statement
from finbot.models import User
from finbot.parser import (
    GoldenRuleError,
    ParseError,
    UnsupportedLocaleError,
    parse_statement,
)


def get_or_create_user(session: Session, tg_id: int, name: str) -> User:
    user = session.scalar(select(User).where(User.tg_id == tg_id))
    if user is None:
        user = User(tg_id=tg_id, name=name)
        session.add(user)
        session.commit()
    return user


def _fmt_kzt(tiyn: int) -> str:
    kzt, rem = divmod(abs(tiyn), 100)
    sign = "-" if tiyn < 0 else ""
    return f"{sign}{kzt:,}".replace(",", " ") + f",{rem:02d} ₸"


def process_pdf(session: Session, tg_id: int, name: str, pdf_bytes: bytes) -> str:
    """Обрабатывает PDF и возвращает готовый текст ответа для чата."""
    user = get_or_create_user(session, tg_id, name)
    try:
        parsed = parse_statement(io.BytesIO(pdf_bytes))
    except UnsupportedLocaleError as e:
        return f"⛔ {e}"
    except GoldenRuleError as e:
        return (
            "⛔ Выписка отклонена целиком: баланс не сошёлся, "
            "молча принимать битый разбор нельзя.\n" + str(e)
        )
    except ParseError as e:
        return f"⛔ Не смог разобрать этот PDF: {e}"

    result = ingest_statement(session, user, parsed, file_sha256(pdf_bytes))
    if result.duplicate_file:
        return "Этот файл уже загружали — ничего не добавил."

    h = parsed.header
    unknown = len(
        {
            tx.counterparty_raw
            for tx in parsed.transactions
        }
    )
    lines = [
        f"✅ Выписка за {h.period_start:%d.%m.%y} – {h.period_end:%d.%m.%y} разобрана, "
        f"баланс сошёлся ({_fmt_kzt(h.opening_balance)} → {_fmt_kzt(h.closing_balance)}).",
        f"Новых операций: {result.added}",
    ]
    if result.duplicates_skipped:
        lines.append(
            f"Пропущено дублей (перекрытие с прошлыми выписками): "
            f"{result.duplicates_skipped}"
        )
    lines.append(
        f"Неизвестных контрагентов: {unknown} — опросник по категориям "
        "появится в следующем чанке."
    )
    return "\n".join(lines)
