"""Бот FinBot: приём выписок, опросник, отчёты, бюджет, пара.

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
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

from finbot.categorize import (
    QuestionView,
    apply_answer,
    autocategorize,
    categories_for_question,
    list_categories,
    next_questions,
    pending_count,
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
    shared_category_ids,
    toggle_share,
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
    freshness_note,
    month_bounds,
    set_budget,
)

logger = logging.getLogger(__name__)

MAX_PDF_BYTES = 20 * 1024 * 1024  # лимит Bot API на скачивание файла
BATCH_SIZE = 5

BTN_REPORT = "📊 Отчёт"
BTN_WEEK = "📅 Неделя"
BTN_QUEUE = "❓ Вопросы"
BTN_BUDGET = "💰 Бюджет"

MENU_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_REPORT), KeyboardButton(text=BTN_WEEK)],
        [KeyboardButton(text=BTN_QUEUE), KeyboardButton(text=BTN_BUDGET)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

dp = Dispatcher()


class CustomCategory(StatesGroup):
    waiting_name = State()


def _session():
    return dp["session_factory"]()


async def _run(fn, *args, **kwargs):
    """Синхронная работа с базой — в тред, чтобы не держать event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def _user_of(msg_or_query, session):
    fu = msg_or_query.from_user
    return get_or_create_user(session, fu.id, fu.first_name or "без имени")


# --- команды -----------------------------------------------------------------


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я считаю личные финансы по выпискам Kaspi Gold.\n\n"
        "Пришли мне PDF-выписку (Kaspi → Карта → Выписка), и я разберу операции. "
        "Про незнакомых контрагентов задам несколько вопросов — так я учусь.\n\n"
        "Команды:\n"
        "/report — отчёт за месяц (/report неделя, /report общий)\n"
        "/budget 300000 — бюджет месяца и «свободно до конца месяца»\n"
        "/unsorted — очередь вопросов по контрагентам\n"
        "/invite — код для связки с партнёром, /join КОД — ввести код\n"
        "/share — какие категории видит партнёр\n\n"
        "Сырой PDF после разбора не хранится, персональные данные не сохраняются.",
        reply_markup=MENU_KB,
    )


async def _do_report(message: Message, mode: str) -> None:
    def work():
        with _session() as session:
            user = _user_of(message, session)
            today = date.today()
            if mode == "week":
                start, end = today - timedelta(days=6), today
                data = build_report(session, user, start, end)
                text = format_report(data, title="Отчёт за неделю")
            else:
                start, end = month_bounds(today)
                data = build_report(session, user, start, end)
                text = format_report(
                    data,
                    title=f"Отчёт за {today:%m.%Y}",
                    budget_line=free_until_month_end(session, user, today),
                    regular=find_regular_payments(session, user),
                )
                if mode == "joint":
                    shared = partner_shared_totals(session, user, start, end)
                    if shared is None:
                        text += "\n\n👥 Пара не настроена: /invite или /join КОД."
                    elif not shared:
                        text += "\n\n👥 Партнёр пока ничего не расшарил."
                    else:
                        text += "\n\n👥 Партнёр (расшаренные категории):"
                        for name, total in shared:
                            text += f"\n  {name}: {_fmt(total)}"
            note = freshness_note(session, user, today)
            if note:
                text += f"\n\n{note}"
            return text

    await message.answer(await _run(work), parse_mode="HTML")


@dp.message(Command("report"))
async def cmd_report(message: Message) -> None:
    arg = (message.text or "").split(maxsplit=1)
    mode = "month"
    if len(arg) > 1:
        word = arg[1].strip().lower()
        if word.startswith("нед"):
            mode = "week"
        elif word.startswith(("общ", "пар", "сов")):
            mode = "joint"
    await _do_report(message, mode)


@dp.message(Command("budget"))
async def cmd_budget(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)

    def work():
        with _session() as session:
            user = _user_of(message, session)
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
            user = _user_of(message, session)
            # доразметка хвостов: транзакции, загруженные до появления
            # категоризации/неттинга, попадают в очередь отсюда
            autocategorize(session, user)
            scan_pairs(session, user)
            return (
                next_questions(session, user, BATCH_SIZE),
                pending_count(session, user),
                _keyboards(session, user),
            )

    questions, total, keyboards = await _run(work)
    if not questions:
        await message.answer("Очередь пуста — все контрагенты разобраны 🎉")
        return
    await _send_batch(message, questions, total, keyboards)


