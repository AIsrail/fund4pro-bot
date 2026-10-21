"""Бонус покупателям книги «Как привлечь денежный грант» (решение владельца
21.10.2026): те, кто пришёл из @c4faq_bot по ссылке t.me/fund4_bot?start=book,
получают config.BOOK_FREE_TRANSFERS бесплатных переносов проекта в шаблон донора
(Word/Excel). Реализовано как платные кредиты ресурса file_export — ровно тот
механизм, который списывается при переносе, поэтому отдельной логики в
месте выдачи файла не нужно.

Метка в ссылке не доказывает покупку (любой, у кого есть ссылка, может её
открыть) — осознанный компромисс при небольшой выгоде. Выдаётся один раз на
чат (SETNX в Redis; без Redis — в памяти процесса)."""

import logging

import billing
import config

logger = logging.getLogger("fund4pro.book_bonus")

START_PAYLOAD = "book"
_KEY_PREFIX = "fund4pro:book:"
_memory_granted: set[int] = set()


async def grant_book_bonus(chat_id: int) -> bool:
    """True — бонус выдан сейчас; False — уже выдавался этому чату."""
    client = billing._get_client()
    granted_now = False
    if client is not None:
        try:
            granted_now = bool(await client.set(f"{_KEY_PREFIX}{chat_id}", "1", nx=True))
        except Exception:
            logger.warning("book_bonus: redis failed, using memory fallback", exc_info=True)
            client = None
    if client is None:
        granted_now = chat_id not in _memory_granted
        _memory_granted.add(chat_id)
    if granted_now:
        await billing.add_paid_credit(chat_id, "file_export", config.BOOK_FREE_TRANSFERS)
    return granted_now


BOOK_WELCOME = (
    "📚 Вы пришли от @c4faq_bot — спасибо, что читаете книгу! Для вас перенос "
    "{n} проектов в шаблон донора (Word/Excel) бесплатный. Сама разработка проекта "
    "и бюджета бесплатна для первых 2 проектов и 2 бюджетов."
)
