"""Хендлеры для платежей (Telegram Payments API). Даже при
config.PAYMENT_ENABLED=false этот роутер безопасно подключается — Telegram
Payments-апдейты просто никогда не приходят, если ни разу не был вызван
send_invoice (см. payments.py, вызывается только за флагом PAYMENT_ENABLED
из handlers/final_version.py)."""

from aiogram import Router
from aiogram.types import Message, PreCheckoutQuery

from payments import handle_pre_checkout
from states import ProjectFlow

router = Router()


@router.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await handle_pre_checkout(pre_checkout_query)


@router.message(lambda m: m.successful_payment is not None)
async def successful_payment(message: Message, state):
    """После успешной оплаты снимаем лимит на одну итерацию — следующий
    вызов final:revise должен снова стать доступен. Реализовано через
    временное увеличение FULL_VERSION_LIMIT для текущей сессии не делаем
    (константа глобальная); вместо этого явно уменьшаем счётчик
    full_version_count на 1, что даёт тот же эффект без изменения
    глобального лимита."""
    data = await state.get_data()
    count = max(data.get("full_version_count", 0) - 1, 0)
    await state.update_data(full_version_count=count)
    await state.set_state(ProjectFlow.final_version)
    await message.answer(
        "✅ Оплата прошла успешно! Пришли, какие правки внести в финальную версию.",
    )
    await state.set_state(ProjectFlow.waiting_final_revision)
