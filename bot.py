import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import ErrorEvent

import config
import agent_router
from handlers import payments_handlers

logger = logging.getLogger("fund4pro.bot")


def _build_storage():
    """Redis, если задан REDIS_URL (для продакшена с несколькими
    инстансами/масштабированием); иначе FileStorage (JSON-файлы на диске) —
    переживает перезапуск процесса без внешних зависимостей."""
    if not config.REDIS_URL:
        from file_storage import FileStorage
        return FileStorage()
    try:
        from aiogram.fsm.storage.redis import RedisStorage
    except ImportError as exc:
        raise RuntimeError(
            "REDIS_URL задан, но пакет aiogram[redis] не установлен. "
            "Установите: pip install 'aiogram[redis]'"
        ) from exc
    return RedisStorage.from_url(config.REDIS_URL)


async def main():
    logging.basicConfig(level=logging.INFO)

    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан (переменная окружения)")
    if not config.ANTHROPIC_API_KEY and not config.GEMINI_API_KEY:
        raise RuntimeError("Ни ANTHROPIC_API_KEY, ни GEMINI_API_KEY не заданы — нужен хотя бы один провайдер модели")
    if config.PAYMENT_ENABLED and not config.PROVIDER_TOKEN:
        logging.warning(
            "PAYMENT_ENABLED=true, но PROVIDER_TOKEN пуст — будет "
            "использован режим Telegram Stars (currency=XTR)."
        )

    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher(storage=_build_storage())

    # РЕДИЗАЙН (v3): единый агентный роутер вместо ~13 файлов handlers/*.py,
    # каждый из которых был изолированным FSM-шагом с узким LLM-промптом на
    # конкретный шаг (жёсткая структура, которую пользователь справедливо
    # называл "рамками" — модель никогда не видела весь путь целиком и не
    # могла сама решить, что спросить дальше). Теперь один системный промпт
    # (agent_roadmap.py) содержит всю методологию сразу, а модель сама
    # решает, когда что спросить/сохранить/сгенерировать через tool calling
    # (agent_tools.py, agent_engine.py) — код только исполняет её вызовы.
    # Старые handlers/*.py и states.py НЕ удалены (оставлены как référence/
    # откат), но больше не подключаются здесь.
    dp.include_router(agent_router.router)
    dp.include_router(payments_handlers.router)

    # РЕАЛЬНЫЙ ИНЦИДЕНТ: необработанное исключение в любом хендлере роняло
    # обработку апдейта ПОЛНОСТЬЮ МОЛЧА — пользователь не получал вообще
    # никакого ответа. Глобальный обработчик — последний рубеж.
    @dp.errors()
    async def on_unhandled_error(event: ErrorEvent):
        logger.exception(
            "Unhandled exception while processing update: %s", event.exception
        )
        update = event.update
        chat_id = None
        if update.message:
            chat_id = update.message.chat.id
        elif update.callback_query and update.callback_query.message:
            chat_id = update.callback_query.message.chat.id
        if chat_id is not None:
            try:
                await bot.send_message(
                    chat_id,
                    "⚠️ Произошла техническая ошибка при обработке твоего "
                    "сообщения. Попробуй повторить его ещё раз — если не "
                    "поможет, начни заново кнопкой ниже.",
                )
            except Exception:
                pass  # даже это может не сработать — не роняем обработчик ошибок из-за этого
        return True

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
