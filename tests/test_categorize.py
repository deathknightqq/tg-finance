from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from finbot.categorize import (
    apply_answer,
    autocategorize,
    next_questions,
    normalize_name,
    pending_count,
    seed_category_name,
)
from finbot.db import init_db, make_engine, make_session_factory
from finbot.ingest import file_sha256, ingest_statement
from finbot.models import Category, QuestionQueue, Transaction, User
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


def tx(d, amount, op_type="purchase", cp="Неизвестный ИП"):
    return ParsedTransaction(date=d, amount=amount, op_type=op_type, counterparty_raw=cp)


def stmt(start, end, txs):
    return ParsedStatement(
        header=StatementHeader(
            period_start=start, period_end=end,
            opening_balance=0, closing_balance=sum(t.amount for t in txs),
        ),
        transactions=txs,
    )


def ingest(session, user, txs, blob: bytes, start=date(2026, 7, 1), end=date(2026, 7, 31)):
    ingest_statement(session, user, stmt(start, end, txs), file_sha256(blob))


class TestNormalize:
    def test_collapses_spaces_and_case(self):
        assert normalize_name("  Билет   Avtobys.  Оплата ") == "билет avtobys. оплата"


class TestSeedRules:
    @pytest.mark.parametrize(
        "name,op,expected",
        [
            ("yandex.go", "purchase", "транспорт"),
            ("билет avtobys. оплата проезда", "purchase", "транспорт"),
            ("wolt.com", "purchase", "кафе и доставка"),
            ("yandex.plus", "purchase", "подписки"),
            ("anthropic* claude sub", "purchase", "подписки"),
            ("beeline", "purchase", "связь"),
            ("аптека европа", "purchase", "аптека"),
            ("tengriphar фармация", "purchase", "аптека"),
            ("банкомат", "withdrawal", "наличка"),
            ("что угодно", "withdrawal", "наличка"),
            ("ерц астана", "purchase", "коммуналка"),
            ("ип наурызбаева", "purchase", None),
        ],
    )
    def test_mapping(self, name, op, expected):
        assert seed_category_name(name, op) == expected


class TestAutocategorize:
    def test_seed_rule_assigns_and_learns(self, session, user):
        ingest(session, user, [
            tx(date(2026, 7, 2), -11000, cp="Билет Avtobys. Оплата проезда"),
            tx(date(2026, 7, 3), -167000, cp="YANDEX.GO"),
        ], b"s1")
        result = autocategorize(session, user)
        assert result.assigned == 2 and result.queued == 0
        transport = session.scalar(select(Category).where(Category.name == "транспорт"))
        for t in session.scalars(select(Transaction)):
            assert t.category_id == transport.id
            assert t.counterparty_id is not None

    def test_unknown_goes_to_queue_with_weight(self, session, user):
        ingest(session, user, [
            tx(date(2026, 7, 2), -100000, cp="ИП ЭЛЬМИРА"),
            tx(date(2026, 7, 3), -200000, cp="ИП ЭЛЬМИРА"),
            tx(date(2026, 7, 4), -5000, cp="M market"),
        ], b"s1")
        result = autocategorize(session, user)
        assert result.queued == 2
        top = next_questions(session, user, 5)
        # ИП ЭЛЬМИРА весомее: (100000+200000) × 2 против 5000 × 1
        assert top[0].display_name == "ИП ЭЛЬМИРА"
        assert top[0].tx_count == 2
        assert len(top[0].examples) == 2

    def test_no_duplicate_questions_on_rerun(self, session, user):
        """Повторный вопрос про того же контрагента — баг."""
        ingest(session, user, [tx(date(2026, 7, 2), -5000)], b"s1")
        autocategorize(session, user)
        autocategorize(session, user)  # повторный прогон
        assert session.scalar(select(func.count(QuestionQueue.id))) == 1

    def test_scoped_to_user(self, session, user):
        other = User(tg_id=456, name="Другой")
        session.add(other)
        session.commit()
        ingest(session, user, [tx(date(2026, 7, 2), -5000)], b"s1")
        autocategorize(session, user)
        assert pending_count(session, other) == 0


