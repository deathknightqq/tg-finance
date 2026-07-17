from datetime import date

import pytest
from sqlalchemy import select

from finbot.db import init_db, make_engine, make_session_factory
from finbot.ingest import file_sha256, ingest_statement
from finbot.models import Category, Transaction, User
from finbot.categorize import (
    apply_answer,
    autocategorize,
    next_questions,
)
from finbot.netting import apply_netting_answer, scan_pairs
from finbot.parser import ParsedStatement, ParsedTransaction, StatementHeader
from finbot.reports import (
    build_report,
    find_regular_payments,
    format_report,
    free_until_month_end,
    get_budget,
    month_bounds,
    set_budget,
)


@pytest.fixture()
def session():
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def user(session):
    u = User(tg_id=123, name="Тест")
    session.add(u)
    session.commit()
    return u


def tx(d, amount, op_type="purchase", cp="M market"):
    return ParsedTransaction(date=d, amount=amount, op_type=op_type, counterparty_raw=cp)


def ingest(session, user, txs, blob=b"s1", start=date(2026, 7, 1), end=date(2026, 7, 31)):
    parsed = ParsedStatement(
        header=StatementHeader(
            period_start=start, period_end=end,
            opening_balance=0, closing_balance=sum(t.amount for t in txs),
        ),
        transactions=txs,
    )
    ingest_statement(session, user, parsed, file_sha256(blob))


class TestMonthBounds:
    def test_mid_month(self):
        assert month_bounds(date(2026, 7, 17)) == (date(2026, 7, 1), date(2026, 7, 31))

    def test_february(self):
        assert month_bounds(date(2028, 2, 5)) == (date(2028, 2, 1), date(2028, 2, 29))


class TestBuildReport:
    def test_numbers_match_manual_recount(self, session, user):
        """Чекпоинт чанка 6: цифры отчёта сходятся с ручным пересчётом."""
        ingest(session, user, [
            tx(date(2026, 7, 2), -11000, cp="Билет Avtobys. Оплата проезда"),  # транспорт
            tx(date(2026, 7, 3), -22000, cp="Билет Avtobys. Оплата проезда"),  # транспорт
            tx(date(2026, 7, 4), -500000, cp="WOLT.COM"),  # кафе
            tx(date(2026, 7, 5), -300000, cp="ИП Кто-То"),  # unassigned
            tx(date(2026, 7, 6), 9100000, op_type="topup", cp="Работодатель"),  # unassigned +
            tx(date(2026, 7, 10), -160000000, cp="Гүлім С."),  # станет транзитом
            tx(date(2026, 7, 14), 180000, cp="OI Market"),  # пара
            tx(date(2026, 7, 14), -180000, cp="OI Market"),  # пара
        ])
        autocategorize(session, user)
        scan_pairs(session, user)
        qs = next_questions(session, user, 20)
        transit_q = next(q for q in qs if "Гүлім" in q.display_name)
        apply_answer(session, user, transit_q.queue_id, transit=True)
        net_q = next(q for q in qs if q.qtype == "netting")
        apply_netting_answer(session, user, net_q.queue_id, "collapse")
        income_cat = session.scalar(select(Category).where(Category.name == "доход"))
        salary_q = next(q for q in qs if "Работодатель" in q.display_name)
        apply_answer(session, user, salary_q.queue_id, category_id=income_cat.id)

        data = build_report(session, user, date(2026, 7, 1), date(2026, 7, 31))
        # ручной пересчёт:
        assert dict(data.expenses_by_category) == {
            "кафе и доставка": -500000,
            "транспорт": -33000,
        }
        assert data.expenses_by_category[0][0] == "кафе и доставка"  # крупное сверху
        assert data.total_expenses == -533000
        assert data.income == 9100000
        assert data.unassigned == -300000  # ИП Кто-То ещё не отвечен
        assert data.uncategorized == 0
        # транзит и схлопнутая пара не участвуют нигде
        flat = data.total_expenses + data.unassigned
        assert flat == -833000

    def test_format_contains_totals(self, session, user):
        ingest(session, user, [tx(date(2026, 7, 2), -11000, cp="Билет Avtobys. Оплата проезда")])
        autocategorize(session, user)
        data = build_report(session, user, date(2026, 7, 1), date(2026, 7, 31))
        text = format_report(data, title="Отчёт")
        assert "транспорт" in text and "110 ₸" in text


