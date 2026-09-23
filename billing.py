"""Учёт лимита бесплатных проектов + купленных платных кредитов (Telegram Stars).

Один free-use = один НОВЫЙ проект (новый донор), а не один сгенерированный
файл — донору может понадобиться несколько документов (форма + Excel-бюджет
и т.п.), и это не должно съедать несколько попыток за раз. Списание
происходит в agent_router._paywall_or_consume, в момент, когда пользователь
реально начинает новый проект (кнопка "Разработать проект" на пустом
аккаунте, "Начать с чистого листа" или "Новый проект, та же организация") —
НЕ при "Продолжить этот проект" (это не новая попытка, а её же продолжение).

Считается per-chat (chat_id), а не внутри FSM-сессии/project_data — иначе
счётчик сбрасывался бы вместе с project_data при каждом "Начать
заново"/новом доноре, ровно от этого лимит и должен защищать. В приватном
чате с ботом chat_id совпадает с Telegram user_id (тот же приём уже
используется в project_memory.py).

Хранится в Redis (тот же REDIS_URL, что FSM-хранилище и project_memory.py) —
переживает рестарт контейнера на Render. Без REDIS_URL (локальная
разработка без Redis) — намеренно НЕ блокируем никого: значит монетизация
физически работает только там, где задан REDIS_URL, который на проде и так
уже обязателен (иначе и project_memory, и FSM без Redis теряют данные при
каждом деплое)."""

import json
import logging

import config

logger = logging.getLogger("fund4pro.billing")

_KEY_PREFIX = "fund4pro:billing:"
_redis_client = None
_redis_init_failed = False


def _get_client():
    global _redis_client, _redis_init_failed
    if _redis_client is not None or _redis_init_failed:
        return _redis_client
    if not config.REDIS_URL:
        _redis_init_failed = True
        return None
    try:
        import redis.asyncio as redis
        _redis_client = redis.from_url(config.REDIS_URL, decode_responses=True)
    except Exception as e:
        logger.warning("billing: Redis unavailable, лимит не будет применяться: %s", e)
        _redis_init_failed = True
        _redis_client = None
    return _redis_client


async def _read(chat_id: int) -> dict:
    client = _get_client()
    if client is None:
        return {"free_used": 0, "paid_credits": 0}
    try:
        raw = await client.get(f"{_KEY_PREFIX}{chat_id}")
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.warning("billing: read failed for chat %s: %s", chat_id, e)
    return {"free_used": 0, "paid_credits": 0}


async def _write(chat_id: int, record: dict) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        await client.set(f"{_KEY_PREFIX}{chat_id}", json.dumps(record, ensure_ascii=False))
    except Exception as e:
        logger.warning("billing: write failed for chat %s: %s", chat_id, e)


async def can_start_project(chat_id: int) -> bool:
    """True — можно начать новый проект без (дополнительной) оплаты: лимит
    выключен (config.ENFORCE_FULL_VERSION_LIMIT=false), аккаунт в
    config.UNLIMITED_USER_IDS, ещё остались бесплатные попытки, либо есть
    купленный ранее платный кредит."""
    if not config.ENFORCE_FULL_VERSION_LIMIT:
        return True
    if chat_id in config.UNLIMITED_USER_IDS:
        return True
    record = await _read(chat_id)
    if record.get("free_used", 0) < config.FULL_VERSION_LIMIT:
        return True
    return record.get("paid_credits", 0) > 0


async def consume_project_start(chat_id: int) -> None:
    """Списывает одну попытку — бесплатную, если ещё есть, иначе платный
    кредит. Вызывать ТОЛЬКО сразу после успешного can_start_project(),
    непосредственно перед стартом нового проекта."""
    if chat_id in config.UNLIMITED_USER_IDS:
        return
    record = await _read(chat_id)
    if record.get("free_used", 0) < config.FULL_VERSION_LIMIT:
        record["free_used"] = record.get("free_used", 0) + 1
    else:
        record["paid_credits"] = max(record.get("paid_credits", 0) - 1, 0)
    await _write(chat_id, record)


async def add_paid_credit(chat_id: int, n: int = 1) -> None:
    """Начисляет n купленных проектов после успешной оплаты (см.
    handlers/payments_handlers.py:successful_payment)."""
    record = await _read(chat_id)
    record["paid_credits"] = record.get("paid_credits", 0) + n
    await _write(chat_id, record)


async def usage_summary(chat_id: int) -> dict:
    """{"free_used": int, "paid_credits": int} — для отладки/саппорта."""
    return await _read(chat_id)
