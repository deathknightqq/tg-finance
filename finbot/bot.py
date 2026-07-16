"""Бот-каркас (чанк 3): /start, приём PDF, прогон пайплайна, ответ.

Запуск: python -m finbot.bot (long polling, без вебхуков).
"""

from __future__ import annotations

import asyncio
import io
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message
from dotenv import load_dotenv

from finbot.db import init_db, make_engine, make_session_factory
from finbot.pipeline import process_pdf

logger = logging.getLogger(__name__)

MAX_PDF_BYTES = 20 * 1024 * 1024  # лимит Bot API на скачивание файла

dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я считаю личные финансы по выпискам Kaspi Gold.\n\n"
        "Пришли мне PDF-выписку (Kaspi → Карта → Выписка), и я разберу операции. "
        "Сырой PDF после разбора не хранится, персональные данные не сохраняются."
    )


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

    session_factory = dp["session_factory"]

    def work() -> str:
        with session_factory() as session:
            return process_pdf(
                session,
                tg_id=message.from_user.id,
                name=message.from_user.first_name or "без имени",
                pdf_bytes=pdf_bytes,
            )

    reply = await asyncio.to_thread(work)
    await message.answer(reply)


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