class TestPartialRefund:
    def test_refund_reduces_category_not_income(self, session, user):
        """Смолл: купил на 40 000, возврат 4 000 — категория нетто −36 000."""
        ingest(session, user, [
            tx(date(2026, 7, 3), -4000000, cp="SMALL"),
            tx(date(2026, 7, 5), 400000, cp="SMALL"),
        ])
        autocategorize(session, user)
        qs = next_questions(session, user, 10)
        # возврат покупки не порождает отдельного вопроса про «входящие»
        assert len(qs) == 1 and qs[0].direction == "all"
        prod = session.scalar(select(Category).where(Category.name == "продукты"))
        apply_answer(session, user, qs[0].queue_id, category_id=prod.id)
        data = build_report(session, user, date(2026, 7, 1), date(2026, 7, 31))
        assert dict(data.expenses_by_category) == {"продукты": -3600000}
        assert data.income == 0
        assert data.total_expenses == -3600000

    def test_refund_reduces_budget_spent(self, session, user):
        ingest(session, user, [
            tx(date(2026, 7, 3), -4000000, cp="SMALL"),
            tx(date(2026, 7, 5), 400000, cp="SMALL"),
        ])
        autocategorize(session, user)
        prod = session.scalar(select(Category).where(Category.name == "продукты"))
        q = next_questions(session, user, 10)[0]
        apply_answer(session, user, q.queue_id, category_id=prod.id)
        set_budget(session, user, "2026-07", 10000000)
        _, spent, free = free_until_month_end(session, user, date(2026, 7, 17))
        assert spent == 3600000
        assert free == 6400000


class TestBudget:
    def test_set_and_get(self, session, user):
        set_budget(session, user, "2026-07", 30000000)
        assert get_budget(session, user, "2026-07") == 30000000
        set_budget(session, user, "2026-07", 40000000)  # перезапись
        assert get_budget(session, user, "2026-07") == 40000000

    def test_free_until_month_end(self, session, user):
        ingest(session, user, [
            tx(date(2026, 7, 2), -11000, cp="Билет Avtobys. Оплата проезда"),
            tx(date(2026, 7, 5), -489000, cp="WOLT.COM"),
        ])
        autocategorize(session, user)
        set_budget(session, user, "2026-07", 30000000)
        budget, spent, free = free_until_month_end(session, user, date(2026, 7, 17))
        assert budget == 30000000
        assert spent == 500000
        assert free == 29500000

    def test_unassigned_not_counted_in_spent(self, session, user):
        ingest(session, user, [tx(date(2026, 7, 5), -489000, cp="ИП Кто-То")])
        autocategorize(session, user)  # остаётся unassigned
        set_budget(session, user, "2026-07", 30000000)
        _, spent, _ = free_until_month_end(session, user, date(2026, 7, 17))
        assert spent == 0

    def test_no_budget_returns_none(self, session, user):
        assert free_until_month_end(session, user, date(2026, 7, 17)) is None


class TestRegularPayments:
    def test_two_months_same_counterparty(self, session, user):
        ingest(session, user, [
            tx(date(2026, 6, 29), -1125919, cp="ANTHROPIC* CLAUDE SUB"),
        ], b"s1", date(2026, 6, 1), date(2026, 6, 30))
        ingest(session, user, [
            tx(date(2026, 7, 3), -1109517, cp="ANTHROPIC* CLAUDE SUB"),
            tx(date(2026, 7, 4), -5000, cp="Разовая Покупка"),
        ], b"s2", date(2026, 7, 1), date(2026, 7, 31))
        autocategorize(session, user)
        regular = find_regular_payments(session, user)
        assert len(regular) == 1
        name, avg = regular[0]
        assert "ANTHROPIC" in name
        assert avg == (1125919 + 1109517) // 2
