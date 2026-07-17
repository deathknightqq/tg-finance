from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from finbot.categorize import apply_answer, autocategorize, next_questions
from finbot.db import init_db, make_engine, make_session_factory
from finbot.ingest import file_sha256, ingest_statement
from finbot.models import Category, QuestionQueue, Transaction, User
from finbot.netting import apply_netting_answer, find_pairs, scan_pairs
from finbot.parser import (
    ParsedStatement,
    ParsedTransaction,
    StatementHeader,
    parse_statement,
)

GOLDEN_PDF = Path(__file__).parent.parent / "data" / "gold_statement.pdf"


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


def tx(d, amount, op_type="purchase", cp="OI Market сервис онлайн доставки"):
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


def mine_expenses(session, user) -> int:
    """Сумма «моих» расходов без схлопнутых пар — как посчитает отчёт."""
    return sum(
        t.amount
        for t in session.scalars(
            select(Transaction).where(
                Transaction.user_id == user.id,
                Transaction.amount < 0,
                Transaction.ownership.in_(("mine", "unassigned")),
                Transaction.netted_with_id.is_(None),
            )
        )
    )


class TestFindPairs:
    def test_pair_within_window(self, session, user):
        ingest(session, user, [
            tx(date(2026, 7, 14), 180000),
            tx(date(2026, 7, 14), -180000),
            tx(date(2026, 7, 8), -335300),
        ])
        autocategorize(session, user)
        txs = list(session.scalars(select(Transaction)))
        pairs = find_pairs(txs)
        assert len(pairs) == 1
        p, n = pairs[0]
        assert p.amount == 180000 and n.amount == -180000

    def test_no_pair_outside_window(self, session, user):
        ingest(session, user, [
            tx(date(2026, 7, 14), 180000),
            tx(date(2026, 7, 2), -180000),  # 12 дней для покупки — не пара
        ])
        autocategorize(session, user)
        assert find_pairs(list(session.scalars(select(Transaction)))) == []

    def test_purchase_refund_within_week(self, session, user):
        ingest(session, user, [
            tx(date(2026, 7, 14), 180000),
            tx(date(2026, 7, 9), -180000),  # 5 дней — возврат покупки, пара
        ])
        autocategorize(session, user)
        assert len(find_pairs(list(session.scalars(select(Transaction))))) == 1

    def test_debt_return_week_later(self, session, user):
        """Еркебулан закинул +2000, через неделю вернул −2000 — это пара."""
        ingest(session, user, [
            tx(date(2026, 6, 14), 200000, op_type="topup", cp="Еркебұлан Б."),
            tx(date(2026, 6, 21), -200000, op_type="transfer", cp="Еркебұлан Б."),
        ])
        autocategorize(session, user)
        assert len(find_pairs(list(session.scalars(select(Transaction))))) == 1

    def test_person_transfers_not_paired_beyond_month(self, session, user):
        ingest(session, user, [
            tx(date(2026, 6, 1), 200000, op_type="topup", cp="Еркебұлан Б."),
            tx(date(2026, 7, 15), -200000, op_type="transfer", cp="Еркебұлан Б."),
        ])
        autocategorize(session, user)
        assert find_pairs(list(session.scalars(select(Transaction)))) == []

    def test_different_amounts_not_paired(self, session, user):
        ingest(session, user, [
            tx(date(2026, 7, 14), 180000),
            tx(date(2026, 7, 14), -170000),
        ])
        autocategorize(session, user)
        assert find_pairs(list(session.scalars(select(Transaction)))) == []


class TestScanAndAnswer:
    def _setup_pair(self, session, user):
        ingest(session, user, [
            tx(date(2026, 7, 14), 180000),
            tx(date(2026, 7, 14), -180000),
        ])
        autocategorize(session, user)
        return scan_pairs(session, user)

    def test_first_pair_asks_question_once(self, session, user):
        result = self._setup_pair(session, user)
        assert result.questions == 1 and result.collapsed == 0
        # повторный скан не плодит вопросы
        again = scan_pairs(session, user)
        assert again.questions == 0
        netting_qs = session.scalar(
            select(func.count(QuestionQueue.id)).where(QuestionQueue.qtype == "netting")
        )
        assert netting_qs == 1

    def test_collapse_answer_links_and_excludes(self, session, user):
        self._setup_pair(session, user)
        q = next(
            v for v in next_questions(session, user, 10) if v.qtype == "netting"
        )
        assert "↔" in q.examples[0]
        name, collapsed = apply_netting_answer(session, user, q.queue_id, "collapse")
        assert collapsed == 1
        assert mine_expenses(session, user) == 0  # пара исключена

    def test_show_answer_keeps_both(self, session, user):
        self._setup_pair(session, user)
        q = next(
            v for v in next_questions(session, user, 10) if v.qtype == "netting"
        )
        _, collapsed = apply_netting_answer(session, user, q.queue_id, "show")
        assert collapsed == 0
        assert mine_expenses(session, user) == -180000

    def test_rule_remembered_on_next_statement(self, session, user):
        self._setup_pair(session, user)
        q = next(
            v for v in next_questions(session, user, 10) if v.qtype == "netting"
        )
        apply_netting_answer(session, user, q.queue_id, "collapse")
        # вторая выписка с новой парой того же контрагента — молча схлопнулась
        ingest(session, user, [
            tx(date(2026, 8, 3), 500000),
            tx(date(2026, 8, 4), -500000),
        ], b"s2", date(2026, 8, 1), date(2026, 8, 31))
        autocategorize(session, user)
        result = scan_pairs(session, user)
        assert result.collapsed == 1 and result.questions == 0