# --- пара ---------------------------------------------------------------------


@dp.message(Command("invite"))
async def cmd_invite(message: Message) -> None:
    def work():
        with _session() as session:
            user = _user_of(message, session)
            try:
                code = create_invite(session, user)
            except CoupleError as e:
                return str(e)
            return (
                f"Код для партнёра: <code>{code}</code>\n"
                f"Партнёр вводит у меня: /join {code}"
            )

    await message.answer(await _run(work), parse_mode="HTML")


@dp.message(Command("join"))
async def cmd_join(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Формат: /join КОД (код даёт /invite у партнёра).")
        return

    def work():
        with _session() as session:
            user = _user_of(message, session)
            try:
                partner = join_couple(session, user, parts[1])
            except CoupleError as e:
                return str(e)
            return (
                f"Связал с партнёром: {partner.name} 👥\n"
                "Что партнёр видит, настраивается через /share "
                "(по умолчанию — ничего)."
            )

    await message.answer(await _run(work))


def _share_kb(session, user) -> InlineKeyboardMarkup:
    shared = shared_category_ids(session, user)
    rows = []
    for c in list_categories(session, user):
        mark = "✅" if c.id in shared else "🔒"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {c.name}", callback_data=f"shr:{c.id}"
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("share"))
async def cmd_share(message: Message) -> None:
    def work():
        with _session() as session:
            user = _user_of(message, session)
            if partner_of(session, user) is None:
                return None
            return _share_kb(session, user)

    kb = await _run(work)
    if kb is None:
        await message.answer("Сначала свяжись с партнёром: /invite или /join КОД.")
        return
    await message.answer(
        "Что видит партнёр (✅ — видит итоги и операции категории, 🔒 — нет):",
        reply_markup=kb,
    )


@dp.callback_query(F.data.startswith("shr:"))
async def cb_share(query: CallbackQuery) -> None:
    _, cat_id = query.data.split(":")

    def work():
        with _session() as session:
            user = _user_of(query, session)
            toggle_share(session, user, int(cat_id))
            return _share_kb(session, user)

    kb = await _run(work)
    await query.message.edit_reply_markup(reply_markup=kb)
    await query.answer()


# --- кнопки меню (регистрируются до FSM-хендлера свободного текста) ------------


@dp.message(F.text == BTN_REPORT)
async def btn_report(message: Message) -> None:
    await _do_report(message, "month")


@dp.message(F.text == BTN_WEEK)
async def btn_week(message: Message) -> None:
    await _do_report(message, "week")


@dp.message(F.text == BTN_QUEUE)
async def btn_queue(message: Message) -> None:
    await cmd_unsorted(message)


@dp.message(F.text == BTN_BUDGET)
async def btn_budget(message: Message) -> None:
    await cmd_budget(message)


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
            user = _user_of(message, session)
            reply = process_pdf(
                session, user.tg_id, user.name, pdf_bytes,
            )
            match = match_mutual_transfers(session, user)
            return (
                reply,
                match,
                next_questions(session, user, BATCH_SIZE),
                pending_count(session, user),
                _keyboards(session, user),
            )

    reply, match, questions, total, keyboards = await _run(work)
    if match.auto_matched:
        reply += f"\nВзаимных переводов пары схлопнуто: {match.auto_matched}"
    await message.answer(reply)
    for cand in match.candidates:
        await _send_match_card(message, cand)
    if questions:
        await message.answer(
            f"Спрошу про {min(BATCH_SIZE, total)} самых весомых из {total}:"
        )
        await _send_batch(message, questions, total, keyboards)


# --- опросник ----------------------------------------------------------------


def _keyboards(session, user) -> dict[str, list[tuple[int, str]]]:
    """Наборы категорий для карточек: отдельно для входящих и исходящих."""
    return {
        "in": categories_for_question(session, user, "in"),
        "out": categories_for_question(session, user, "out"),
        "all": categories_for_question(session, user, "all"),
    }


def _question_kb(
    queue_id: int, categories: list[tuple[int, str]]
) -> InlineKeyboardMarkup:
    rows, row = [], []
    for cat_id, name in categories:
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


