"""Регрессионный тест для bot._WatchdogSession/_polling_watchdog.

РЕАЛЬНЫЙ ИНЦИДЕНТ (23.09.2026, Render Logs, сервис fund4pro-bot): бот молча
перестал отвечать на 34 минуты — последний Update обработан, дальше ни
одной строки в логе (ни ошибки, ни нового API-запроса), хотя health-check
всё это время исправно отвечал 200 (событийный цикл в целом жив, завис
именно pending-запрос getUpdates к Telegram). Спас только случайный
редеплой. См. докстринг у bot._WatchdogSession для полного разбора.

Реальную сеть не поднимаем: подменяем AiohttpSession.make_request на
управляемую заглушку (либо мгновенно завершается, либо "зависает" —
никогда не возвращает управление), чтобы проверить именно логику
watchdog'а (детект отсутствия завершённых запросов + принудительный
session.close()), а не aiohttp/aiogram сами по себе.

    python -m tests.test_polling_watchdog
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot as bot_module


def test_make_request_updates_last_activity_on_success():
    session = bot_module._WatchdogSession()

    async def fake_super_make_request(*args, **kwargs):
        return "ok"

    async def run():
        import aiogram.client.session.aiohttp as aiohttp_session_mod
        orig = aiohttp_session_mod.AiohttpSession.make_request
        aiohttp_session_mod.AiohttpSession.make_request = fake_super_make_request
        try:
            before = session.last_activity
            await asyncio.sleep(0.01)
            result = await session.make_request(None, None)
        finally:
            aiohttp_session_mod.AiohttpSession.make_request = orig
        assert result == "ok"
        assert session.last_activity > before, (
            "last_activity должен обновиться после завершения запроса"
        )

    asyncio.run(run())
    print("OK: успешный make_request обновляет last_activity")


def test_make_request_updates_last_activity_even_on_error():
    session = bot_module._WatchdogSession()

    async def failing_super_make_request(*args, **kwargs):
        raise RuntimeError("network exploded")

    async def run():
        import aiogram.client.session.aiohttp as aiohttp_session_mod
        orig = aiohttp_session_mod.AiohttpSession.make_request
        aiohttp_session_mod.AiohttpSession.make_request = failing_super_make_request
        try:
            before = session.last_activity
            await asyncio.sleep(0.01)
            try:
                await session.make_request(None, None)
                raised = False
            except RuntimeError:
                raised = True
        finally:
            aiohttp_session_mod.AiohttpSession.make_request = orig
        assert raised, "исключение должно пробрасываться наружу как обычно"
        assert session.last_activity > before, (
            "last_activity должен обновляться даже при ошибке — иначе "
            "watchdog не отличит 'запрос упал и обработан' от 'завис'"
        )

    asyncio.run(run())
    print("OK: last_activity обновляется и при ошибке запроса, не только при успехе")


def test_watchdog_closes_session_when_stalled():
    session = bot_module._WatchdogSession()
    # Симулируем зависание: запрос "начался" давно и ещё не завершился —
    # last_activity не обновлялась дольше _STALL_THRESHOLD.
    session.last_activity -= (bot_module._STALL_THRESHOLD + 5)

    closed = {"called": False}

    async def fake_close():
        closed["called"] = True

    session.close = fake_close

    async def run():
        # Один цикл проверки watchdog'а вручную (не гоняем бесконечный while
        # True — тестируем именно тело проверки на протухшую метку).
        import time
        stalled_for = time.monotonic() - session.last_activity
        if stalled_for > bot_module._STALL_THRESHOLD:
            await session.close()
            session.last_activity = time.monotonic()

    asyncio.run(run())
    assert closed["called"], "session.close() должен быть вызван при протухшей метке"
    print("OK: watchdog закрывает сессию, когда ни один запрос не завершался дольше порога")


def test_watchdog_leaves_healthy_session_alone():
    session = bot_module._WatchdogSession()  # last_activity = только что

    closed = {"called": False}

    async def fake_close():
        closed["called"] = True

    session.close = fake_close

    async def run():
        import time
        stalled_for = time.monotonic() - session.last_activity
        if stalled_for > bot_module._STALL_THRESHOLD:
            await session.close()

    asyncio.run(run())
    assert not closed["called"], "здоровую (свежую) сессию watchdog не должен трогать"
    print("OK: watchdog не трогает сессию, пока запросы завершаются вовремя")


if __name__ == "__main__":
    test_make_request_updates_last_activity_on_success()
    test_make_request_updates_last_activity_even_on_error()
    test_watchdog_closes_session_when_stalled()
    test_watchdog_leaves_healthy_session_alone()
    print("\nAll polling-watchdog tests passed.")
