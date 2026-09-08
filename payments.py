"""Платёжная заготовка (ВЫКЛЮЧЕНА по умолчанию, config.PAYMENT_ENABLED=false).

Реализует минимальный слой поверх Telegram Payments API (через Telegram
Stars — не требует внешнего платёжного провайдера, работает "из коробки"
у любого бота). Когда будет готово подключать реальные деньги — можно
либо оставить Stars, либо заменить send_invoice/pre_checkout на конкретного
провайдера (Stripe и т.п.), не трогая остальной код бота: весь платёжный
путь изолирован в этом файле + двух хендлерах в handlers/final_version.py,
которые сейчас неактивны из-за проверки config.PAYMENT_ENABLED.

Что нужно сделать, чтобы включить (~через месяц, по словам заказчика):
  1. В .env: PAYMENT_ENABLED=true
  2. Задать цену: PAID_VERSION_PRICE_XTR (в Telegram Stars, целое число)
  3. Если решите использовать не Stars, а классический провайдер (карты
     и т.п.) — получить PROVIDER_TOKEN через @BotFather -> Payments и
     прописать его в .env; send_invoice ниже уже поддерживает оба режима
     (provider_token="" означает Stars, непустой — обычный провайдер).
  4. Ничего в handlers/final_version.py менять не придётся — там уже
     есть ветка на PAYMENT_ENABLED, просто активируется.
"""

from aiogram import Bot
from aiogram.types import LabeledPrice, PreCheckoutQuery

import config

PAID_REVISION_PAYLOAD = "paid_full_version_revision"


async def send_paid_revision_invoice(bot: Bot, chat_id: int) -> None:
    """Отправляет инвойс на покупку ещё одной полной версии документа
    сверх бесплатного лимита. Не вызывается нигде, пока
    config.PAYMENT_ENABLED=false."""
    prices = [LabeledPrice(label="Дополнительная версия документа", amount=config.PAID_VERSION_PRICE_XTR)]
    await bot.send_invoice(
        chat_id=chat_id,
        title="Дополнительная версия документа",
        description=(
            "Ещё одна итерация финальной версии заявки/бизнес-плана сверх "
            f"бесплатного лимита ({config.FULL_VERSION_LIMIT} версии)."
        ),
        payload=PAID_REVISION_PAYLOAD,
        provider_token=config.PROVIDER_TOKEN,  # "" => Telegram Stars
        currency="XTR" if not config.PROVIDER_TOKEN else "USD",
        prices=prices,
    )


async def handle_pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
    """Подтверждает оплату перед списанием средств. Telegram требует ответ
    в течение 10 секунд, иначе платёж отклоняется автоматически."""
    if pre_checkout_query.invoice_payload != PAID_REVISION_PAYLOAD:
        await pre_checkout_query.answer(ok=False, error_message="Неизвестный платёж")
        return
    await pre_checkout_query.answer(ok=True)
