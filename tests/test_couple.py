from datetime import date

import pytest
from sqlalchemy import select

from finbot.categorize import (
    apply_answer,
    autocategorize,
    categories_for_question,
    next_questions,
    skip_question,
)
from finbot.couple import (
    CoupleError,
    confirm_match,
    create_invite,
    join_couple,
    match_mutual_transfers,
    partner_of,
    partner_shared_totals,
    toggle_share,
)
from finbot.db import init_db, make_engine, make_session_factory
from finbot.ingest import file_sha256, ingest_statement
from finbot.models import Category, Transaction, User
from finbot.parser import ParsedStatement, ParsedTransaction, StatementHeader
from finbot.reports import build_report, data_coverage, freshness_note


@pytest.fixture()
def session():
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def azamat(session):
    u = User(tg_id=1, name="Азамат")
    session.add(u)
    session.commit()
    return u


@pytest.fixture()
def aruzhan(session):
    u = User(tg_id=2, name="Аружан")
    session.add(u)
    session.commit()
    return u


@pytest.fixture()
def couple(session, azamat, aruzhan):
    code = create_invite(session, azamat)
    join_couple(session, aruzhan, code)
    return azamat, aruzhan


def tx(d, amount, op_type="transfer", cp="Аружан К."):
    return ParsedTransaction(date=d, amount=amount, op_type=op_type, counterparty_raw=cp)


def ingest(session, user, txs, blob, start=date(2026, 7, 1), end=date(2026, 7, 31)):
    parsed = ParsedStatement(
        header=StatementHeader(
            period_start=start, period_end=end,
            opening_balance=0, closing_balance=sum(t.amount for t in txs),
        ),
        transactions=txs,
    )
    ingest_statement(session, user, parsed, file_sha256(blob))


class TestInviteJoin:
    def test_happy_path(self, session, azamat, aruzhan):
        code = create_invite(session, azamat)
        partner = join_couple(session, aruzhan, code)
        assert partner.id == azamat.id
        assert azamat.couple_id == aruzhan.couple_id == azamat.id
        assert partner_of(session, azamat).id == aruzhan.id
        assert partner_of(session, aruzhan).id == azamat.id

    def test_join_own_code(self, session, azamat):
        code = create_invite(session, azamat)
        with pytest.raises(CoupleError, match="собственный"):
            join_couple(session, azamat, code)

    def test_bad_code(self, session, azamat):
        with pytest.raises(CoupleError, match="не найден"):
            join_couple(session, azamat, "XXXXXX")

    def test_already_coupled(self, session, couple):
        azamat, aruzhan = couple
        third = User(tg_id=3, name="Третий")
        session.add(third)
        session.commit()
        with pytest.raises(CoupleError, match="уже в паре"):
            create_invite(session, azamat)
        code = create_invite(session, third)
        with pytest.raises(CoupleError, match="уже в паре"):
            join_couple(session, aruzhan, code)

    def test_code_case_insensitive(self, session, azamat, aruzhan):
        code = create_invite(session, azamat)
        join_couple(session, aruzhan, code.lower())
        assert partner_of(session, aruzhan) is not None


class TestMutualTransfers:
    def test_full_match_auto_intrafamily(self, session, couple):
        azamat, aruzhan = couple
        ingest(session, azamat, [tx(date(2026, 7, 5), -450000, cp="Аружан К.")], b"a")
        ingest(
            session, aruzhan,
            [tx(date(2026, 7, 5), 450000, op_type="topup", cp="Азамат К.")],
            b"b",
        )
        result = match_mutual_transfers(session, azamat)
        assert result.auto_matched == 1
        assert result.candidates == []
        for t in session.scalars(select(Transaction)):
            assert t.ownership == "intrafamily"
            assert t.matched_tx_id is not None
        # исключено из статистики обоих
        for u in (azamat, aruzhan):
            data = build_report(session, u, date(2026, 7, 1), date(2026, 7, 31))
            assert data.total_expenses == 0 and data.income == 0

    def test_partial_match_asks(self, session, couple):
        azamat, aruzhan = couple
        # сумма и дата сходятся, но имя контрагента чужое — вопрос, не авто
        ingest(session, azamat, [tx(date(2026, 7, 5), -450000, cp="Гүлім С.")], b"a")
        ingest(
            session, aruzhan,
            [tx(date(2026, 7, 5), 450000, op_type="topup", cp="Азамат К.")],
            b"b",
        )
        result = match_mutual_transfers(session, azamat)
        assert result.auto_matched == 0
        assert len(result.candidates) == 1

    def test_confirm_yes_links(self, session, couple):
        azamat, aruzhan = couple
        ingest(session, azamat, [tx(date(2026, 7, 5), -450000, cp="Гүлім С.")], b"a")
        ingest(
            session, aruzhan,
            [tx(date(2026, 7, 5), 450000, op_type="topup", cp="Азамат К.")],
            b"b",
        )
        cand = match_mutual_transfers(session, azamat).candidates[0]
        confirm_match(session, azamat, cand.my_tx_id, cand.partner_tx_id, yes=True)
        my = session.get(Transaction, cand.my_tx_id)
        assert my.ownership == "intrafamily" and my.matched_tx_id is not None

    def test_decline_not_asked_again(self, session, couple):
        azamat, aruzhan = couple
        ingest(session, azamat, [tx(date(2026, 7, 5), -450000, cp="Гүлім С.")], b"a")
        ingest(
            session, aruzhan,
            [tx(date(2026, 7, 5), 450000, op_type="topup", cp="Азамат К.")],
            b"b",
        )
        cand = match_mutual_transfers(session, azamat).candidates[0]
        confirm_match(session, azamat, cand.my_tx_id, cand.partner_tx_id, yes=False)
        again = match_mutual_transfers(session, azamat)
        assert again.candidates == [] and again.auto_matched == 0

    def test_no_match_outside_window(self, session, couple):
        azamat, aruzhan = couple
        ingest(session, azamat, [tx(date(2026, 7, 5), -450000)], b"a")
        ingest(
            session, aruzhan,
            [tx(date(2026, 7, 8), 450000, op_type="topup", cp="Азамат К.")],
            b"b",
        )
        result = match_mutual_transfers(session, azamat)
        assert result.auto_matched == 0 and result.candidates == []

    def test_solo_user_noop(self, session, azamat):
        ingest(session, azamat, [tx(date(2026, 7, 5), -450000)], b"a")
        result = match_mutual_transfers(session, azamat)
        assert result.auto_matched == 0 and result.candidates == []


