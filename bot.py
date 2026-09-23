import asyncio
import logging
import os
import time

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import ErrorEvent

import config
import agent_router
from handlers import payments_handlers
from update_dedup import DedupMiddleware

logger = logging.getLogger("fund4pro.bot")


# РЕАЛЬНЫЙ ИНЦИДЕНТ (23.09.2026, Render Logs, сервис fund4pro-bot): бот
# перестал отвечать на сообщения — последний Update обработан в 14:12,
# дальше 34 МИНУТЫ полная тишина в логе (ни ошибки, ни нового API-запроса),
# пока не спас случайный редеплой (пуш в этот же день). Health-check в это
# самое время продолжал отвечать 200 — событийный цикл в целом не завис
# (health-check — отдельный HTTP-хендлер, ему не помешало), завис именно
# pending-запрос getUpdates. У aiogram для этого есть встроенная защита
# (Dispatcher._listen_updates передаёт request_timeout = session.timeout(60)
# + polling_timeout(30) = 90 сек, после чего запрос должен упасть ошибкой
# и уйти в backoff-ретрай с логом "Failed to fetch updates") — по факту она
# не сработала: ни одной строки лога за все 34 минуты. Вероятная причина —
# "мёртвое" pooled-соединение в сети Render (удалённая сторона молча
# перестала отвечать без TCP RST/FIN), которое aiohttp's ClientTimeout не
# всегда ловит вовремя в такой ситуации. Раз это баг сети/aiohttp, а не
# нашего кода, чинить его изнутри aiogram нет смысла — вместо этого ловим
# зависание СНАРУЖИ, тем же приёмом, что уже использован для LLM-клиентов
# в llm.py: явный внешний таймаут вместо доверия таймауту библиотеки.
#
# _WatchdogSession обновляет last_activity при завершении КАЖДОГО запроса к
# Telegram Bot API (успешного или с ошибкой — важно само завершение, а не
# результат). _polling_watchdog ниже проверяет, не протухла ли эта метка —
# если ни один запрос не завершился дольше _STALL_THRESHOLD, значит текущий
# запрос завис так же, как в инциденте выше. Форсированный session.close()
# обрывает зависшее соединение ошибкой (aiogram сам поймает её в штатном
# backoff-цикле _listen_updates и передёт заново со свежим соединением —
# create_session() создаёт новую ClientSession, если старая закрыта), без
# необходимости убивать и перезапускать весь процесс.
_STALL_THRESHOLD = 180.0  # 3 минуты без НИ ОДНОГО завершённого API-запроса
_STALL_CHECK_INTERVAL = 30.0


class _WatchdogSession(AiohttpSession):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_activity = time.monotonic()

    async def make_request(self, *args, **kwargs):
        try:
            return await super().make_request(*args, **kwargs)
        finally:
            self.last_activity = time.monotonic()


async def _polling_watchdog(session: "_WatchdogSession") -> None:
    while True:
        await asyncio.sleep(_STALL_CHECK_INTERVAL)
        stalled_for = time.monotonic() - session.last_activity
        if stalled_for > _STALL_THRESHOLD:
            logger.error(
                "Polling watchdog: ни один запрос к Telegram Bot API не "
                "завершился за %.0f сек — похоже на зависшее соединение "
                "(см. инцидент 23.09.2026 в комментарии выше), принудительно "
                "закрываю сессию, чтобы освободить зависший запрос.",
                stalled_for,
            )
            try:
                await session.close()
            except Exception:
                logger.exception("Polling watchdog: session.close() тоже упал")
            session.last_activity = time.monotonic()


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

    bot = Bot(token=config.BOT_TOKEN, session=_WatchdogSession())
    dp = Dispatcher(storage=_build_storage())

    # Защита от повторной обработки одного и того же update_id (см.
    # update_dedup.py) — закрывает наиболее вероятный триггер бага, когда
    # один и тот же апдейт (например файл от пользователя) отрабатывался
    # дважды параллельно и второй ответ агента противоречил первому.
    dp.update.outer_middleware(DedupMiddleware())

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
    #
    # РЕАЛЬНЫЙ БАГ (найден при подключении монетизации): payments_handlers
    # ДОЛЖЕН идти ПЕРЕД agent_router. agent_router.receive_any_message — это
    # @router.message() без фильтра, то есть ловит вообще любое сообщение;
    # апдейт Telegram с successful_payment не содержит .text/.caption, так
    # что до фикса он попадал в ветку "Не увидел текста в этом сообщении" и
    # до payments_handlers.successful_payment просто не доходил — оплата
    # проходила по деньгам, но бот не увидел бы, что платить она перестала.
    dp.include_router(payments_handlers.router)
    dp.include_router(agent_router.router)

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

    # РЕАЛЬНЫЙ ИНЦИДЕНТ ("бот пропускает ответы/не отвечает", жалоба держится
    # >2 недель): render.yaml — план free, который Render усыпляет после ~15
    # минут без входящих HTTP-запросов к публичному URL. Раз в контейнер
    # ничего не стучится снаружи (Telegram long-polling — исходящие запросы
    # САМОГО бота, а не входящий трафик к Render), сервис засыпает и не
    # проснётся сам — сообщения пользователей копятся на стороне Telegram
    # (до 24ч), а drop_pending_updates=True при каждом следующем старте их
    # просто выбрасывал, вместо того чтобы обработать после пробуждения.
    # Оставляем pending-апдейты в очереди — DedupMiddleware выше уже
    # защищает от повторной обработки, так что дублирования не будет, а
    # реально пропущенные во время сна сообщения теперь будут отвечены.
    await bot.delete_webhook(drop_pending_updates=False)

    # Простой HTTP healthcheck для облачных платформ (Koyeb, Render, Railway),
    # чтобы они видели, что контейнер активен и не перезагружали его.
    port = int(os.environ.get("PORT", "8000"))
    try:
        from aiohttp import web
        health_app = web.Application()
        health_app.router.add_get("/", lambda r: web.Response(text="Bot is running!"))
        health_app.router.add_get("/health", lambda r: web.Response(text="OK"))
        runner = web.AppRunner(health_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info("Healthcheck HTTP server started on port %d", port)
    except Exception as e:
        logger.warning("Could not start healthcheck HTTP server on port %d: %s", port, e)

    asyncio.create_task(_polling_watchdog(bot.session))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
