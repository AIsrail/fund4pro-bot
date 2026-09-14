"""Бот САМ предлагает цель проекта и 2-3 задачи (одним структурированным
вызовом, не диалогом) сразу после того, как проблема/идея определена и
данные собраны. Пользователь одобряет или просит поправить — только после
этого идёт выбор скорости (черновик концепта / детализация мероприятий).

Заменяет прежний открытый диалог "какое изменение мы хотим увидеть", который
терял нить между репликами пользователя (см. states.py комментарии и разбор
бага: модель путала "уже случившееся" с "желаемым будущим" на разных ходах
диалога). Один структурированный вызов с чёткой цель+задачи убирает этот
класс ошибок целиком — модели больше не нужно самой удерживать состояние
диалога через несколько раундов.
"""

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from keyboards import concept_speed_keyboard, goal_objectives_keyboard
from llm import (
    LLMEmptyResponseError,
    call_claude_required,
    generate_goal_and_objectives,
    looks_like_question,
    match_intent,
    SYSTEM_PROMPT,
    ui_language_clause,
)
from states import ProjectFlow
from telegram_text import send_long
from typing_indicator import show_typing, show_working

router = Router()


async def propose_goal_and_objectives(message: Message, state: FSMContext):
    """Точка входа после сбора данных (Шаг 4) — заменяет прежний прямой
    переход к выбору скорости/деревьям."""
    data = await state.get_data()
    try:
        async with show_working(message, "⏳ Формулирую цель и задачи проекта..."):
            async with show_typing(message.bot, message.chat.id):
                proposal = await generate_goal_and_objectives(data)
    except LLMEmptyResponseError:
        await message.answer(
            "⚠️ Не удалось получить ответ от модели. Напиши что-нибудь, "
            "чтобы попробовать снова."
        )
        return
    await state.update_data(goal_and_objectives=proposal)
    await state.set_state(ProjectFlow.goal_objectives_review)
    await send_long(
        message,
        f"{proposal}\n\nЭто соответствует твоему видению?",
        reply_markup=goal_objectives_keyboard(),
    )


@router.callback_query(ProjectFlow.goal_objectives_review, F.data == "goal:approve")
async def goal_approve(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(ProjectFlow.concept_speed_check)
    await callback.message.answer(
        "Как будем строить концепт?",
        reply_markup=concept_speed_keyboard(),
    )


@router.callback_query(ProjectFlow.goal_objectives_review, F.data == "goal:revise")
async def goal_revise(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(ProjectFlow.waiting_goal_objectives_revision)
    await callback.message.answer("Что поправить в цели или задачах? Опиши текстом.")


# РАНЬШЕ на состоянии goal_objectives_review (цель+задачи уже показаны,
# бот ждёт кнопку "Согласен"/"Поправить") любой свободный текст
# ИГНОРИРОВАЛСЯ — жёсткая привязка к кнопкам, на которую пожаловался
# пользователь ("бот цепляет кнопку, не относящуюся к вопросу"). Теперь
# текст сначала классифицируется по смыслу (согласие / просьба
# поправить / нечто другое) — кнопки остаются доступны, но не единственный
# канал.
@router.message(ProjectFlow.goal_objectives_review)
async def goal_review_free_text(message: Message, state: FSMContext):
    text = message.text or ""
    idx = await match_intent(text, [
        "Согласен с целью и задачами, продолжаем дальше",
        "Нужно поправить формулировку цели или задач",
    ])
    if idx == 0:
        await state.set_state(ProjectFlow.concept_speed_check)
        await message.answer("Как будем строить концепт?", reply_markup=concept_speed_keyboard())
    elif idx == 1:
        await state.set_state(ProjectFlow.waiting_goal_objectives_revision)
        await receive_goal_revision(message, state)
    elif looks_like_question(text):
        # Похоже на вопрос/непонятную реплику, а не на инструкцию по
        # содержанию — здесь действительно стоит уточнить, а не гадать.
        await message.answer(
            "Не совсем понял, что имеешь в виду — уточни, или нажми одну "
            "из кнопок:",
            reply_markup=goal_objectives_keyboard(),
        )
    else:
        # РЕАЛЬНЫЙ ИНЦИДЕНТ (тот же класс, что в concept.py): содержательная
        # инструкция по правке ("вставь где возможно реальные данные")
        # классифицируется match_intent нестабильно между 'согласен' и
        # 'нужно поправить'. Раньше в этом случае бот просто повторял
        # кнопки — пользователь справедливо жаловался на "жёсткие рамки,
        # не вставляет данные". Любой содержательный текст, не похожий на
        # вопрос, обрабатываем как инструкцию по правке.
        await state.set_state(ProjectFlow.waiting_goal_objectives_revision)
        await receive_goal_revision(message, state)


@router.message(ProjectFlow.waiting_goal_objectives_revision)
async def receive_goal_revision(message: Message, state: FSMContext):
    data = await state.get_data()
    prompt = (
        f"{SYSTEM_PROMPT}\n\nВот текущее предложение цели и задач:\n"
        f"{data.get('goal_and_objectives', '')}\n\nВнеси такие правки: "
        f"{message.text}\nВерни обновлённый вариант целиком, в том же "
        f"формате ('## Цель' и '## Задачи').{ui_language_clause(data)}"
    )
    try:
        async with show_typing(message.bot, message.chat.id):
            revised = await call_claude_required(prompt, message.text or "")
    except LLMEmptyResponseError:
        await message.answer(
            "⚠️ Не удалось получить обновлённый вариант от модели. Повтори "
            "правку, чтобы попробовать снова."
        )
        return
    await state.update_data(goal_and_objectives=revised)
    await state.set_state(ProjectFlow.goal_objectives_review)
    await send_long(
        message,
        f"{revised}\n\nТеперь так лучше?",
        reply_markup=goal_objectives_keyboard(),
    )