class TestMixedDirections:
    def test_mixed_counterparty_two_questions(self, session, user):
        """Олжас: −919к транзит, +91к зарплата — размечаются раздельно."""
        ingest(session, user, [
            tx(date(2026, 7, 2), -91900000, op_type="transfer", cp="Олжас Б."),
            tx(date(2026, 6, 24), 9100000, op_type="topup", cp="Олжас Б."),
        ])
        autocategorize(session, user)
        qs = [v for v in next_questions(session, user, 10) if v.qtype == "category"]
        directions = {q.direction for q in qs}
        assert directions == {"in", "out"}

        q_out = next(q for q in qs if q.direction == "out")
        q_in = next(q for q in qs if q.direction == "in")
        assert all("−" in e for e in q_out.examples)
        assert all("+" in e for e in q_in.examples)

        r_out = apply_answer(session, user, q_out.queue_id, transit=True)
        assert r_out.affected == 1
        income = session.scalar(select(Category).where(Category.name == "доход"))
        r_in = apply_answer(session, user, q_in.queue_id, category_id=income.id)
        assert r_in.affected == 1

        out_tx = session.scalar(select(Transaction).where(Transaction.amount < 0))
        in_tx = session.scalar(select(Transaction).where(Transaction.amount > 0))
        assert out_tx.ownership == "transit"
        assert in_tx.ownership == "mine"
        assert in_tx.category_id == income.id

    def test_single_direction_stays_one_question(self, session, user):
        ingest(session, user, [
            tx(date(2026, 7, 2), -5000, cp="M market"),
            tx(date(2026, 7, 3), -7000, cp="M market"),
        ])
        autocategorize(session, user)
        qs = [v for v in next_questions(session, user, 10) if v.qtype == "category"]
        assert len(qs) == 1 and qs[0].direction == "all"


class TestCheckpoint:
    def test_monthly_stats_exclude_transit_and_pairs(self, session, user):
        """Чекпоинт чанка 5: транзит и тест-пары не в статистике."""
        ingest(session, user, [
            tx(date(2026, 7, 2), -160000000, cp="Гүлім С."),  # транзит 1.6M
            tx(date(2026, 7, 14), 180000),  # рабочая тест-пара
            tx(date(2026, 7, 14), -180000),
            tx(date(2026, 7, 5), -1500000, cp="Magnum"),  # честный расход
        ])
        autocategorize(session, user)
        scan_pairs(session, user)
        qs = next_questions(session, user, 10)
        transit_q = next(
            q for q in qs if q.qtype == "category" and "Гүлім" in q.display_name
        )
        apply_answer(session, user, transit_q.queue_id, transit=True)
        netting_q = next(q for q in qs if q.qtype == "netting")
        apply_netting_answer(session, user, netting_q.queue_id, "collapse")

        assert mine_expenses(session, user) == -1500000  # только Magnum


@pytest.mark.skipif(not GOLDEN_PDF.exists(), reason="эталонный PDF не в git")
class TestGoldenNetting:
    def test_oi_market_pair_detected(self, session, user):
        parsed = parse_statement(GOLDEN_PDF)
        ingest_statement(session, user, parsed, file_sha256(b"x"))
        autocategorize(session, user)
        result = scan_pairs(session, user)
        assert result.questions >= 1  # как минимум OI Market +1800/−1800
        oi = session.scalars(
            select(Transaction).where(
                Transaction.counterparty_raw.contains("OI Market")
            )
        ).all()
        pairs = find_pairs(oi)
        assert any(p.amount == 180000 and n.amount == -180000 for p, n in pairs)
