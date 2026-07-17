"""Бот: /start, приём PDF, опросник категорий пачками по 5, /unsorted.

Запуск: python -m finbot.bot (long polling, без вебхуков).
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
from datetime import date, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

from finbot.categorize import (
    QuestionView,
    apply_answer,
    autocategorize,
    list_categories,
    next_questions,
    pending_count,
    skip_question,
)
from finbot.db import init_db, make_engine, make_session_factory
from finbot.netting import apply_netting_answer, scan_pairs
from finbot.pipeline import get_or_create_user, process_pdf
from finbot.reports import (
    _fmt_kzt as _fmt,
    build_report,
    find_regular_payments,
    format_report,
    free_until_month_end,
    month_bounds,
    set_budget,
)

logger = logging.getLogger(__name__)

MAX_PDF_BYTES = 20 * 1024 * 1024  # лимит Bot API на скачивание файла
BATCH_SIZE = 5

dp = Dispatcher()


class CustomCategory(StatesGroup):
    waiting_name = State()


def _session():
    return dp["session_factory"]()


async def _run(fn, *args, **kwargs):
    """Синхронная работа с базой — в тред, чтобы не держать event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


# --- команды -----------------------------------------------------------------


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я считаю личные финансы по выпискам Kaspi Gold.\n\n"
        "Пришли мне PDF-выписку (Kaspi → Карта → Выписка), и я разберу операции. "
        "Про незнакомых контрагентов задам несколько вопросов — так я учусь.\n\n"
        "Команды:\n"
        "/report — отчёт за месяц (/report неделя — за 7 дней)\n"
        "/budget 300000 — бюджет месяца и «свободно до конца месяца»\n"
        "/unsorted — очередь вопросов по контрагентам\n\n"
        "Сырой PDF после разбора не хранится, персональные данные не сохраняются."
    )


@dp.message(Command("report"))
async def cmd_report(message: Message) -> None:
    arg = (message.text or "").split(maxsplit=1)
    weekly = len(arg) > 1 and arg[1].strip().lower().startswith("нед")

    def work():
        with _session() as session:
            user = get_or_create_user(
                session, message.from_user.id,
                message.from_user.first_name or "без имени",
            )
            today = date.today()
            if weekly:
                start, end = today - timedelta(days=6), today
                title = "Отчёт за неделю"
                budget_line = None
            else:
                start, end = month_bounds(today)
                title = f"Отчёт за {today:%m.%Y}"
                budget_line = free_until_month_end(session, user, today)
            data = build_report(session, user, start, end)
            regular = None if weekly else find_regular_payments(session, user)
            return format_report(
                data, title=title, budget_line=budget_line, regular=regular
            )

    await message.answer(await _run(work), parse_mode="HTML")


@dp.message(Command("budget"))
async def cmd_budget(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)

    def work():
        with _session() as session:
            user = get_or_create_user(
                session, message.from_user.id,
                message.from_user.first_name or "без имени",
            )
            today = date.today()
            if len(parts) > 1:
                raw = parts[1].strip().replace(" ", "").replace("₸", "")
                try:
                    amount_kzt = int(raw)
                except ValueError:
                    return (
                        "Не понял сумму. Пример: /budget 300000 — бюджет "
                        "на текущий месяц в тенге."
                    )
                set_budget(session, user, f"{today:%Y-%m}", amount_kzt * 100)
            line = free_until_month_end(session, user, today)
            if line is None:
                return (
                    "Бюджет на этот месяц не задан. Задать: /budget 300000 "
                    "(сумма в тенге)."
                )
            budget, spent, free = line
            return (
                f"💰 Бюджет {today:%m.%Y}: {_fmt(budget)}\n"
                f"Потрачено (моё): {_fmt(spent)}\n"
                f"Свободно до конца месяца: {_fmt(free)}"
            )

    await message.answer(await _run(work))


@dp.message(Command("unsorted"))
async def cmd_unsorted(message: Message) -> None:
    def work():
        with _session() as session:
            user = get_or_create_user(
                session, message.from_user.id,
                message.from_user.first_name or "без имени",
            )
            # доразметка хвостов: транзакции, загруженные до появления
            # категоризации/неттинга, попадают в очередь отсюда
            autocategorize(session, user)
            scan_pairs(session, user)
            return next_questions(session, user, BATCH_SIZE), pending_count(
                session, user
            ), _keyboards(session, user)

    questions, total, keyboards = await _run(work)
    if not questions:
        await message.answer("Очередь пуста — все контрагенты разобраны 🎉")
        return
    await _send_batch(message, questions, total, keyboards)


