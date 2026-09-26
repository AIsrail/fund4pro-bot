"""Хендлеры для платежей (Telegram Payments API). Даже при
config.PAYMENT_ENABLED=false этот роутер безопасно подключается — Telegram
Payments-апдейты просто никогда не приходят, если ни разу не был вызван
send_invoice (см. payments.py: send_project_invoice — живой путь из
agent_router._paywall_or_consume; send_paid_revision_invoice — заготовка
под старую, неподключённую FSM-архитектуру).

ВАЖНО про порядок подключения роутеров (bot.py): этот router должен быть
включён РАНЬШЕ agent_router.router. agent_router содержит catch-all
@router.message() без фильтра (receive_any_message) — сообщение с
successful_payment не имеет .text/.caption, поэтому попадало бы в его ветку
"Не увидел текста" и до этого хендлера просто не доходило бы."""

import billing
from aiogram import Router
from aiogram.types import Message, PreCheckoutQuery

from payments import (
    PAID_FILE_EXPORT_PAYLOAD,
    PAID_PROJECT_PAYLOAD,
    PAID_REVISION_PAYLOAD,
    PAID_TEMPLATE_DOWNLOAD_PAYLOAD,
    handle_pre_checkout,
)

router = Router()

# Владелец попросил тёплое сообщение после оплаты вместо сухого "оплата
# прошла" — по его словам, "это символическая оплата, тогда как оплата
# настоящему профи стоила в разы дороже". Без конкретного множителя ("в
# сотни раз") — это ничем не подтверждённая цифра, а "в разы" передаёт ту же
# мысль без риска, что кто-то спросит, откуда цифра.
PAID_PROJECT_THANK_YOU = (
    "✅ Оплата прошла успешно — спасибо, что поддерживаете проект! 🙏\n\n"
    "Это символическая цена: помощь живого консультанта по грантам обошлась "
    "бы в разы дороже. Продолжаю работу над проектом."
)


@router.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await handle_pre_checkout(pre_checkout_query)


@router.message(lambda m: m.successful_payment is not None)
async def successful_payment(message: Message, state):
    payload = message.successful_payment.invoice_payload

    if payload == PAID_PROJECT_PAYLOAD:
        # Живой путь: оплата ещё одного проекта сверх бесплатного лимита
        # (см. billing.py, agent_router._paywall_or_consume).
        import agent_router

        await billing.add_paid_credit(message.chat.id, "project")
        await message.answer(PAID_PROJECT_THANK_YOU)
        await agent_router.resume_after_payment(message, state)
        return

    if payload == PAID_TEMPLATE_DOWNLOAD_PAYLOAD:
        # Оплата скачивания шаблона донора сверх бесплатного лимита — файл
        # бот уже скачал ДО выставления счёта (см. agent_engine.py), здесь
        # только начисляем кредит и отдаём придержанную копию.
        import agent_router

        await billing.add_paid_credit(message.chat.id, "template_download")
        await message.answer("✅ Оплата прошла — вот шаблон:")
        await agent_router.resume_after_template_payment(message, state)
        return

    if payload == PAID_FILE_EXPORT_PAYLOAD:
        # Оплата сборки документа в официальный файл шаблона донора (по
        # умолчанию бесплатно — только текст в чате).
        import agent_router

        await billing.add_paid_credit(message.chat.id, "file_export")
        await message.answer("✅ Оплата прошла — собираю файл:")
        await agent_router.resume_after_file_export_payment(message, state)
        return

    if payload == PAID_REVISION_PAYLOAD:
        # Старая ветка (правка уже сгенерированного документа) — принадлежит
        # неподключённой FSM-архитектуре (handlers/final_version.py), которую
        # bot.py не монтирует. Оставлена нетронутой на случай отката; в
        # текущем живом боте этот payload не отправляется.
        from states import ProjectFlow

        data = await state.get_data()
        count = max(data.get("full_version_count", 0) - 1, 0)
        await state.update_data(full_version_count=count)
        await state.set_state(ProjectFlow.final_version)
        await message.answer(
            "✅ Оплата прошла успешно! Пришли, какие правки внести в финальную версию.",
        )
        await state.set_state(ProjectFlow.waiting_final_revision)
        return

    await message.answer("✅ Оплата получена.")