class TestSharing:
    def test_partner_sees_only_shared(self, session, couple):
        azamat, aruzhan = couple
        ingest(
            session, aruzhan,
            [
                ParsedTransaction(
                    date=date(2026, 7, 3), amount=-200000,
                    op_type="purchase", counterparty_raw="WOLT.COM",
                ),
                ParsedTransaction(
                    date=date(2026, 7, 4), amount=-90000,
                    op_type="purchase", counterparty_raw="Аптека Европа",
                ),
            ],
            b"b",
        )
        autocategorize(session, aruzhan)  # wolt → кафе, аптека → аптека
        cafe = session.scalar(select(Category).where(Category.name == "кафе и доставка"))
        # Аружан шарит только кафе
        assert toggle_share(session, aruzhan, cafe.id) is True
        shared = partner_shared_totals(
            session, azamat, date(2026, 7, 1), date(2026, 7, 31)
        )
        assert shared == [("кафе и доставка", -200000)]
        # выключила — Азамат не видит ничего
        assert toggle_share(session, aruzhan, cafe.id) is False
        assert partner_shared_totals(
            session, azamat, date(2026, 7, 1), date(2026, 7, 31)
        ) == []

    def test_no_couple_returns_none(self, session, azamat):
        assert partner_shared_totals(
            session, azamat, date(2026, 7, 1), date(2026, 7, 31)
        ) is None


class TestUxFixes:
    def test_skip_answered_question_raises(self, session, azamat):
        ingest(session, azamat, [
            ParsedTransaction(
                date=date(2026, 7, 2), amount=-5000,
                op_type="purchase", counterparty_raw="ИП Кто-То",
            )
        ], b"a")
        autocategorize(session, azamat)
        q = next_questions(session, azamat, 1)[0]
        cafe = session.scalar(select(Category).where(Category.name == "кафе и доставка"))
        apply_answer(session, azamat, q.queue_id, category_id=cafe.id)
        with pytest.raises(ValueError, match="уже отвечен"):
            skip_question(session, azamat, q.queue_id)

    def test_keyboard_out_hides_income(self, session, azamat):
        names = [n for _, n in categories_for_question(session, azamat, "out")]
        assert "доход" not in names and "транзит" not in names
        assert "продукты" in names

    def test_keyboard_in_compact(self, session, azamat):
        names = [n for _, n in categories_for_question(session, azamat, "in")]
        assert "доход" in names and "семья" in names
        assert "коммуналка" not in names  # расходный шум скрыт

    def test_keyboard_sorted_by_usage(self, session, azamat):
        ingest(session, azamat, [
            ParsedTransaction(
                date=date(2026, 7, d), amount=-5000,
                op_type="purchase", counterparty_raw="WOLT.COM",
            )
            for d in range(1, 6)
        ], b"a")
        autocategorize(session, azamat)  # 5 операций в «кафе и доставка»
        names = [n for _, n in categories_for_question(session, azamat, "out")]
        assert names[0] == "кафе и доставка"

    def test_freshness_note(self, session, azamat):
        assert "Данных пока нет" in freshness_note(session, azamat, date(2026, 7, 17))
        ingest(session, azamat, [
            ParsedTransaction(
                date=date(2026, 7, 2), amount=-5000,
                op_type="purchase", counterparty_raw="ИП",
            )
        ], b"a", start=date(2026, 6, 14), end=date(2026, 7, 10))
        assert data_coverage(session, azamat) == date(2026, 7, 10)
        note = freshness_note(session, azamat, date(2026, 7, 17))
        assert "10.07.26" in note and "актуализировать" in note
        assert freshness_note(session, azamat, date(2026, 7, 10)) is None