# --- приём PDF ---------------------------------------------------------------


@dp.message(F.document)
async def on_document(message: Message, bot: Bot) -> None:
    doc = message.document
    is_pdf = (doc.mime_type == "application/pdf") or (
        (doc.file_name or "").lower().endswith(".pdf")
    )
    if not is_pdf:
        await message.answer("Мне нужна PDF-выписка Kaspi Gold, это не PDF.")
        return
    if doc.file_size and doc.file_size > MAX_PDF_BYTES:
        await message.answer("Файл больше 20 МБ — Telegram не даст мне его скачать.")
        return

    await message.answer("Разбираю выписку…")
    buf = io.BytesIO()
    await bot.download(doc, destination=buf)
    pdf_bytes = buf.getvalue()

    def work():
        with _session() as session:
            user = get_or_create_user(
                session, message.from_user.id,
                message.from_user.first_name or "без имени",
            )
            reply = process_pdf(
                session, message.from_user.id,
                message.from_user.first_name or "без имени", pdf_bytes,
            )
            return reply, next_questions(session, user, BATCH_SIZE), pending_count(
                session, user
            ), _keyboards(session, user)

    reply, questions, total, keyboards = await _run(work)
    await message.answer(reply)
    if questions:
        await message.answer(
            f"Спрошу про {min(BATCH_SIZE, total)} самых весомых из {total}:"
        )
        await _send_batch(message, questions, total, keyboards)


# --- опросник ----------------------------------------------------------------


def _keyboards(session, user) -> list[tuple[int, str]]:
    """(id, name) категорий для клавиатуры — считается внутри сессии."""
    return [(c.id, c.name) for c in list_categories(session, user)]


def _question_kb(
    queue_id: int, categories: list[tuple[int, str]]
) -> InlineKeyboardMarkup:
    rows, row = [], []
    for cat_id, name in categories:
        if name == "транзит":
            continue  # транзит — отдельная кнопка ниже
        row.append(
            InlineKeyboardButton(
                text=name, callback_data=f"ans:{queue_id}:{cat_id}"
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(text="🚚 транзит", callback_data=f"trn:{queue_id}"),
            InlineKeyboardButton(text="✏️ своя категория", callback_data=f"cst:{queue_id}"),
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="⏭ потом", callback_data=f"skp:{queue_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _batch_footer_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="ещё 5", callback_data="more"),
                InlineKeyboardButton(text="хватит, потом", callback_data="stop"),
            ]
        ]
    )


_DIRECTION_LABEL = {"in": " (входящие ➕)", "out": " (исходящие ➖)", "all": ""}


def _netting_kb(queue_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🧹 схлопнуть, не считать",
                    callback_data=f"net:{queue_id}:collapse",
                ),
                InlineKeyboardButton(
                    text="👁 показывать отдельно",
                    callback_data=f"net:{queue_id}:show",
                ),
            ],
            [InlineKeyboardButton(text="⏭ потом", callback_data=f"skp:{queue_id}")],
        ]
    )


async def _send_batch(
    message: Message,
    questions: list[QuestionView],
    total: int,
    keyboards: list[tuple[int, str]],
) -> None:
    for q in questions:
        examples = "\n".join(q.examples)
        if q.qtype == "netting":
            text = (
                f"↔️ <b>{q.display_name}</b> — похоже на возврат/встречные "
                f"операции ({q.tx_count} пар):\n{examples}\n"
                "Схлопнуть пару и не считать в статистике?"
            )
            await message.answer(
                text, reply_markup=_netting_kb(q.queue_id), parse_mode="HTML"
            )
            continue
        label = _DIRECTION_LABEL.get(q.direction, "")
        text = (
            f"❓ <b>{q.display_name}</b>{label} — {q.tx_count} операц.\n{examples}"
        )
        await message.answer(
            text, reply_markup=_question_kb(q.queue_id, keyboards),
            parse_mode="HTML",
        )
    remaining = total - len(questions)
    if remaining > 0:
        await message.answer(
            f"В очереди ещё {remaining}.", reply_markup=_batch_footer_kb()
        )