class TestApplyAnswer:
    def test_category_answer_applies_to_all(self, session, user):
        ingest(session, user, [
            tx(date(2026, 7, 2), -100000, cp="ИП ЭЛЬМИРА"),
            tx(date(2026, 7, 3), -200000, cp="ИП ЭЛЬМИРА"),
        ], b"s1")
        autocategorize(session, user)
        q = next_questions(session, user, 1)[0]
        cafe = session.scalar(select(Category).where(Category.name == "кафе и доставка"))
        result = apply_answer(session, user, q.queue_id, category_id=cafe.id)
        assert result.affected == 2
        assert pending_count(session, user) == 0
        for t in session.scalars(select(Transaction)):
            assert t.category_id == cafe.id
            assert t.ownership == "mine"

    def test_transit_answer(self, session, user):
        ingest(session, user, [tx(date(2026, 7, 2), -160000000, cp="Рабочий Контрагент")], b"s1")
        autocategorize(session, user)
        q = next_questions(session, user, 1)[0]
        result = apply_answer(session, user, q.queue_id, transit=True)
        assert result.category_name == "транзит"
        t = session.scalar(select(Transaction))
        assert t.ownership == "transit"

    def test_custom_category(self, session, user):
        ingest(session, user, [tx(date(2026, 7, 2), -5000, cp="Зоомагазин Хвост")], b"s1")
        autocategorize(session, user)
        q = next_questions(session, user, 1)[0]
        result = apply_answer(session, user, q.queue_id, custom_name="кот")
        assert result.category_name == "кот"
        cat = session.scalar(select(Category).where(Category.name == "кот"))
        assert cat.user_id == user.id

    def test_learned_counterparty_not_asked_again(self, session, user):
        """Чекпоинт: вторая выписка того же юзера задаёт ≤5 новых вопросов."""
        first = [
            tx(date(2026, 7, 2), -100000, cp="ИП ЭЛЬМИРА"),
            tx(date(2026, 7, 3), -5000, cp="M market"),
            tx(date(2026, 7, 4), -11000, cp="Билет Avtobys. Оплата проезда"),
        ]
        ingest(session, user, first, b"s1", date(2026, 7, 1), date(2026, 7, 31))
        autocategorize(session, user)
        cafe = session.scalar(select(Category).where(Category.name == "кафе и доставка"))
        prod = session.scalar(select(Category).where(Category.name == "продукты"))
        for q in next_questions(session, user, 5):
            apply_answer(
                session, user, q.queue_id,
                category_id=cafe.id if "ЭЛЬМИРА" in q.display_name else prod.id,
            )
        assert pending_count(session, user) == 0

        # вторая выписка: те же контрагенты + один новый
        second = [
            tx(date(2026, 8, 2), -120000, cp="ИП ЭЛЬМИРА"),
            tx(date(2026, 8, 3), -7000, cp="M market"),
            tx(date(2026, 8, 5), -30000, cp="Новый Салон Красоты"),
        ]
        ingest(session, user, second, b"s2", date(2026, 8, 1), date(2026, 8, 31))
        result = autocategorize(session, user)
        assert result.assigned == 2  # старые узнаны словарём
        assert result.queued == 1  # вопрос только про новый салон
        assert pending_count(session, user) <= 5


@pytest.mark.skipif(not GOLDEN_PDF.exists(), reason="эталонный PDF не в git")
class TestGoldenStatementCategorize:
    def test_seed_rules_catch_known_brands(self, session, user):
        parsed = parse_statement(GOLDEN_PDF)
        ingest_statement(session, user, parsed, file_sha256(b"x"))
        result = autocategorize(session, user)
        assert result.assigned > 0
        assert result.queued > 0
        # Avtobys реально размечен транспортом
        transport = session.scalar(select(Category).where(Category.name == "транспорт"))
        avtobys = session.scalars(
            select(Transaction).where(Transaction.counterparty_raw.contains("Avtobys"))
        ).all()
        assert avtobys and all(t.category_id == transport.id for t in avtobys)
        # ANTHROPIC — подписки
        subs = session.scalar(select(Category).where(Category.name == "подписки"))
        anth = session.scalars(
            select(Transaction).where(Transaction.counterparty_raw.contains("ANTHROPIC"))
        ).all()
        assert anth and all(t.category_id == subs.id for t in anth)
        # повторный прогон не плодит вопросы
        before = session.scalar(select(func.count(QuestionQueue.id)))
        autocategorize(session, user)
        assert session.scalar(select(func.count(QuestionQueue.id))) == before
