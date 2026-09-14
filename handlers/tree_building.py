"""Мероприятия/действия ПОД уже одобренную цель и задачи (см. goal_review.py).

В отличие от прежнего трёхэтапного диалога "проблема -> цели -> действия",
здесь модель работает с уже зафиксированной целью/задачами — не додумывает
их заново на каждом ходу диалога. Это единственная оставшаяся диалоговая
стадия перед концептом, доступна пользователю, выбравшему "хочу подробнее
проработать мероприятия" на шаге выбора скорости (concept_speed.py).
"""

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from keyboards import concept_approval_keyboard, tree_stage_keyboard
from llm import LLMEmptyResponseError, check_red_flags, detail_activities, format_issues_for_user, generate_concept
from states import ProjectFlow
from typing_indicator import show_typing, show_working

router = Router()


@router.message(ProjectFlow.tree_building)
async def activities_dialogue(message: Message, state: FSMContext):
    data = await state.get_data()
    history = data.get("activities_history", [])

    try:
        async with show_typing(message.bot, message.chat.id):
            reply = await detail_activities(data, message.text or "", history)
    except LLMEmptyResponseError:
        await message.answer(
            "⚠️ Не удалось получить ответ от модели. Повтори сообщение, "
            "чтобы попробовать снова."
        )
        return

    history = history + [
        {"role": "user", "content": message.text or ""},
        {"role": "assistant", "content": reply},
    ]
    await state.update_data(activities_history=history, action_trees=reply)
    await message.answer(reply, reply_markup=tree_stage_keyboard())


@router.callback_query(ProjectFlow.tree_building, F.data == "tree:hint_comment")
async def tree_hint_comment(callback: CallbackQuery, state: FSMContext):
    # Подсказывающая кнопка (см. keyboards.tree_stage_keyboard) — просто
    # приглашает написать текстом; свободный текст на этом состоянии уже
    # обрабатывается activities_dialogue выше без этой кнопки.
    await callback.answer()
    await callback.message.answer(
        "Напиши комментарий или уточнение по мероприятиям текстом — учту его."
    )


@router.callback_query(ProjectFlow.tree_building, F.data == "tree:advance")
async def activities_advance(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    async with show_working(callback.message, "⏳ Формирую концепт проекта, это может занять до минуты..."):
        await _finish_activities(callback.message, state)


async def _finish_activities(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        async with show_typing(message.bot, message.chat.id):
            concept = await generate_concept(data)
            await state.update_data(concept_text=concept)
            await state.set_state(ProjectFlow.concept_approval)

            # Лёгкая red-flags проверка уже на уровне концепта (§1.2): часть ошибок
            # видна уже здесь (проблема/решение не связаны, цели-как-активности,
            # слишком широкая аудитория). Полная проверка — на финальной версии
            # (handlers/final_version.py), где уже есть бюджет и риски. Здесь мы
            # НЕ блокируем показ концепта — просто прикладываем предупреждение,
            # т.к. концепт короткий и дешёво поправить на следующей итерации.
            warning = ""
            red_flags_result = await check_red_flags(concept, data)
    except LLMEmptyResponseError:
        await message.answer(
            "⚠️ Не удалось получить ответ от модели (пустой или неудачный "
            "запрос). Нажми кнопку ещё раз, чтобы попробовать снова."
        )
        return
    if not red_flags_result.get("passed"):
        warning = "\n\n" + format_issues_for_user(red_flags_result.get("issues", []))

    await message.answer(
        "📝 Черновая структура готова. Вот концепт проекта. Посмотри и "
        "одобри — на его основе будет подготовлена ФИНАЛЬНАЯ версия документа "
        "(максимум 2 итерации полной версии).\n\n" + concept + warning,
        reply_markup=concept_approval_keyboard(),
    )