@dp.callback_query(F.data.startswith("ans:"))
async def cb_answer(query: CallbackQuery) -> None:
    _, queue_id, cat_id = query.data.split(":")

    def work():
        with _session() as session:
            user = get_or_create_user(
                session, query.from_user.id,
                query.from_user.first_name or "без имени",
            )
            return apply_answer(
                session, user, int(queue_id), category_id=int(cat_id)
            )

    result = await _run(work)
    await query.message.edit_text(
        f"✔ {result.display_name} → <b>{result.category_name}</b> "
        f"({result.affected} операц.)",
        parse_mode="HTML",
    )
    await query.answer("Запомнил")


@dp.callback_query(F.data.startswith("trn:"))
async def cb_transit(query: CallbackQuery) -> None:
    _, queue_id = query.data.split(":")

    def work():
        with _session() as session:
            user = get_or_create_user(
                session, query.from_user.id,
                query.from_user.first_name or "без имени",
            )
            return apply_answer(session, user, int(queue_id), transit=True)

    result = await _run(work)
    await query.message.edit_text(
        f"✔ {result.display_name} → <b>транзит</b> (исключён из статистики, "
        f"{result.affected} операц.)",
        parse_mode="HTML",
    )
    await query.answer("Запомнил")


@dp.callback_query(F.data.startswith("net:"))
async def cb_netting(query: CallbackQuery) -> None:
    _, queue_id, rule = query.data.split(":")

    def work():
        with _session() as session:
            user = get_or_create_user(
                session, query.from_user.id,
                query.from_user.first_name or "без имени",
            )
            return apply_netting_answer(session, user, int(queue_id), rule)

    name, collapsed = await _run(work)
    if rule == "collapse":
        text = (
            f"✔ {name}: пары возвратов схлопываю и не считаю "
            f"(сейчас {collapsed})."
        )
    else:
        text = f"✔ {name}: пары показываю отдельными строками."
    await query.message.edit_text(text)
    await query.answer("Запомнил")


@dp.callback_query(F.data.startswith("skp:"))
async def cb_skip(query: CallbackQuery) -> None:
    _, queue_id = query.data.split(":")

    def work():
        with _session() as session:
            user = get_or_create_user(
                session, query.from_user.id,
                query.from_user.first_name or "без имени",
            )
            return skip_question(session, user, int(queue_id))

    name = await _run(work)
    await query.message.edit_text(
        f"⏭ {name} — отложил в конец очереди (/unsorted вернёт)."
    )
    await query.answer()


@dp.callback_query(F.data.startswith("cst:"))
async def cb_custom(query: CallbackQuery, state: FSMContext) -> None:
    _, queue_id = query.data.split(":")
    await state.set_state(CustomCategory.waiting_name)
    await state.update_data(queue_id=int(queue_id))
    await query.message.answer("Напиши название новой категории:")
    await query.answer()


@dp.message(CustomCategory.waiting_name, F.text)
async def custom_category_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()

    def work():
        with _session() as session:
            user = get_or_create_user(
                session, message.from_user.id,
                message.from_user.first_name or "без имени",
            )
            return apply_answer(
                session, user, data["queue_id"], custom_name=message.text
            )

    result = await _run(work)
    await message.answer(
        f"✔ {result.display_name} → новая категория "
        f"<b>{result.category_name}</b> ({result.affected} операц.)",
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "more")
async def cb_more(query: CallbackQuery) -> None:
    def work():
        with _session() as session:
            user = get_or_create_user(
                session, query.from_user.id,
                query.from_user.first_name or "без имени",
            )
            return next_questions(session, user, BATCH_SIZE), pending_count(
                session, user
            ), _keyboards(session, user)

    questions, total, keyboards = await _run(work)
    await query.message.edit_reply_markup(reply_markup=None)
    if not questions:
        await query.message.answer("Очередь пуста — все контрагенты разобраны 🎉")
    else:
        await _send_batch(query.message, questions, total, keyboards)
    await query.answer()


@dp.callback_query(F.data == "stop")
async def cb_stop(query: CallbackQuery) -> None:
    await query.message.edit_text(
        "Ок, остальное подождёт. Вернуться к вопросам: /unsorted"
    )
    await query.answer()


# --- fallback (регистрируется последним) --------------------------------------


@dp.message()
async def fallback(message: Message) -> None:
    await message.answer("Пришли PDF-выписку Kaspi Gold или /start для справки.")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN не задан — заполни .env (см. .env.example).")

    engine = make_engine()
    init_db(engine)
    dp["session_factory"] = make_session_factory(engine)

    bot = Bot(token)
    me = await bot.get_me()
    logger.info("Запущен бот @%s (id=%s), long polling", me.username, me.id)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
