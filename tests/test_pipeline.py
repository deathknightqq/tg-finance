from pathlib import Path

import pytest
from sqlalchemy import select

from finbot.db import init_db, make_engine, make_session_factory
from finbot.models import User
from finbot.pipeline import get_or_create_user, process_pdf

GOLDEN_PDF = Path(__file__).parent.parent / "data" / "gold_statement.pdf"


@pytest.fixture()
def session():
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


class TestGetOrCreateUser:
    def test_creates_once(self, session):
        u1 = get_or_create_user(session, 42, "Азамат")
        u2 = get_or_create_user(session, 42, "Азамат")
        assert u1.id == u2.id
        assert len(list(session.scalars(select(User)))) == 1


class TestProcessPdf:
    def test_garbage_bytes_rejected(self, session):
        reply = process_pdf(session, 42, "Тест", b"not a pdf at all")
        assert reply.startswith("⛔")

    @pytest.mark.skipif(not GOLDEN_PDF.exists(), reason="эталонный PDF не в git")
    def test_golden_statement_end_to_end(self, session):
        reply = process_pdf(session, 42, "Тест", GOLDEN_PDF.read_bytes())
        assert "Новых операций: 299" in reply
        assert "баланс сошёлся" in reply

    @pytest.mark.skipif(not GOLDEN_PDF.exists(), reason="эталонный PDF не в git")
    def test_same_file_twice(self, session):
        pdf = GOLDEN_PDF.read_bytes()
        process_pdf(session, 42, "Тест", pdf)
        reply = process_pdf(session, 42, "Тест", pdf)
        assert "уже загружали" in reply
