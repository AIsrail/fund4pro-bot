"""Приём чека оплаты и подтверждение владельцем (см. receipt_payments.py).

Роутер подключается в bot.py ДО agent_router: у того catch-all на
сообщения/колбэки, который иначе съел бы и фото чека, и кнопки rc:*."""

import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import billing
import receipt_payments as rp

router = Router()
logger = logging.getLogger("fund4pro.receipt_handlers")


class _AwaitingReceipt(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        if not (message.photo or message.document):
            return False
        return await rp.get_waiting(message.chat.id) is not None


@router.message(_AwaitingReceipt())
async def receive_receipt(message: Message):
    chat_id = message.chat.id
    code = await rp.get_waiting(chat_id)
    if code is None:
        return
    _payload, _resource, _price_attr, what = rp.OFFERS[code]
    price = rp.price_kgs(code)

    owner = rp.owner_chat_id()
    if not owner:
        await message.answer("⚠️ Не удалось передать чек владельцу (не настроен получатель). Напишите владельцу бота напрямую.")
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"rc:ok:{chat_id}:{code}")
    kb.button(text="❌ Отклонить", callback_data=f"rc:no:{chat_id}:{code}")
    kb.adjust(2)
    try:
        await message.bot.forward_message(owner, chat_id, message.message_id)
        who = f"@{message.from_user.username}" if message.from_user and message.from_user.username else f"chat {chat_id}"
        await message.bot.send_message(
            owner,
            f"🧾 Чек от {who}: {what}, {price} сом.\nПроверьте поступление и подтвердите.",
            reply_markup=kb.as_markup(),
        )
    except Exception:
        logger.exception("receipt: failed to forward to owner")
        await message.answer("⚠️ Не удалось передать чек владельцу. Попробуйте ещё раз чуть позже.")
        return
    await message.answer("✅ Чек отправлен владельцу. Как только он подтвердит оплату — продолжу автоматически.")


@router.callback_query(F.data.startswith("rc:"))
async def owner_decision(callback: CallbackQuery, dispatcher):
    if not rp.is_owner(callback.from_user.id):
        await callback.answer("Только владелец бота.", show_alert=True)
        return
    _, verdict, chat_str, code = callback.data.split(":", 3)
    chat_id = int(chat_str)
    if code not in rp.OFFERS:
        await callback.answer("Неизвестный тип оплаты.", show_alert=True)
        return
    if not await rp.claim_decision(callback.message.chat.id, callback.message.message_id):
        await callback.answer("Решение по этому чеку уже принято.", show_alert=True)
        return
    await callback.answer()
    bot = callback.bot
    _payload, resource, _attr, what = rp.OFFERS[code]

    import telemetry
    await telemetry.log_event(chat_id, "payment_rejected" if verdict == "no" else "payment_confirmed", what=what)

    if verdict == "no":
        await rp.clear_waiting(chat_id)
        await callback.message.edit_text(f"{callback.message.text}\n\n❌ Отклонено.", reply_markup=None)
        await bot.send_message(
            chat_id,
            "❌ Владелец не нашёл этот платёж. Проверьте реквизиты и сумму и пришлите чек ещё раз "
            "(можно нажать нужное действие в боте снова).",
        )
        return

    await billing.add_paid_credit(chat_id, resource)
    await rp.clear_waiting(chat_id)
    await callback.message.edit_text(f"{callback.message.text}\n\n✅ Подтверждено, кредит начислен.", reply_markup=None)
    await bot.send_message(chat_id, f"✅ Оплата подтверждена — спасибо! 🙏 Продолжаю: {what}.")

    # Продолжаем действие, на котором пользователь упёрся в лимит (те же
    # resume_* что и у Telegram Stars). Message/FSM строим для чата ПОЛЬЗОВАТЕЛЯ.
    user_msg = Message(message_id=0, date=datetime.now(), chat=Chat(id=chat_id, type="private")).as_(bot)
    state = FSMContext(storage=dispatcher.storage, key=StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=chat_id))
    try:
        if code == "pf":
            import agent_router
            await agent_router.resume_after_file_export_payment(user_msg, state)
        elif code == "bf":
            from handlers import budget_standalone
            await budget_standalone.resume_after_budget_payment(user_msg, state)
        elif code == "pr":
            import agent_router
            await agent_router.resume_after_payment(user_msg, state)
        elif code == "td":
            import agent_router
            await agent_router.resume_after_template_payment(user_msg, state)
    except Exception:
        logger.exception("receipt: resume failed for chat %s code %s", chat_id, code)
        await bot.send_message(chat_id, "⚠️ Оплата засчитана, но продолжить автоматически не получилось — повторите действие, платить повторно не нужно.")
        await rp.notify_owner(bot, f"⚠️ Кредит начислен chat {chat_id} ({what}), но автопродолжение упало — см. логи.")
