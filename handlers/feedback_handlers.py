"""Опрос обратной связи после выдачи заявки (см. feedback.py).

Роутер ДОЛЖЕН быть подключён РАНЬШЕ agent_router (bot.py): у agent_router
есть catch-all @router.callback_query() и @router.message() без фильтра,
которые иначе перехватили бы кнопки fb:* и текст предложений.

Опрос НЕ блокирует работу: кнопки — обычные inline, обычное сообщение
пользователя уходит агенту как раньше. Текст предложений перехватывается
только после того, как человек сам нажал "✍️ Написать предложение" — иначе
следующее обычное сообщение по проекту ошибочно улетало бы в отзыв."""

import logging

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import feedback

router = Router()
logger = logging.getLogger("fund4pro.feedback_handlers")

INTRO = (
    "🙏 Заявка готова. Помогите сделать бота лучше — ответьте на 4 коротких "
    "вопроса (меньше минуты). Это необязательно."
)


def _kb_start():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Ответить", callback_data="fb:begin")
    kb.button(text="Пропустить", callback_data="fb:skip")
    kb.adjust(2)
    return kb.as_markup()


def _kb_choice(idx: int):
    key, _text, options = feedback.CHOICE_QUESTIONS[idx]
    kb = InlineKeyboardBuilder()
    for code, label in options:
        kb.button(text=label, callback_data=f"fb:c:{key}:{code}")
    kb.adjust(2)
    return kb.as_markup()


def _kb_quality():
    kb = InlineKeyboardBuilder()
    for n in range(1, 6):
        kb.button(text=f"{n}⭐", callback_data=f"fb:q:{n}")
    kb.adjust(5)
    return kb.as_markup()


def _kb_suggest():
    kb = InlineKeyboardBuilder()
    kb.button(text="✍️ Написать предложение", callback_data="fb:text")
    kb.button(text="Готово, без предложений", callback_data="fb:done")
    kb.adjust(1)
    return kb.as_markup()


async def offer_feedback(message: Message, state: FSMContext) -> None:
    """Вызывается из agent_router после выдачи заявки — не чаще одного раза
    на проект (флаг сбрасывается вместе с FSM-данными при новом проекте)."""
    data = await state.get_data()
    if data.get("_feedback_asked"):
        return
    await state.update_data(_feedback_asked=True)
    try:
        await message.answer(INTRO, reply_markup=_kb_start())
    except Exception:
        logger.warning("Failed to send feedback offer", exc_info=True)


async def _ask_choice(message: Message, idx: int) -> None:
    await message.answer(feedback.CHOICE_QUESTIONS[idx][1], reply_markup=_kb_choice(idx))


@router.callback_query(F.data == "fb:skip")
async def fb_skip(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data == "fb:begin")
async def fb_begin(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.update_data(_fb={})
    await _ask_choice(callback.message, 0)


@router.callback_query(F.data.startswith("fb:c:"))
async def fb_choice(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    _, _, key, code = callback.data.split(":", 3)
    data = await state.get_data()
    answers = dict(data.get("_fb") or {})
    answers[key] = code
    await state.update_data(_fb=answers)
    keys = [q[0] for q in feedback.CHOICE_QUESTIONS]
    if key in keys and keys.index(key) + 1 < len(keys):
        await _ask_choice(callback.message, keys.index(key) + 1)
    else:
        await callback.message.answer(feedback.QUALITY_QUESTION, reply_markup=_kb_quality())


@router.callback_query(F.data.startswith("fb:q:"))
async def fb_quality(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    answers = dict(data.get("_fb") or {})
    answers["quality"] = int(callback.data.rsplit(":", 1)[1])
    await state.update_data(_fb=answers)
    await callback.message.answer(
        "И последнее — есть предложения, что улучшить?", reply_markup=_kb_suggest(),
    )


@router.callback_query(F.data == "fb:text")
async def fb_text_prompt(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.update_data(_fb_awaiting_text=True)
    await callback.message.answer("Напишите предложение одним сообщением 👇")


@router.callback_query(F.data == "fb:done")
async def fb_done(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await _finish(callback.message, state, callback.from_user)


class _AwaitingFeedbackText(BaseFilter):
    async def __call__(self, message: Message, state: FSMContext) -> bool:
        text = (message.text or "").strip()
        if not text or text.startswith("/"):
            return False
        return bool((await state.get_data()).get("_fb_awaiting_text"))


@router.message(_AwaitingFeedbackText())
async def fb_receive_text(message: Message, state: FSMContext):
    data = await state.get_data()
    answers = dict(data.get("_fb") or {})
    answers["suggestions"] = message.text.strip()[:2000]
    await state.update_data(_fb=answers, _fb_awaiting_text=False)
    await _finish(message, state, message.from_user)


async def _finish(message: Message, state: FSMContext, user) -> None:
    data = await state.get_data()
    answers = dict(data.get("_fb") or {})
    await state.update_data(_fb=None, _fb_awaiting_text=False)
    chat_id = message.chat.id
    username = getattr(user, "username", None)
    await feedback.save_feedback(chat_id, username, answers, data.get("flow"))
    await message.answer("Спасибо за отзыв! 🙏 Можно продолжать работу над проектом.")
    await notify_owner(message, chat_id, username, answers)


async def notify_owner(message: Message, chat_id: int, username, answers: dict) -> None:
    owner = config.OWNER_CHAT_ID
    if not owner and getattr(config, "UNLIMITED_USER_IDS", None):
        owner = min(config.UNLIMITED_USER_IDS)
    if not owner:
        return
    try:
        await message.bot.send_message(owner, feedback.format_for_owner(chat_id, username, answers))
    except Exception:
        logger.warning("Failed to forward feedback to owner", exc_info=True)


def _is_owner(chat_id: int) -> bool:
    owner = config.OWNER_CHAT_ID
    if owner and str(chat_id) == str(owner):
        return True
    return chat_id in (getattr(config, "UNLIMITED_USER_IDS", None) or ())


@router.message(Command("feedback_stats"))
async def feedback_stats(message: Message):
    """Только владелец: сводка + JSONL-выгрузка всех отзывов (для анализа
    и улучшения бота — файл кладётся в папку feedback/ репозитория)."""
    if not _is_owner(message.chat.id):
        return
    import json
    records = await feedback.load_all()
    await message.answer(feedback.summarize(records))
    if records:
        raw = "\n".join(json.dumps(r, ensure_ascii=False) for r in records).encode("utf-8")
        await message.answer_document(BufferedInputFile(raw, filename="feedback.jsonl"))


@router.message(Command("insights"))
async def insights(message: Message):
    """Только владелец: воронка по шагам, частые выборы, оплаты + выгрузка
    events.jsonl (телеметрия, см. telemetry.py) и feedback.jsonl — для
    еженедельного анализа и улучшений бота."""
    if not _is_owner(message.chat.id):
        return
    import json
    import telemetry
    events = await telemetry.load_all()
    from telegram_text import send_long
    await send_long(message, telemetry.summarize(events))
    if events:
        raw = "\n".join(json.dumps(e, ensure_ascii=False) for e in events).encode("utf-8")
        await message.answer_document(BufferedInputFile(raw, filename="events.jsonl"))
    records = await feedback.load_all()
    if records:
        await message.answer(feedback.summarize(records))
        raw = "\n".join(json.dumps(r, ensure_ascii=False) for r in records).encode("utf-8")
        await message.answer_document(BufferedInputFile(raw, filename="feedback.jsonl"))
