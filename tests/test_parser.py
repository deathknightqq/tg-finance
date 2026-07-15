from datetime import date
from pathlib import Path

import pytest

from finbot.parser import (
    GoldenRuleError,
    ParseError,
    UnsupportedLocaleError,
    parse_statement,
)
from finbot.parser.kaspi_gold import (
    _detect_locale,
    _parse_header,
    _parse_transactions,
    parse_amount,
    parse_date,
)

GOLDEN_PDF = Path(__file__).parent.parent / "data" / "gold_statement.pdf"


class TestParseAmount:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("+ 1 800,00", 180000),
            ("- 1 800,00", -180000),
            ("- 23,20", -2320),
            ("+ 95 591,09", 9559109),
            ("+ 2 202 256,67", 220225667),
            ("+ 0,00", 0),
            ("+ 1 800,00", 180000),  # nbsp-разделители
        ],
    )
    def test_ok(self, raw, expected):
        assert parse_amount(raw) == expected

    @pytest.mark.parametrize("raw", ["1 800,00", "+ 1800.00", "abc", "+ 1 800,0"])
    def test_bad(self, raw):
        with pytest.raises(ParseError):
            parse_amount(raw)


class TestParseDate:
    def test_ok(self):
        assert parse_date("14.06.26") == date(2026, 6, 14)

    @pytest.mark.parametrize("raw", ["32.06.26", "14/06/26", "14.06.2026"])
    def test_bad(self, raw):
        with pytest.raises(ParseError):
            parse_date(raw)


class TestDetectLocale:
    def test_ru(self):
        assert _detect_locale("ВЫПИСКА\nпо Kaspi Gold") == "ru"

    def test_kk(self):
        assert _detect_locale("ҮЗІНДІ КӨШІРМЕ\nKaspi Gold") == "kk"

    def test_en(self):
        assert _detect_locale("STATEMENT\nfor Kaspi Gold") == "en"

    def test_unknown(self):
        assert _detect_locale("случайный текст") is None


HEADER_TEXT = """ВЫПИСКА
по Kaspi Gold за период с 14.06.26 по 14.07.26
Доступно на 14.07.26: + 95 591,09 ₸ Валюта счета: тенге
Доступно на 14.06.26 + 274,44 ₸ Остаток зарплатных денег 0,00 ₸
Доступно на 14.07.26 + 95 591,09 ₸
"""


class TestParseHeader:
    def test_ok(self):
        h = _parse_header(HEADER_TEXT)
        assert h.period_start == date(2026, 6, 14)
        assert h.period_end == date(2026, 7, 14)
        assert h.opening_balance == 27444
        assert h.closing_balance == 9559109

    def test_no_period(self):
        with pytest.raises(ParseError, match="период"):
            _parse_header("ВЫПИСКА без периода")


def _table(*lines: str) -> list[list[str]]:
    return [["Дата Сумма Операция Детали", *lines]]


class TestParseTransactions:
    def test_basic(self):
        txs = _parse_transactions(
            _table(
                "14.07.26 - 1 040,00 ₸ Покупка M market",
                "14.07.26 + 4 500,00 ₸ Пополнение Аружан К.",
                "13.07.26 - 1 020,00 ₸ Перевод Тамирис А.",
                "10.07.26 - 300 000,00 ₸ Снятие Банкомат",
            )
        )
        assert [t.op_type for t in txs] == [
            "purchase", "topup", "transfer", "withdrawal",
        ]
        assert txs[0].amount == -104000
        assert txs[0].counterparty_raw == "M market"
        assert txs[0].date == date(2026, 7, 14)

    def test_continuation_glued(self):
        txs = _parse_transactions(
            _table(
                "03.07.26 - 11 095,17 ₸ Покупка ANTHROPIC* CLAUDE SUB",
                "(- 23,20 USD)",
                "02.07.26 - 110,00 ₸ Покупка Билет Avtobys. Оплата проезда",
            )
        )
        assert len(txs) == 2
        assert txs[0].currency_note == "- 23,20 USD"
        assert txs[1].currency_note is None

    def test_continuation_across_page_break(self):
        pages = [
            ["Дата Сумма Операция Детали",
             "03.07.26 - 11 095,17 ₸ Покупка ANTHROPIC* CLAUDE SUB"],
            ["Приложение к Справке №1238684861 от 14 июля 2026",
             "(- 23,20 USD)"],
        ]
        txs = _parse_transactions(pages)
        assert len(txs) == 1
        assert txs[0].currency_note == "- 23,20 USD"

    def test_noise_filtered(self):
        txs = _parse_transactions(
            _table(
                "14.06.26 - 200,00 ₸ Покупка Аппарат самообслуживания",
                "- Сумма заблокирована. Банк ожидает подтверждения от платежной системы.",
                "Раздел «Краткое содержание операций по карте», в строках",
                "АО «Kaspi Bank», БИК CASPKZKA, www.kaspi.kz",
                "ААОО ««KKaassppii BBaannkk»»,, ББИИКК CCAASSPPKKZZKKAA,, wwwwww..kkaassppii..kkzz",
            )
        )
        assert len(txs) == 1

    def test_unknown_line_raises(self):
        with pytest.raises(ParseError, match="Неопознанная строка"):
            _parse_transactions(
                _table("14.06.26 какая-то дичь вместо транзакции")
            )

    def test_no_table_raises(self):
        with pytest.raises(ParseError, match="таблица"):
            _parse_transactions([["просто текст"]])


class TestGoldenRuleError:
    def test_delta_and_message(self):
        err = GoldenRuleError(opening=100, closing=500, tx_sum=300, tx_count=7)
        assert err.delta == -100
        assert "7 транзакций" in str(err)


@pytest.fixture(scope="module")
def statement():
    return parse_statement(GOLDEN_PDF)


@pytest.mark.skipif(not GOLDEN_PDF.exists(), reason="эталонный PDF не в git")
class TestGoldenStatement:
    """Чекпоинт чанка 1 на реальной эталонной выписке."""

    def test_transaction_count(self, statement):
        assert len(statement.transactions) == 299

    def test_golden_rule(self, statement):
        h = statement.header
        total = sum(t.amount for t in statement.transactions)
        assert h.opening_balance + total == h.closing_balance
        assert h.opening_balance == 27444
        assert h.closing_balance == 9559109

    def test_continuations_glued(self, statement):
        noted = [t for t in statement.transactions if t.currency_note]
        assert len(noted) == 2
        assert all(t.currency_note == "- 23,20 USD" for t in noted)
        assert all("ANTHROPIC" in t.counterparty_raw for t in noted)

    def test_no_personal_data_in_result(self, statement):
        dump = repr(statement)
        assert "Кунтуаров" not in dump
        assert "000212550606" not in dump  # ИИН
        assert "KZ31722C" not in dump  # IBAN
