"""Обратная связь пользователей после выдачи заявки (просьба владельца
06.10.2026): несколько коротких вопросов — дал ли бот хорошие идеи, нашёл ли
нужные данные, удалось ли заполнить заявку по шаблону, оценка качества 1-5,
свободные предложения.

Хранится в Redis (тот же REDIS_URL, что billing/project_memory) списком
`fund4pro:feedback` — по одной JSON-записи на заполненный опрос, чтобы
владелец мог потом выгрузить всё разом. Дополнительно каждая запись сразу
уходит владельцу в Telegram (см. feedback_handlers.notify_owner) — без Redis
(локальная разработка) опрос всё равно работает, просто не накапливается."""

import json
import logging
import time

import config

logger = logging.getLogger("fund4pro.feedback")

REDIS_LIST_KEY = "fund4pro:feedback"

# Вопросы с вариантами-кнопками. (ключ, текст вопроса, [(код, подпись)]).
CHOICE_QUESTIONS = [
    ("ideas", "1/4 · Дал ли бот хорошие идеи для проекта?",
     [("yes", "👍 Да"), ("part", "🤔 Частично"), ("no", "👎 Нет"), ("na", "Не использовал(а)")]),
    ("data", "2/4 · Нашёл ли бот нужные данные (статистика, факты, источники)?",
     [("yes", "👍 Да"), ("part", "🤔 Частично"), ("no", "👎 Нет"), ("na", "Не нужно было")]),
    ("template", "3/4 · Удалось ли заполнить заявку по шаблону донора?",
     [("yes", "👍 Да"), ("part", "🤔 Частично"), ("no", "👎 Нет"), ("na", "Шаблона не было")]),
]
QUALITY_QUESTION = "4/4 · Оцените качество заявки от 1 до 5:"

ANSWER_LABELS = {"yes": "да", "part": "частично", "no": "нет", "na": "не применимо"}

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
        logger.warning("feedback: Redis unavailable, отзывы не накапливаются: %s", e)
        _redis_init_failed = True
    return _redis_client


async def save_feedback(chat_id: int, username: str | None, answers: dict, flow: str | None) -> None:
    record = {
        "ts": int(time.time()),
        "chat_id": chat_id,
        "username": username,
        "flow": flow,
        **{k: answers.get(k) for k in ("ideas", "data", "template", "quality", "suggestions")},
    }
    logger.info("feedback: %s", json.dumps(record, ensure_ascii=False))
    client = _get_client()
    if client is None:
        return
    try:
        await client.rpush(REDIS_LIST_KEY, json.dumps(record, ensure_ascii=False))
    except Exception as e:
        logger.warning("feedback: save failed for chat %s: %s", chat_id, e)


def format_for_owner(chat_id: int, username: str | None, answers: dict) -> str:
    def lab(key):
        v = answers.get(key)
        return ANSWER_LABELS.get(v, "—") if v else "—"

    who = f"@{username}" if username else f"chat {chat_id}"
    text = (
        f"📝 Отзыв fund4pro от {who}\n"
        f"• Идеи: {lab('ideas')}\n"
        f"• Данные найдены: {lab('data')}\n"
        f"• Заявка по шаблону: {lab('template')}\n"
        f"• Качество: {answers.get('quality') or '—'}/5"
    )
    if answers.get("suggestions"):
        text += f"\n• Предложения: {answers['suggestions'][:1500]}"
    return text


async def load_all() -> list[dict]:
    client = _get_client()
    if client is None:
        return []
    try:
        return [json.loads(x) for x in await client.lrange(REDIS_LIST_KEY, 0, -1)]
    except Exception as e:
        logger.warning("feedback: load failed: %s", e)
        return []


def summarize(records: list[dict]) -> str:
    """Короткая сводка по всем отзывам: доли ответов и средняя оценка."""
    n = len(records)
    if not n:
        return "Отзывов пока нет."
    lines = [f"Отзывов: {n}"]
    for key, label in (("ideas", "Идеи"), ("data", "Данные"), ("template", "Шаблон")):
        counts = {}
        for r in records:
            v = r.get(key)
            if v:
                counts[v] = counts.get(v, 0) + 1
        if counts:
            lines.append(f"{label}: " + ", ".join(f"{ANSWER_LABELS.get(k, k)} {v}" for k, v in counts.items()))
    q = [r["quality"] for r in records if isinstance(r.get("quality"), int)]
    if q:
        lines.append(f"Средняя оценка: {sum(q) / len(q):.2f}/5 ({len(q)} оценок)")
    sugg = sum(1 for r in records if r.get("suggestions"))
    lines.append(f"С предложениями: {sugg}")
    return "\n".join(lines)
