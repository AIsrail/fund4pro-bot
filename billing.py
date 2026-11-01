"""Учёт лимитов бесплатного использования + купленных платных кредитов
(Telegram Stars) — по нескольким НЕЗАВИСИМЫМ ресурсам, не одному общему счётчику.

РЕАЛЬНАЯ ПРОСЬБА ВЛАДЕЛЬЦА (25.09.2026): бесплатный тест бота должен давать
РАЗНЫЕ вещи бесплатно в разном объёме — идеи проекта всегда бесплатно (не
ресурс вообще), скачивание официального шаблона донора — 1 раз бесплатно,
дальше платно, а сборка итогового файла ПО ШАБЛОНУ донора (не просто текст
в чате) — платно с самого начала, отдельно от лимита на количество проектов.
Один общий счётчик "free_used/paid_credits" этого не умеет — отсюда
"resource: str" параметр везде ниже. Ресурсы сейчас:
  - "project"           — старый лимит: сколько НОВЫХ проектов можно начать.
  - "template_download" — сколько РАЗ можно скачать официальный шаблон
                           формы донора (не считается, если скачать не
                           удалось — см. agent_engine._execute_tool,
                           списание происходит только после успеха).
  - "budget_export"      — бюджет в файле (режим «Составить бюджет»):
                           1 бесплатно, дальше платно (config.FREE_BUDGET_EXPORTS).
  - "file_export"        — сборка итогового документа В ОФИЦИАЛЬНЫЙ ФАЙЛ
                           шаблона (не текст в чате) — по умолчанию 0
                           бесплатных, платно с первого раза.
Каждый ресурс использует СВОИ лимиты/флаги из config.py (см. RESOURCE_LIMITS
ниже) — довольно списывается через тот же (chat_id, resource) API, так что
добавление ЕЩЁ одного платного действия в будущем не требует менять форму
хранения, только дописать запись в RESOURCE_LIMITS.

Один free-use "project" = один НОВЫЙ проект (новый донор), а не один
сгенерированный файл — донору может понадобиться несколько документов
(форма + Excel-бюджет и т.п.), и это не должно съедать несколько попыток
за раз. Списание происходит в agent_router._paywall_or_consume, в момент,
когда пользователь реально начинает новый проект (кнопка "Разработать
проект" на пустом аккаунте, "Начать с чистого листа" или "Новый проект, та
же организация") — НЕ при "Продолжить этот проект" (это не новая попытка,
а её же продолжение).

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

_EMPTY_RESOURCE = {"free_used": 0, "paid_credits": 0}


def _resource_limits(resource: str) -> tuple[bool, int]:
    """(enforce, free_limit) для ресурса — читает config.py по имени, чтобы
    не размазывать if/elif по всему модулю при добавлении нового ресурса."""
    if resource == "project":
        return config.ENFORCE_FULL_VERSION_LIMIT, config.FULL_VERSION_LIMIT
    if resource == "template_download":
        return config.ENFORCE_TEMPLATE_DOWNLOAD_LIMIT, config.FREE_TEMPLATE_DOWNLOADS
    if resource == "file_export":
        return config.ENFORCE_FILE_EXPORT_PAYMENT and config.PAYMENT_ENABLED, config.FREE_FILE_EXPORTS
    if resource == "budget_dev":
        return config.ENFORCE_BUDGET_DEV_LIMIT and config.PAYMENT_ENABLED, config.FREE_BUDGET_DEVS
    if resource == "budget_export":
        # Без включённой оплаты второй файл заблокировать нечем (счёт не отправить) —
        # поэтому ограничение действует только при PAYMENT_ENABLED=true.
        return config.ENFORCE_BUDGET_EXPORT_PAYMENT and config.PAYMENT_ENABLED, config.FREE_BUDGET_EXPORTS
    raise ValueError(f"billing: unknown resource {resource!r}")


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
        logger.warning("billing: Redis unavailable, лимиты не будут применяться: %s", e)
        _redis_init_failed = True
        _redis_client = None
    return _redis_client


async def _read(chat_id: int) -> dict:
    """{"project": {"free_used","paid_credits"}, "template_download": {...},
    "file_export": {...}} — недостающие ресурсы достраиваются пустыми, так
    что вызывающему коду никогда не нужно проверять наличие ключа."""
    client = _get_client()
    record = {}
    if client is not None:
        try:
            raw = await client.get(f"{_KEY_PREFIX}{chat_id}")
            if raw:
                data = json.loads(raw)
                if isinstance(data, dict):
                    record = data
        except Exception as e:
            logger.warning("billing: read failed for chat %s: %s", chat_id, e)

    # МИГРАЦИЯ старого плоского формата ({"free_used","paid_credits"} без
    # вложенности по ресурсу, до 25.09.2026 — единственным ресурсом был
    # "project") — переносим один раз в новую форму, ничего не теряя.
    if "free_used" in record or "paid_credits" in record:
        record = {"project": {
            "free_used": record.get("free_used", 0),
            "paid_credits": record.get("paid_credits", 0),
        }}

    for res in ("project", "template_download", "file_export", "budget_export", "budget_dev"):
        record.setdefault(res, dict(_EMPTY_RESOURCE))
    return record


async def _write(chat_id: int, record: dict) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        await client.set(f"{_KEY_PREFIX}{chat_id}", json.dumps(record, ensure_ascii=False))
    except Exception as e:
        logger.warning("billing: write failed for chat %s: %s", chat_id, e)


async def can_use(chat_id: int, resource: str) -> bool:
    """True — можно воспользоваться ресурсом без (дополнительной) оплаты:
    лимит для него выключен, аккаунт в config.UNLIMITED_USER_IDS, ещё
    остались бесплатные попытки этого ресурса, либо есть купленный ранее
    платный кредит именно на него."""
    enforce, free_limit = _resource_limits(resource)
    if not enforce:
        return True
    if chat_id in config.UNLIMITED_USER_IDS:
        return True
    record = await _read(chat_id)
    bucket = record[resource]
    if bucket.get("free_used", 0) < free_limit:
        return True
    return bucket.get("paid_credits", 0) > 0


async def consume(chat_id: int, resource: str) -> None:
    """Списывает одно использование ресурса — бесплатное, если ещё есть,
    иначе платный кредит именно этого ресурса. Вызывать ТОЛЬКО сразу после
    успешного can_use() (или, для template_download, ПОСЛЕ подтверждённого
    успешного скачивания — см. agent_engine.py, деньги/бесплатная попытка
    не должны списываться за неудачную попытку)."""
    if chat_id in config.UNLIMITED_USER_IDS:
        return
    _, free_limit = _resource_limits(resource)
    record = await _read(chat_id)
    bucket = record[resource]
    if bucket.get("free_used", 0) < free_limit:
        bucket["free_used"] = bucket.get("free_used", 0) + 1
    else:
        bucket["paid_credits"] = max(bucket.get("paid_credits", 0) - 1, 0)
    await _write(chat_id, record)


async def add_paid_credit(chat_id: int, resource: str, n: int = 1) -> None:
    """Начисляет n купленных использований РЕСУРСА после успешной оплаты
    (см. handlers/payments_handlers.py:successful_payment)."""
    record = await _read(chat_id)
    record[resource]["paid_credits"] = record[resource].get("paid_credits", 0) + n
    await _write(chat_id, record)


async def usage_summary(chat_id: int) -> dict:
    """Полная запись по всем ресурсам — для отладки/саппорта."""
    return await _read(chat_id)


# --- Обратная совместимость (project — самый старый и пока единственный
# вызываемый из agent_router.py ресурс) -------------------------------------

async def can_start_project(chat_id: int) -> bool:
    return await can_use(chat_id, "project")


async def consume_project_start(chat_id: int) -> None:
    await consume(chat_id, "project")


# --- Платежи по конкурсам (28.10.2026) -----------------------------------------------

async def get_contest_count(chat_id: int) -> int:
    """Сколько уникальных конкурсов уже использовал юзер?
    Не обнуляется (отличие от месячного лимита /code)."""
    client = _get_client()
    if client is None:
        return 0
    key = f"{_KEY_PREFIX}donor_contests:{chat_id}"
    try:
        contests = await client.hgetall(key)
        return len(contests)
    except Exception:
        logger.exception(f"billing.get_contest_count failed for {chat_id}")
        return 0


async def check_contest_quota(chat_id: int, contest_id: str, has_code_access: bool = False) -> dict:
    """Проверить квоту для конкретного конкурса.

    Returns:
      {
        "contest_number": 1,      # На каком по счёту конкурсе находится
        "is_paid_access": bool,   # Требует ли оплата доступа (3-й+ конкурс)?
        "projects_used": int,     # Сколько проектов уже разработано под этим
        "can_start": bool,        # Может ли юзер стартовать проект сейчас?
        "reason": str,            # Если не может — почему
      }
    """
    # /code (платный тариф) обходит все лимиты
    if has_code_access:
        return {
            "contest_number": 1,
            "is_paid_access": False,
            "projects_used": 0,
            "can_start": True,
            "reason": "code_access",
        }

    client = _get_client()
    if client is None:
        return {
            "contest_number": 1,
            "is_paid_access": False,
            "projects_used": 0,
            "can_start": True,
            "reason": "redis_unavailable",
        }

    try:
        # Считаем сколько всего конкурсов использовано
        contests_key = f"{_KEY_PREFIX}donor_contests:{chat_id}"
        contests = await client.hgetall(contests_key)
        total_contests = len(contests)

        # Номер конкурса (1-based)
        if contest_id not in contests:
            # Новый конкурс
            contest_number = total_contests + 1
            projects_used = 0
        else:
            # Конкурс уже был использован
            contest_number = int(contests[contest_id].get("order", total_contests + 1))
            projects_used = int(contests[contest_id].get("projects_count", 0))

        # Требуется ли оплата доступа к конкурсу?
        is_paid_access = contest_number > config.FREE_CONTESTS

        # Проверяем платные концерты — оплачены ли они?
        if is_paid_access:
            paid_contests_key = f"{_KEY_PREFIX}paid_contests:{chat_id}"
            paid_contests = await client.smembers(paid_contests_key)
            if contest_id not in paid_contests:
                return {
                    "contest_number": contest_number,
                    "is_paid_access": True,
                    "projects_used": projects_used,
                    "can_start": False,
                    "reason": "paid_contest_access_required",
                }

        # Проверяем лимит проектов под этим конкурсом
        can_start = projects_used < config.PROJECTS_PER_CONTEST_FREE
        if not can_start and not is_paid_access:
            can_start_reason = "projects_limit_exceeded"
        elif not can_start:
            can_start_reason = "projects_limit_exceeded_paid_contest"
        else:
            can_start_reason = "ok"

        return {
            "contest_number": contest_number,
            "is_paid_access": is_paid_access,
            "projects_used": projects_used,
            "can_start": can_start,
            "reason": can_start_reason,
        }
    except Exception:
        logger.exception(f"billing.check_contest_quota failed for {chat_id}, {contest_id}")
        return {
            "contest_number": 1,
            "is_paid_access": False,
            "projects_used": 0,
            "can_start": True,
            "reason": "error_fallback_allow",
        }


async def register_contest_usage(chat_id: int, contest_id: str) -> None:
    """Регистрирует использование конкурса и увеличивает счётчик проектов под ним."""
    client = _get_client()
    if client is None:
        return

    try:
        contests_key = f"{_KEY_PREFIX}donor_contests:{chat_id}"
        contests = await client.hgetall(contests_key)

        if contest_id not in contests:
            # Новый конкурс — записываем
            contest_order = len(contests) + 1
            await client.hset(contests_key, contest_id, json.dumps({
                "order": contest_order,
                "projects_count": 1,
            }))
        else:
            # Увеличиваем счётчик проектов
            existing = json.loads(contests[contest_id])
            existing["projects_count"] = existing.get("projects_count", 0) + 1
            await client.hset(contests_key, contest_id, json.dumps(existing))
    except Exception:
        logger.exception(f"billing.register_contest_usage failed for {chat_id}, {contest_id}")


async def mark_contest_paid(chat_id: int, contest_id: str) -> None:
    """Отмечает, что юзер оплатил доступ к конкурсу (#3+)."""
    client = _get_client()
    if client is None:
        return

    try:
        paid_contests_key = f"{_KEY_PREFIX}paid_contests:{chat_id}"
        await client.sadd(paid_contests_key, contest_id)
    except Exception:
        logger.exception(f"billing.mark_contest_paid failed for {chat_id}, {contest_id}")
