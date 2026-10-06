"""Телеметрия по ключевым шагам разработки (06.10.2026, просьба владельца):
видеть, где пользователи застревают, что выбирают и где платят — чтобы на
этом основании улучшать бот и методологию.

События (`kind`):
  project_started  — начат новый проект/бюджет (flow, kind)
  field_saved      — агент сохранил поле проекта (field, text — первые 300 символов)
  quick_reply      — пользователь нажал кнопку-подсказку (text, question)
  user_msg         — текст пользователя (stage, text — первые 200 символов)
  doc_delivered    — выдан документ/текст заявки (n)
  payment_requested / payment_confirmed / payment_rejected (what)
  restart          — «Начать заново»

Хранится в Redis списком `fund4pro:events` (последние MAX_EVENTS), без Redis —
в памяти процесса. ВСЕ вызовы best-effort: сбой телеметрии никогда не должен
ломать ответ пользователю."""

import json
import logging
import time

import billing

logger = logging.getLogger("fund4pro.telemetry")

REDIS_KEY = "fund4pro:events"
MAX_EVENTS = 20000
_memory: list[dict] = []

STAGE_FIELDS = (
    ("org_info", "1 организация"),
    ("donor_info", "2 донор"),
    ("problem_and_idea", "3 проблема и идея"),
    ("goal_and_objectives", "4 цели и задачи"),
    ("activities_and_budget", "5 план и бюджет"),
)


def stage_of(project_data: dict | None) -> str:
    """Текущий этап по заполненности project_data (первое незаполненное поле)."""
    pd = project_data or {}
    for field, label in STAGE_FIELDS:
        if not (pd.get(field) or "").strip():
            return label
    return "6 документ"


async def log_event(chat_id, kind: str, **data) -> None:
    try:
        record = {"ts": int(time.time()), "chat_id": chat_id, "kind": kind}
        for k, v in data.items():
            record[k] = v[:300] if isinstance(v, str) else v
        client = billing._get_client()
        if client is not None:
            await client.rpush(REDIS_KEY, json.dumps(record, ensure_ascii=False))
            await client.ltrim(REDIS_KEY, -MAX_EVENTS, -1)
        else:
            _memory.append(record)
            del _memory[:-MAX_EVENTS]
    except Exception:
        logger.warning("telemetry: log_event failed", exc_info=True)


async def load_all() -> list[dict]:
    client = billing._get_client()
    if client is None:
        return list(_memory)
    try:
        return [json.loads(x) for x in await client.lrange(REDIS_KEY, 0, -1)]
    except Exception:
        logger.warning("telemetry: load failed", exc_info=True)
        return []


def summarize(events: list[dict]) -> str:
    """Воронка по пользователям + частые выборы + оплаты."""
    if not events:
        return "Событий пока нет."
    users = {e["chat_id"] for e in events}
    by_user: dict = {}
    for e in events:
        by_user.setdefault(e["chat_id"], []).append(e)

    def reached(pred) -> int:
        return sum(1 for evs in by_user.values() if any(pred(e) for e in evs))

    lines = [f"Событий: {len(events)}, пользователей: {len(users)}", "", "Воронка (сколько пользователей дошло):"]
    lines.append(f"• начали проект: {reached(lambda e: e['kind'] == 'project_started')}")
    for field, label in STAGE_FIELDS:
        lines.append(f"• {label}: {reached(lambda e, f=field: e['kind'] == 'field_saved' and e.get('field') == f)}")
    lines.append(f"• получили документ: {reached(lambda e: e['kind'] == 'doc_delivered')}")
    lines.append(f"• запросили оплату: {reached(lambda e: e['kind'] == 'payment_requested')}")
    lines.append(f"• оплатили: {reached(lambda e: e['kind'] == 'payment_confirmed')}")
    lines.append(f"• нажали «Начать заново»: {reached(lambda e: e['kind'] == 'restart')}")

    # где пользователи пишут больше всего сообщений (признак вопросов/правок/застревания)
    stage_msgs: dict = {}
    for e in events:
        if e["kind"] == "user_msg":
            stage_msgs[e.get("stage", "?")] = stage_msgs.get(e.get("stage", "?"), 0) + 1
    if stage_msgs:
        lines += ["", "Сообщения пользователей по этапам (много = застревают/правят):"]
        lines += [f"• {k}: {v}" for k, v in sorted(stage_msgs.items())]

    picks: dict = {}
    for e in events:
        if e["kind"] == "quick_reply":
            picks[e.get("text", "")] = picks.get(e.get("text", ""), 0) + 1
    if picks:
        lines += ["", "Частые нажатия кнопок:"]
        lines += [f"• {t[:60]} — {n}" for t, n in sorted(picks.items(), key=lambda x: -x[1])[:8]]
    return "\n".join(lines)
