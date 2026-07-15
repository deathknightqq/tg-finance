"""Парсер PDF-выписки Kaspi Gold (локаль RU).

Чистый модуль без зависимостей от бота и базы: PDF → шапка + список транзакций.
Golden Rule (opening + Σ транзакций == closing, тиын в тиын) — обязательный гейт:
не сошлось — выписка отклоняется целиком с отчётом, молча битый парс не отдаём.

Персональные данные (ФИО, ИИН, IBAN, номер карты) не извлекаются и не хранятся.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import IO

import pdfplumber

# Маркеры локали в шапке выписки. RU поддержан; KZ/EN детектим и честно отказываем.
_LOCALE_MARKERS = {
    "ru": "ВЫПИСКА",
    "kk": "ҮЗІНДІ КӨШІРМЕ",
    "en": "STATEMENT",
}

_OP_TYPES = {
    "Покупка": "purchase",
    "Пополнение": "topup",
    "Перевод": "transfer",
    "Снятие": "withdrawal",
}

_DATE_RE = r"\d{2}\.\d{2}\.\d{2}"
_AMOUNT_RE = r"[+-]\s*[\d\s ]+,\d{2}"

_PERIOD_RE = re.compile(rf"за период с ({_DATE_RE}) по ({_DATE_RE})")
_TX_RE = re.compile(
    rf"^({_DATE_RE}) ({_AMOUNT_RE}) ₸ ({'|'.join(_OP_TYPES)}) (.+)$"
)
# Continuation-строка: продолжение предыдущей транзакции без даты,
# например "(- 23,20 USD)" — валютная пометка.
_CONTINUATION_RE = re.compile(r"^\(.+\)$")
_TABLE_HEADER = "Дата Сумма Операция Детали"

# Служебные строки, которые не являются ни транзакциями, ни continuation.
_NOISE_MARKERS = (
    "kaspi.kz",  # футер; на некоторых страницах идёт с задвоенными глифами
    "Приложение к Справке",
    "Сумма заблокирована",
    "Раздел «Краткое содержание",
    "содержит информацию об операциях",
)


class ParseError(Exception):
    """Выписка не разобрана: неожиданный формат."""


class UnsupportedLocaleError(ParseError):
    """Локаль выписки детектирована, но не поддерживается."""

    def __init__(self, locale: str):
        self.locale = locale
        super().__init__(
            f"Локаль выписки «{locale}» не поддерживается, нужна русская (RU)."
        )


class GoldenRuleError(ParseError):
    """Баланс не сошёлся: выписка отклоняется целиком."""

    def __init__(self, *, opening: int, closing: int, tx_sum: int, tx_count: int):
        self.opening = opening
        self.closing = closing
        self.tx_sum = tx_sum
        self.tx_count = tx_count
        self.delta = opening + tx_sum - closing
        super().__init__(
            f"Golden Rule не сошёлся: распарсено {tx_count} транзакций, "
            f"opening {_fmt_tiyn(opening)} + операции {_fmt_tiyn(tx_sum)} "
            f"= {_fmt_tiyn(opening + tx_sum)}, а closing {_fmt_tiyn(closing)} "
            f"(дельта {_fmt_tiyn(self.delta)})."
        )


@dataclass(frozen=True)
class StatementHeader:
    period_start: date
    period_end: date
    opening_balance: int  # тиыны
    closing_balance: int  # тиыны


@dataclass
class ParsedTransaction:
    date: date
    amount: int  # тиыны, со знаком
    op_type: str  # purchase | topup | transfer | withdrawal
    counterparty_raw: str
    currency_note: str | None = None


@dataclass
class ParsedStatement:
    header: StatementHeader
    transactions: list[ParsedTransaction] = field(default_factory=list)


def _fmt_tiyn(tiyn: int) -> str:
    sign = "-" if tiyn < 0 else "+"
    kzt, rem = divmod(abs(tiyn), 100)
    return f"{sign}{kzt:,}".replace(",", " ") + f",{rem:02d} ₸"


def parse_amount(raw: str) -> int:
    """«- 1 800,00» → -180000 (тиыны). Пробелы/nbsp — разделители тысяч."""
    m = re.fullmatch(r"([+-])\s*([\d\s ]+),(\d{2})", raw.strip())
    if not m:
        raise ParseError(f"Не удалось разобрать сумму: {raw!r}")
    sign, whole, cents = m.groups()
    value = int(re.sub(r"[\s ]", "", whole)) * 100 + int(cents)
    return -value if sign == "-" else value


def parse_date(raw: str) -> date:
    """ДД.ММ.ГГ → date (век 2000+)."""
    try:
        return datetime.strptime(raw, "%d.%m.%y").date()
    except ValueError as e:
        raise ParseError(f"Не удалось разобрать дату: {raw!r}") from e


def _is_noise(line: str) -> bool:
    if any(marker in line for marker in _NOISE_MARKERS):
        return True
    # Футер с задвоенными глифами («ААОО ««KKaassppii...»): схлопываем пары
    # одинаковых символов и проверяем маркеры ещё раз.
    collapsed = re.sub(r"(.)\1", r"\1", line)
    return any(marker in collapsed for marker in _NOISE_MARKERS)


def _detect_locale(full_text: str) -> str | None:
    for locale, marker in _LOCALE_MARKERS.items():
        if marker in full_text:
            return locale
    return None


def _parse_header(text: str) -> StatementHeader:
    period_m = _PERIOD_RE.search(text)
    if not period_m:
        raise ParseError("Не найден период выписки («за период с ... по ...»).")
    period_start = parse_date(period_m.group(1))
    period_end = parse_date(period_m.group(2))

    def balance_on(d: str) -> int:
        m = re.search(rf"Доступно на {re.escape(d)}:?\s*({_AMOUNT_RE})\s*₸", text)
        if not m:
            raise ParseError(f"Не найден остаток «Доступно на {d}».")
        return parse_amount(m.group(1))

    return StatementHeader(
        period_start=period_start,
        period_end=period_end,
        opening_balance=balance_on(period_m.group(1)),
        closing_balance=balance_on(period_m.group(2)),
    )


def _parse_transactions(pages_lines: list[list[str]]) -> list[ParsedTransaction]:
    transactions: list[ParsedTransaction] = []
    in_table = False
    for lines in pages_lines:
        for line in lines:
            line = line.strip()
            if not line or _is_noise(line):
                continue
            if not in_table:
                if line == _TABLE_HEADER:
                    in_table = True
                continue
            tx_m = _TX_RE.match(line)
            if tx_m:
                raw_date, raw_amount, op_word, details = tx_m.groups()
                transactions.append(
                    ParsedTransaction(
                        date=parse_date(raw_date),
                        amount=parse_amount(raw_amount),
                        op_type=_OP_TYPES[op_word],
                        counterparty_raw=details.strip(),
                    )
                )
            elif _CONTINUATION_RE.match(line):
                if not transactions:
                    raise ParseError(
                        f"Continuation-строка до первой транзакции: {line!r}"
                    )
                prev = transactions[-1]
                note = line[1:-1].strip()
                prev.currency_note = (
                    f"{prev.currency_note}; {note}" if prev.currency_note else note
                )
            else:
                raise ParseError(f"Неопознанная строка в таблице операций: {line!r}")
    if not in_table:
        raise ParseError("Не найдена таблица операций («Дата Сумма Операция Детали»).")
    if not transactions:
        raise ParseError("Таблица операций пуста.")
    return transactions


def parse_statement(source: str | Path | IO[bytes]) -> ParsedStatement:
    """PDF → ParsedStatement. Бросает ParseError/GoldenRuleError, мусор не отдаёт."""
    with pdfplumber.open(source) as pdf:
        pages_text = [page.extract_text() or "" for page in pdf.pages]

    full_text = "\n".join(pages_text)
    locale = _detect_locale(full_text)
    if locale is None:
        raise ParseError("Это не похоже на выписку Kaspi: маркер шапки не найден.")
    if locale != "ru":
        raise UnsupportedLocaleError(locale)

    header = _parse_header(full_text)
    transactions = _parse_transactions([t.splitlines() for t in pages_text])

    tx_sum = sum(tx.amount for tx in transactions)
    if header.opening_balance + tx_sum != header.closing_balance:
        raise GoldenRuleError(
            opening=header.opening_balance,
            closing=header.closing_balance,
            tx_sum=tx_sum,
            tx_count=len(transactions),
        )
    return ParsedStatement(header=header, transactions=transactions)
