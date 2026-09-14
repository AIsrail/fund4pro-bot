from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from handlers.budget import start_budget_discussion
from keyboards import concept_approval_keyboard
from llm import LLMEmptyResponseError, SYSTEM_PROMPT, call_claude_required, looks_like_question, match_intent, ui_language_clause
from states import ProjectFlow
from telegram_text import send_long
from typing_indicator import show_typing, show_working

router = Router()


@router.callback_query(ProjectFlow.concept_approval, F.data == "concept:approve")
async def concept_approve(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await start_budget_discussion(callback.message, state)


@router.callback_query(ProjectFlow.concept_approval, F.data == "concept:revise")
async def concept_revise(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProjectFlow.waiting_concept_revision)
    await callback.message.answer("Что поправить?")
    await callback.answer()


# РАНЬШЕ свободный текст на состоянии concept_approval (концепт уже
# показан, бот ждёт кнопку) полностью игнорировался — жёсткая привязка к
# кнопкам. Теперь классифицируем по смыслу вместо требования клика.
@router.message(ProjectFlow.concept_approval)
async def concept_approval_free_text(message: Message, state: FSMContext):
    text = message.text or ""
    idx = await match_intent(text, [
        "Одобряю концепт, готовь финальную версию",
        "Нужны правки к концепту",
    ])
    if idx == 0:
        await start_budget_discussion(message, state)
    elif idx == 1:
        await state.set_state(ProjectFlow.waiting_concept_revision)
        await receive_concept_revision(message, state)
    elif looks_like_question(text):
        # Похоже на вопрос/непонятную реплику, а не на инструкцию по
        # содержанию — здесь действительно стоит уточнить, а не гадать.
        await message.answer(
            "Не совсем понял — уточни, или нажми одну из кнопок:",
            reply_markup=concept_approval_keyboard(),
        )
    else:
        # РЕАЛЬНЫЙ ИНЦИДЕНТ: "вставь где возможно реальные данные" — явная
        # инструкция по содержанию концепта, но match_intent классифицирует
        # её нестабильно (не строгое 'одобряю' и не строгое 'нужны правки'
        # по формулировке, хотя по сути это правка). Раньше в этом случае
        # бот повторял те же 2 кнопки, а пользователь видел это как "бот
        # игнорирует прямой запрос" — обоснованная жалоба на "жёсткие
        # рамки". Любой содержательный текст, не являющийся вопросом,
        # обрабатываем как инструкцию по правке (безопасный дефолт: хуже,
        # если бот молча требует переформулировать, чем если применит
        # правку по существу).
        await state.set_state(ProjectFlow.waiting_concept_revision)
        await receive_concept_revision(message, state)


@router.message(ProjectFlow.waiting_concept_revision)
async def receive_concept_revision(message: Message, state: FSMContext):
    data = await state.get_data()
    prompt = (
        f"{SYSTEM_PROMPT}\n\nВот текущий концепт:\n{data.get('concept_text', '')}\n\n"
        f"Внеси такие правки: {message.text}\nВерни обновлённый концепт целиком, "
        f"с теми же markdown-заголовками.{ui_language_clause(data)}"
    )
    try:
        async with show_typing(message.bot, message.chat.id):
            revised = await call_claude_required(prompt, message.text or "", max_tokens=4000)
    except LLMEmptyResponseError:
        await message.answer(
            "⚠️ Не удалось получить обновлённый концепт от модели. Повтори "
            "сообщение с правками, чтобы попробовать снова."
        )
        return
    await state.update_data(concept_text=revised)
    await state.set_state(ProjectFlow.concept_approval)
    await send_long(
        message,
        "📝 Обновлённый концепт:\n\n" + revised,
        reply_markup=concept_approval_keyboard(),
    )
