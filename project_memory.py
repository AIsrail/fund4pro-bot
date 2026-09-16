"""Долгоживущая память о проекте пользователя — переживает /start, кнопку
"Начать заново" и повторный клик "Разработать проект" (все они вызывают
state.clear() / обнуляют project_data в agent_router.py), а также рестарт
процесса на Render.

РЕАЛЬНЫЙ ИНЦИДЕНТ: раньше для этого существовал org_profile.py, но он писал
JSON-файлы на ЛОКАЛЬНЫЙ ДИСК и — что важнее — вызывался только из старых
handlers/*.py, которые bot.py явно НЕ подключает с момента перехода на
агентную архитектуру (см. комментарий в bot.py: "Старые handlers/*.py и
states.py НЕ удалены... но больше не подключаются здесь"). В итоге
"память" была полностью мёртвым кодом в продакшене — единственный активный
роутер (agent_router.py) при каждом /start / "Начать заново" / повторном
"Разработать проект" полностью обнулял project_data и ни разу не пытался
восстановить ни org_info, ни donor_info ни откуда. Пользователь был
вынужден заново пересказывать организацию и донора при каждом новом заходе.

Эта версия хранит снепшот в Redis (тот же REDIS_URL, что и FSM-хранилище
aiogram — переживает рестарт контейнера на Render) и вызывается из
ЕДИНСТВЕННОГО активного роутера. Если REDIS_URL не задан (локальная
разработка без Redis) — работает как no-op, ничего не падает."""

import json
import logging

import config

logger = logging.getLogger("fund4pro.project_memory")

_KEY_PREFIX = "fund4pro:last_project:"
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
        logger.warning("project_memory: Redis unavailable, falling back to no-op: %s", e)
        _redis_init_failed = True
        _redis_client = None
    return _redis_client


async def save_last_project(user_id: int, project_data: dict, flow: str, donor_files: dict | None = None) -> None:
    """Best-effort — сохраняет снепшот проекта. Никогда не бросает исключение
    наружу (вызывается после каждого хода, не должно ронять ответ пользователю).

    РЕАЛЬНЫЙ ИНЦИДЕНТ: изначально сохранялся только project_data — donor_template
    (ТЕКСТ структуры формы) восстанавливался при "Продолжить проект", а вот
    chosen_donor_form/saved_donor_files (ссылка на САМ ФАЙЛ + его content_b64,
    см. agent_docgen.export_docx) — нет, потому что живут на верхнем уровне
    session, не внутри project_data. В итоге после резюмирования сборка
    документа не находила файл формы донора и молча уходила в свободный
    формат (с явным предупреждением пользователю, но результат всё равно не
    тот). donor_files — снимок именно этих верхнеуровневых ключей."""
    client = _get_client()
    if client is None:
        return
    # Не сохраняем пустышки — если из проекта известен только флоу без
    # единого реального факта, восстанавливать нечего.
    if not any(v and str(v).strip() for v in project_data.values()):
        return
    try:
        payload = json.dumps(
            {"project_data": project_data, "flow": flow, "donor_files": donor_files or {}},
            ensure_ascii=False,
        )
        await client.set(f"{_KEY_PREFIX}{user_id}", payload, ex=60 * 60 * 24 * 90)
    except Exception as e:
        logger.warning("project_memory: failed to save snapshot for user %s: %s", user_id, e)


async def load_last_project(user_id: int) -> dict | None:
    """Возвращает {"project_data": {...}, "flow": "grant"|"bizplan", "donor_files": {...}} или None."""
    client = _get_client()
    if client is None:
        return None
    try:
        raw = await client.get(f"{_KEY_PREFIX}{user_id}")
        if not raw:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("project_data"), dict):
            return None
        return data
    except Exception as e:
        logger.warning("project_memory: failed to load snapshot for user %s: %s", user_id, e)
        return None


async def clear_last_project(user_id: int) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        await client.delete(f"{_KEY_PREFIX}{user_id}")
    except Exception as e:
        logger.warning("project_memory: failed to clear snapshot for user %s: %s", user_id, e)


def summarize(project_data: dict, max_len: int = 220) -> str:
    """Короткая человекочитаемая выжимка для экрана 'Продолжить проект?'."""
    org = project_data.get("org_info", "")
    donor = project_data.get("donor_info", "")
    idea = project_data.get("problem_and_idea", "")
    parts = []
    if org:
        parts.append(f"Организация: {org[:90]}")
    if donor:
        parts.append(f"Донор: {donor[:90]}")
    if idea:
        parts.append(f"Идея: {idea[:90]}")
    text = " | ".join(parts) if parts else "есть частичные данные проекта"
    return text[:max_len] + ("…" if len(text) > max_len else "")
