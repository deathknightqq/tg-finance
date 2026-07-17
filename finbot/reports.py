"""Отчёты и бюджет (спека §6, §7.6).

Правила счёта:
- расходы/доходы «мои» — ownership == mine; unassigned показывается
  отдельной строкой и отчёт не блокирует (спека §5.7);
- transit и intrafamily исключены;
- схлопнутые пары (netted_with_id) исключены;
- «свободно до конца месяца» = бюджет месяца − потрачено с ownership=mine.
Регулярные списания — только информер, в расчёт свободного не вмешиваются.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from finbot.models import Budget, Category, Transaction, User


def _fmt_kzt(tiyn: int) -> str:
    sign = "−" if tiyn < 0 else ""
    kzt, rem = divmod(abs(tiyn), 100)
    body = f"{kzt:,}".replace(",", " ")
    return f"{sign}{body},{rem:02d} ₸" if rem else f"{sign}{body} ₸"


def month_bounds(today: date) -> tuple[date, date]:
    start = today.replace(day=1)
    next_month = (start + timedelta(days=32)).replace(day=1)
    return start, next_month - timedelta(days=1)


@dataclass(frozen=True)
class ReportData:
    period_start: date
    period_end: date
    expenses_by_category: list[tuple[str, int]]  # (категория, сумма<0) по убыванию
    uncategorized: int  # мои расходы без категории
    unassigned: int  # расходы с ownership=unassigned (неразобранное)
    income: int  # мои доходы
    total_expenses: int  # мои расходы: категории + без категории (unassigned не входит)


def _visible_txs(session: Session, user: User, start: date, end: date):
    return list(
        session.scalars(
            select(Transaction).where(
                Transaction.user_id == user.id,
                Transaction.date.between(start, end),
                Transaction.netted_with_id.is_(None),
                Transaction.ownership.in_(("mine", "unassigned")),
            )
        )
    )


def build_report(
    session: Session, user: User, start: date, end: date
) -> ReportData:
    by_cat: dict[int | None, int] = defaultdict(int)
    uncategorized = 0
    unassigned = 0
    income = 0
    for t in _visible_txs(session, user, start, end):
        if t.ownership == "unassigned":
            if t.amount < 0:
                unassigned += t.amount
            continue
        if t.amount > 0:
            income += t.amount
            continue
        if t.category_id is None:
            uncategorized += t.amount
        else:
            by_cat[t.category_id] += t.amount

    names = {
        c.id: c.name
        for c in session.scalars(
            select(Category).where(Category.id.in_(by_cat.keys()))
        )
    }
    expenses = sorted(
        ((names.get(cid, "?"), total) for cid, total in by_cat.items()),
        key=lambda pair: pair[1],
    )
    return ReportData(
        period_start=start,
        period_end=end,
        expenses_by_category=expenses,
        uncategorized=uncategorized,
        unassigned=unassigned,
        income=income,
        total_expenses=sum(by_cat.values()) + uncategorized,
    )


def get_budget(session: Session, user: User, month: str) -> int | None:
    row = session.get(Budget, (user.id, month))
    return row.amount if row else None


def set_budget(session: Session, user: User, month: str, amount: int) -> None:
    row = session.get(Budget, (user.id, month))
    if row is None:
        session.add(Budget(user_id=user.id, month=month, amount=amount))
    else:
        row.amount = amount
    session.commit()


def free_until_month_end(
    session: Session, user: User, today: date
) -> tuple[int, int, int] | None:
    """(бюджет, потрачено mine, свободно) за календарный месяц today, или None."""
    budget = get_budget(session, user, f"{today:%Y-%m}")
    if budget is None:
        return None
    start, end = month_bounds(today)
    spent = -sum(
        t.amount
        for t in _visible_txs(session, user, start, end)
        if t.ownership == "mine" and t.amount < 0
    )
    return budget, spent, budget - spent


def find_regular_payments(
    session: Session, user: User
) -> list[tuple[str, int]]:
    """Информер: контрагенты с «моими» списаниями в ≥2 разных месяцах.

    Возвращает (имя, средняя сумма за месяц). В расчёт свободного не входит.
    """
    monthly: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    names: dict[int, str] = {}
    for t in session.scalars(
        select(Transaction).where(
            Transaction.user_id == user.id,
            Transaction.amount < 0,
            Transaction.ownership == "mine",
            Transaction.netted_with_id.is_(None),
            Transaction.counterparty_id.is_not(None),
        )
    ):
        monthly[t.counterparty_id][f"{t.date:%Y-%m}"] += -t.amount
        names.setdefault(t.counterparty_id, t.counterparty_raw)
    regular = []
    for cp_id, months in monthly.items():
        if len(months) >= 2:
            avg = sum(months.values()) // len(months)
            regular.append((names[cp_id], avg))
    regular.sort(key=lambda pair: -pair[1])
    return regular


def format_report(
    data: ReportData,
    *,
    title: str,
    budget_line: tuple[int, int, int] | None = None,
    regular: list[tuple[str, int]] | None = None,
) -> str:
    lines = [f"📊 <b>{title}</b> ({data.period_start:%d.%m} – {data.period_end:%d.%m})"]
    if data.expenses_by_category or data.uncategorized:
        lines.append("\nРасходы:")
        for name, total in data.expenses_by_category:
            lines.append(f"  {name}: {_fmt_kzt(total)}")
        if data.uncategorized:
            lines.append(f"  без категории: {_fmt_kzt(data.uncategorized)}")
        lines.append(f"Итого расходов: {_fmt_kzt(data.total_expenses)}")
    else:
        lines.append("\nРасходов нет.")
    if data.income:
        lines.append(f"Доходы: +{_fmt_kzt(data.income)}")
    if data.unassigned:
        lines.append(
            f"⚠️ Неразобранное (жду ответов в /unsorted): {_fmt_kzt(data.unassigned)}"
        )
    if budget_line is not None:
        budget, spent, free = budget_line
        lines.append(
            f"\n💰 Бюджет месяца: {_fmt_kzt(budget)} | потрачено: {_fmt_kzt(spent)} "
            f"| свободно: <b>{_fmt_kzt(free)}</b>"
        )
    if regular:
        top = ", ".join(f"{name} ~{_fmt_kzt(avg)}/мес" for name, avg in regular[:5])
        lines.append(f"\n🔁 Похоже на регулярные: {top}")
    return "\n".join(lines)
