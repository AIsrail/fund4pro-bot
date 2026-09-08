"""Выбор скорости получения концепта — после того, как цель и задачи уже
согласованы (см. goal_review.py):

- "draft" — черновик концепта прямо сейчас, без диалога; недостающие цифры
  помечаются плейсхолдером [XYZ: ...] вместо выдумывания правдоподобных чисел.
  После показа черновика — отдельный вопрос "детализировать мероприятия?"
  (нетерпеливый пользователь может сразу увидеть концепт и решить дальше).
- "full" — диалог по детализации конкретных мероприятий (handlers/tree_building.py)
  перед тем, как собирать концепт.
"""

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from keyboards import concept_approval_keyboard, post_draft_detail_keyboard
from llm import (
    LLMEmptyResponseError,
    check_red_flags,
    format_issues_for_user,
    generate_draft_concept,
    summarize_xyz_placeholders,
)
from states import ProjectFlow
from telegram_text import send_long
from typing_indicator import show_typing, show_working

router = Router()



@router.callback_query(ProjectFlow.concept_speed_check, F.data == "speed:full")
async def speed_full(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(ProjectFlow.tree_building)
    await state.update_data(activities_history=[])
    await callback.message.answer(
        "Хорошо, разберём мероприятия подробнее. Какие конкретные действия "
        "нужны, чтобы выполнить задачи — жду твой ответ."
    )


@router.callback_query(ProjectFlow.concept_speed_check, F.data == "speed:draft")
async def speed_draft(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    try:
        async with show_working(callback.message, "⏳ Собираю черновик концепта..."):
            async with show_typing(callback.bot, callback.message.chat.id):
                concept = await generate_draft_concept(data)
            await state.update_data(concept_text=concept)
            await state.set_state(ProjectFlow.post_draft_detail_check)
            warning = await _concept_red_flags_warning(concept, data)
    except LLMEmptyResponseError:
        await callback.message.answer(
            "⚠️ Не удалось получить ответ от модели (пустой или неудачный "
            "запрос). Нажми кнопку ещё раз, чтобы попробовать снова."
        )
        return
    xyz_summary = summarize_xyz_placeholders(concept)
    if xyz_summary:
        # РАНЬШЕ здесь была только общая фраза "где-то есть XYZ, найди
        # сам" — пользователь просил явную, но КРАТКУЮ сводку конкретных
        # мест, а не разбор всего текста вручную.
        await send_long(callback.message, xyz_summary)
    else:
        await callback.message.answer("📝 Черновая структура готова, всех данных хватило.")
    await send_long(
        callback.message,
        concept + warning
        + "\n\nХочешь детализировать мероприятия перед финальной версией, "
          "или этого достаточно?",
        reply_markup=post_draft_detail_keyboard(),
    )


@router.callback_query(ProjectFlow.post_draft_detail_check, F.data == "detail:done")
async def detail_done(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    concept_text = data.get("concept_text", "").strip()
    if not concept_text:
        # Не должно происходить теперь, что concept_text пуст (см.
        # LLMEmptyResponseError выше — генерация либо даёт текст, либо явно
        # сообщает об ошибке до сохранения в state), но fail-safe на случай
        # рассинхрона состояния всё равно лучше явного сообщения, чем
        # передавать пустую строку в Telegram (тот самый крах "message text
        # is empty").
        await callback.message.answer(
            "⚠️ Черновик концепта не сохранился корректно. Вернись на шаг "
            "выбора черновика и попробуй заново."
        )
        return
    await state.set_state(ProjectFlow.concept_approval)
    await send_long(callback.message, concept_text, reply_markup=concept_approval_keyboard())


@router.callback_query(ProjectFlow.post_draft_detail_check, F.data == "detail:more")
async def detail_more(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(ProjectFlow.tree_building)
    await state.update_data(activities_history=[])
    await callback.message.answer(
        "Хорошо, разберём мероприятия подробнее. Какие конкретные действия "
        "нужны, чтобы выполнить задачи — жду твой ответ."
    )


async def _concept_red_flags_warning(concept: str, session_data: dict) -> str:
    """Та же лёгкая red-flags проверка, что и в полном пути (tree_building.py
    ::_finish_activities) — не блокирует показ, только предупреждает."""
    result = await check_red_flags(concept, session_data)
    if not result.get("passed"):
        return "\n\n" + format_issues_for_user(result.get("issues", []))
    return ""