async def _send_match_card(message: Message, cand) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👥 да, внутрисемейный",
                    callback_data=f"fam:{cand.my_tx_id}:{cand.partner_tx_id}:y",
                ),
                InlineKeyboardButton(
                    text="нет",
                    callback_data=f"fam:{cand.my_tx_id}:{cand.partner_tx_id}:n",
                ),
            ]
        ]
    )
    await message.answer(
        f"👥 Похоже на перевод внутри пары: {cand.date} {cand.amount} "
        f"↔ у {cand.partner_name} встречная операция. Это внутрисемейный "
        "перевод (не считать в статистике обоих)?",
        reply_markup=kb,
    )


async def _send_batch(
    message: Message,
    questions: list[QuestionView],
    total: int,
    keyboards: dict[str, list[tuple[int, str]]],
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
            text,
            reply_markup=_question_kb(
                q.queue_id, keyboards.get(q.direction, keyboards["all"])
            ),
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
            user = _user_of(query, session)
            return apply_answer(
                session, user, int(queue_id), category_id=int(cat_id)
            )

    try:
        result = await _run(work)
    except ValueError:
        await query.answer("Эта карточка устарела", show_alert=True)
        return
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
            user = _user_of(query, session)
            return apply_answer(session, user, int(queue_id), transit=True)

    try:
        result = await _run(work)
    except ValueError:
        await query.answer("Эта карточка устарела", show_alert=True)
        return
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
            user = _user_of(query, session)
            return apply_netting_answer(session, user, int(queue_id), rule)

    try:
        name, collapsed = await _run(work)
    except ValueError:
        await query.answer("Эта карточка устарела", show_alert=True)
        return
    if rule == "collapse":
        text = (
            f"✔ {name}: пары возвратов схлопываю и не считаю "
            f"(сейчас {collapsed})."
        )
    else:
        text = f"✔ {name}: пары показываю отдельными строками."
    await query.message.edit_text(text)
    await query.answer("Запомнил")


@dp.callback_query(F.data.startswith("fam:"))
async def cb_family(query: CallbackQuery) -> None:
    _, my_id, partner_id, answer = query.data.split(":")

    def work():
        with _session() as session:
            user = _user_of(query, session)
            confirm_match(
                session, user, int(my_id), int(partner_id), yes=(answer == "y")
            )

    try:
        await _run(work)
    except ValueError:
        await query.answer("Эта карточка устарела", show_alert=True)
        return
    if answer == "y":
        await query.message.edit_text(
            "✔ Пометил внутрисемейным — не считается в статистике обоих."
        )
    else:
        await query.message.edit_text("✔ Ок, не внутрисемейный — больше не спрошу.")
    await query.answer("Запомнил")


@dp.callback_query(F.data.startswith("skp:"))
async def cb_skip(query: CallbackQuery) -> None:
    _, queue_id = query.data.split(":")

    def work():
        with _session() as session:
            user = _user_of(query, session)
            return skip_question(session, user, int(queue_id))

    try:
        name = await _run(work)
    except ValueError:
        await query.answer("Эта карточка устарела", show_alert=True)
        return
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
            user = _user_of(message, session)
            return apply_answer(
                session, user, data["queue_id"], custom_name=message.text
            )

    try:
        result = await _run(work)
    except ValueError:
        await message.answer("Эта карточка устарела — открой очередь заново: /unsorted")
        return
    await message.answer(
        f"✔ {result.display_name} → новая категория "
        f"<b>{result.category_name}</b> ({result.affected} операц.)",
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "more")
async def cb_more(query: CallbackQuery) -> None:
    def work():
        with _session() as session:
            user = _user_of(query, session)
            return (
                next_questions(session, user, BATCH_SIZE),
                pending_count(session, user),
                _keyboards(session, user),
            )

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
    await bot.set_my_commands(
        [
            BotCommand(command="report", description="Отчёт за месяц"),
            BotCommand(command="budget", description="Бюджет месяца"),
            BotCommand(command="unsorted", description="Вопросы по контрагентам"),
            BotCommand(command="invite", description="Код для связки с партнёром"),
            BotCommand(command="join", description="Ввести код партнёра"),
            BotCommand(command="share", description="Что видит партнёр"),
            BotCommand(command="start", description="Справка"),
        ]
    )
    me = await bot.get_me()
    logger.info("Запущен бот @%s (id=%s), long polling", me.username, me.id)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
