"""Оплата переводом + скрин чека (06.10.2026, по образцу c4faq_bot).

Пользователь упёрся в платный лимит -> бот присылает цену и реквизиты и
просит скрин чека -> чек пересылается владельцу с кнопками «Подтвердить /
Отклонить» -> после подтверждения начисляется платный кредит на нужный
ресурс (billing.add_paid_credit) и пользователю автоматически отдаётся то,
ради чего он платил (те же resume_after_* функции, что и у Telegram Stars).

Включается PAYMENT_MODE=receipt (по умолчанию) + PAYMENT_ENABLED=true.
Ожидание чека хранится в Redis с TTL 24 ч (люди не успевают перевести за
час), а не в FSM-данных: платёжный счёт выставляется из мест, где FSM
недоступен (payments.send_*_invoice(bot, chat_id)).
"""

import logging
import time

import billing
import config

logger = logging.getLogger("fund4pro.receipt_payments")

WAIT_TTL_SECONDS = 24 * 3600
_WAIT_PREFIX = "fund4pro:rcpt_wait:"
_DONE_PREFIX = "fund4pro:rcpt_done:"

# код -> (payload счёта, ресурс billing, имя настройки цены в сомах, описание)
OFFERS = {
    "pf": ("paid_file_export", "file_export", "PROJECT_FILE_PRICE_KGS", "проект в файле (заявка + бюджет)"),
    "bf": ("paid_budget_export", "budget_export", "BUDGET_EXPORT_PRICE_KGS", "бюджет в файле"),
    "pr": ("paid_new_project", "project", "PROJECT_FILE_PRICE_KGS", "ещё один проект"),
    "td": ("paid_template_download", "template_download", "PAID_TEMPLATE_DOWNLOAD_PRICE_KGS", "шаблон донора"),
}

_memory_wait: dict[int, tuple[str, float]] = {}


def code_for_payload(payload: str) -> str | None:
    for code, (p, *_rest) in OFFERS.items():
        if p == payload:
            return code
    return None


def price_kgs(code: str) -> int:
    return int(getattr(config, OFFERS[code][2]))


def owner_chat_id():
    owner = config.OWNER_CHAT_ID
    if not owner and getattr(config, "UNLIMITED_USER_IDS", None):
        owner = min(config.UNLIMITED_USER_IDS)
    return owner


def is_owner(user_id: int) -> bool:
    owner = owner_chat_id()
    return (bool(owner) and str(user_id) == str(owner)) or user_id in (getattr(config, "UNLIMITED_USER_IDS", None) or ())


async def set_waiting(chat_id: int, code: str) -> None:
    client = billing._get_client()
    if client is not None:
        try:
            await client.set(f"{_WAIT_PREFIX}{chat_id}", code, ex=WAIT_TTL_SECONDS)
            return
        except Exception:
            logger.warning("receipt: redis set_waiting failed", exc_info=True)
    _memory_wait[chat_id] = (code, time.time() + WAIT_TTL_SECONDS)


async def get_waiting(chat_id: int) -> str | None:
    client = billing._get_client()
    if client is not None:
        try:
            return await client.get(f"{_WAIT_PREFIX}{chat_id}")
        except Exception:
            logger.warning("receipt: redis get_waiting failed", exc_info=True)
    item = _memory_wait.get(chat_id)
    if item and item[1] > time.time():
        return item[0]
    return None


async def clear_waiting(chat_id: int) -> None:
    _memory_wait.pop(chat_id, None)
    client = billing._get_client()
    if client is not None:
        try:
            await client.delete(f"{_WAIT_PREFIX}{chat_id}")
        except Exception:
            logger.warning("receipt: redis clear_waiting failed", exc_info=True)


async def claim_decision(owner_chat: int, owner_message_id: int) -> bool:
    """True — решение по этому чеку ещё не принималось (защита от двойного
    клика владельца и двойного начисления кредита)."""
    client = billing._get_client()
    if client is None:
        key = (owner_chat, owner_message_id)
        if key in _memory_wait:
            return False
        _memory_wait[key] = ("done", time.time())
        return True
    try:
        return bool(await client.set(f"{_DONE_PREFIX}{owner_chat}:{owner_message_id}", "1", nx=True, ex=30 * 24 * 3600))
    except Exception:
        logger.warning("receipt: claim_decision failed, allowing", exc_info=True)
        return True


async def notify_owner(bot, text: str, **kwargs):
    owner = owner_chat_id()
    if not owner:
        logger.warning("receipt: OWNER_CHAT_ID не задан, уведомление потеряно: %s", text)
        return None
    try:
        return await bot.send_message(owner, text, **kwargs)
    except Exception:
        logger.warning("receipt: owner notify failed", exc_info=True)
        return None


async def request_receipt(bot, chat_id: int, payload: str) -> None:
    """Вместо Telegram-счёта: цена + реквизиты + просьба прислать чек."""
    code = code_for_payload(payload)
    if code is None:
        logger.warning("receipt: unknown payload %r", payload)
        return
    price = price_kgs(code)
    what = OFFERS[code][3]
    await set_waiting(chat_id, code)

    requisites = config.PAYMENT_REQUISITES or (
        "Реквизиты для перевода пришлёт владелец бота в ближайшее время."
    )
    await bot.send_message(
        chat_id,
        f"💳 Оплата: {what} — {price} сом (тестовый режим, цена может измениться).\n\n"
        f"{requisites}\n\n"
        "После перевода пришлите сюда скрин чека (фото или файл) — владелец "
        "проверит, и я сразу продолжу с того же места. Чек можно прислать в течение 24 часов.",
    )

    who = str(chat_id)
    try:
        chat = await bot.get_chat(chat_id)
        who = f"@{chat.username}" if getattr(chat, "username", None) else f"{chat.full_name or ''} (chat {chat_id})".strip()
    except Exception:
        pass
    note = "" if config.PAYMENT_REQUISITES else "\n⚠️ PAYMENT_REQUISITES не задан в Render Environment — пользователю реквизиты не показаны."
    import telemetry
    await telemetry.log_event(chat_id, "payment_requested", what=what, price=price)
    await notify_owner(bot, f"💳 {who} запросил оплату: {what}, {price} сом. Жду чек.{note}")


def wrap_invoice_function(payload: str, original):
    """Оборачивает payments.send_*_invoice: в режиме receipt вместо
    Telegram-счёта запускает оплату по чеку, остальные режимы не трогает."""
    async def wrapper(bot, chat_id, *args, **kwargs):
        if config.PAYMENT_MODE == "receipt":
            await request_receipt(bot, chat_id, payload)
            return
        return await original(bot, chat_id, *args, **kwargs)

    wrapper.__name__ = original.__name__
    wrapper.__doc__ = original.__doc__
    return wrapper
