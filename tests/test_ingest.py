from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from finbot.db import init_db, make_engine, make_session_factory
from finbot.ingest import file_sha256, ingest_statement
from finbot.models import Category, Transaction, User
from finbot.parser import ParsedStatement, ParsedTransaction, StatementHeader
from finbot.parser import parse_statement

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


def tx(d: date, amount: int, op_type: str = "purchase", cp: str = "M market"):
    return ParsedTransaction(
        date=d, amount=amount, op_type=op_type, counterparty_raw=cp
    )


def stmt(start: date, end: date, txs: list[ParsedTransaction]) -> ParsedStatement:
    opening = 0
    return ParsedStatement(
        header=StatementHeader(
            period_start=start,
            period_end=end,
            opening_balance=opening,
            closing_balance=opening + sum(t.amount for t in txs),
        ),
        transactions=txs,
    )


def tx_count(session) -> int:
    return session.scalar(select(func.count(Transaction.id)))


class TestSeedCategories:
    def test_seeded_and_idempotent(self, session):
        names = list(
            session.scalars(select(Category.name).where(Category.user_id.is_(None)))
        )
        assert "транспорт" in names and "прочее" in names
        init_db(session.get_bind())  # повторный вызов не дублирует сид
        assert (
            session.scalar(
                select(func.count(Category.id)).where(Category.user_id.is_(None))
            )
            == len(names)
        )


class TestSameFileTwice:
    def test_second_upload_rejected_by_hash(self, session, user):
        parsed = stmt(
            date(2026, 7, 1), date(2026, 7, 7), [tx(date(2026, 7, 2), -11000)]
        )
        h = file_sha256(b"same-pdf-bytes")
        first = ingest_statement(session, user, parsed, h)
        assert first.added == 1 and not first.duplicate_file
        second = ingest_statement(session, user, parsed, h)
        assert second.duplicate_file
        assert second.added == 0
        assert tx_count(session) == 1


class TestOverlapDedup:
    def test_weekly_then_monthly(self, session, user):
        """Недельная, потом месячная с тем же куском → без потерь и без дублей."""
        weekly_txs = [
            tx(date(2026, 7, 2), -11000, cp="Билет Avtobys. Оплата проезда"),
            tx(date(2026, 7, 3), -250000, cp="WOLT.COM"),
        ]
        monthly_txs = weekly_txs + [
            tx(date(2026, 7, 15), -99900, cp="YANDEX.PLUS"),
            tx(date(2026, 7, 20), 500000, op_type="topup", cp="Аружан К."),
        ]
        ingest_statement(
            session, user,
            stmt(date(2026, 7, 1), date(2026, 7, 7), list(weekly_txs)),
            file_sha256(b"weekly"),
        )
        result = ingest_statement(
            session, user,
            stmt(date(2026, 7, 1), date(2026, 7, 31), list(monthly_txs)),
            file_sha256(b"monthly"),
        )
        assert result.added == 2
        assert result.duplicates_skipped == 2
        assert tx_count(session) == 4

    def test_monthly_then_weekly(self, session, user):
        """Обратный порядок: недельная внутри уже загруженного месяца → 0 новых."""
        monthly_txs = [
            tx(date(2026, 7, 2), -11000),
            tx(date(2026, 7, 10), -5000),
        ]
        ingest_statement(
            session, user,
            stmt(date(2026, 7, 1), date(2026, 7, 31), list(monthly_txs)),
            file_sha256(b"monthly"),
        )
        result = ingest_statement(
            session, user,
            stmt(date(2026, 7, 1), date(2026, 7, 7), [tx(date(2026, 7, 2), -11000)]),
            file_sha256(b"weekly"),
        )
        assert result.added == 0 and result.duplicates_skipped == 1
        assert tx_count(session) == 2

    def test_multiplicity_kept(self, session, user):
        """Три одинаковых Avtobys в день — норма, не дубли."""
        avtobys = lambda: tx(  # noqa: E731
            date(2026, 7, 2), -11000, cp="Билет Avtobys. Оплата проезда"
        )
        ingest_statement(
            session, user,
            stmt(date(2026, 7, 1), date(2026, 7, 7), [avtobys(), avtobys(), avtobys()]),
            file_sha256(b"weekly"),
        )
        assert tx_count(session) == 3
        # месячная содержит те же 3 + ещё одну четвёртую такую же
        result = ingest_statement(
            session, user,
            stmt(
                date(2026, 7, 1), date(2026, 7, 31),
                [avtobys(), avtobys(), avtobys(), avtobys()],
            ),
            file_sha256(b"monthly"),
        )
        assert result.duplicates_skipped == 3
        assert result.added == 1
        assert tx_count(session) == 4

    def test_other_user_not_affected(self, session, user):
        """Дедуп смотрит только на транзакции того же юзера."""
        other = User(tg_id=456, name="Партнёрша")
        session.add(other)
        session.commit()
        parsed_txs = [tx(date(2026, 7, 2), -11000)]
        ingest_statement(
            session, user,
            stmt(date(2026, 7, 1), date(2026, 7, 7), list(parsed_txs)),
            file_sha256(b"user1"),
        )
        result = ingest_statement(
            session, other,
            stmt(date(2026, 7, 1), date(2026, 7, 7), list(parsed_txs)),
            file_sha256(b"user2"),
        )
        assert result.added == 1 and result.duplicates_skipped == 0


@pytest.mark.skipif(not GOLDEN_PDF.exists(), reason="эталонный PDF не в git")
class TestGoldenStatementIngest:
    """Чекпоинт чанка 2 на реальной выписке."""

    def test_same_pdf_twice_zero_new(self, session, user):
        parsed = parse_statement(GOLDEN_PDF)
        h = file_sha256(GOLDEN_PDF.read_bytes())
        first = ingest_statement(session, user, parsed, h)
        assert first.added == 299
        second = ingest_statement(session, user, parsed, h)
        assert second.duplicate_file and second.added == 0
        assert tx_count(session) == 299

    def test_same_content_different_file_zero_new(self, session, user):
        """Тот же период, «другой» файл (другой хэш) → мультимножество ловит всё."""
        parsed = parse_statement(GOLDEN_PDF)
        ingest_statement(session, user, parsed, file_sha256(b"a"))
        result = ingest_statement(session, user, parsed, file_sha256(b"b"))
        assert result.added == 0
        assert result.duplicates_skipped == 299
        assert tx_count(session) == 299
