from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from keyboards import data_availability_keyboard, idea_select_keyboard
from llm import answer_user_question, generate_ideas, looks_like_question, summarize_understanding
from states import ProjectFlow
from typing_indicator import show_typing, show_working

router = Router()


@router.callback_query(ProjectFlow.idea_defined_check, F.data == "idea:defined")
async def idea_defined(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    flow = data.get("flow", "grant")
    label = "проблему" if flow == "grant" else "бизнес-идею"
    await state.set_state(ProjectFlow.waiting_problem_description)
    await callback.message.answer(f"Опиши {label} текстом.")
    await callback.answer()


@router.message(ProjectFlow.waiting_problem_description)
async def receive_problem_description(message: Message, state: FSMContext):
    raw_text = message.text or ""
    if looks_like_question(raw_text):
        async with show_typing(message.bot, message.chat.id):
            answer = await answer_user_question(raw_text, "описание проблемы/идеи")
        if answer:
            await message.answer(answer)
        return
    problem_text = raw_text
    await state.update_data(selected_idea=problem_text, idea_defined=True)
    async with show_typing(message.bot, message.chat.id):
        summary = await summarize_understanding("Проблема/идея", problem_text)
    if summary:
        await message.answer(f"✅ Понял: {summary}")
    await _go_to_data_check(message, state)


@router.callback_query(ProjectFlow.idea_defined_check, F.data == "idea:need_help")
async def idea_need_help(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    async with show_working(callback.message, "⏳ Генерирую идеи, это может занять до минуты..."):
        async with show_typing(callback.bot, callback.message.chat.id):
            note, ideas = await generate_ideas(
                data.get("org_info", ""),
                data.get("donor_info", ""),
                data.get("flow", "grant"),
                data.get("donor_forms_text", ""),
            )
    await state.update_data(generated_ideas=ideas, idea_defined=False)
    ideas_text = "\n\n".join(f"{i + 1}. {idea}" for i, idea in enumerate(ideas))
    intro = f"{note}\n\n" if note else ""
    await callback.message.answer(
        f"{intro}Вот несколько идей:\n\n{ideas_text}\n\nВыбери одну:",
        reply_markup=idea_select_keyboard(ideas),
    )


@router.callback_query(ProjectFlow.idea_defined_check, F.data.startswith("idea:select:"))
async def idea_selected(callback: CallbackQuery, state: FSMContext):
    index = int(callback.data.split(":")[-1])
    data = await state.get_data()
    ideas = data.get("generated_ideas", [])
    selected = ideas[index] if index < len(ideas) else (ideas[0] if ideas else "")
    await state.update_data(selected_idea=selected)
    await _go_to_data_check(callback.message, state)
    await callback.answer()


async def _go_to_data_check(message: Message, state: FSMContext):
    await state.set_state(ProjectFlow.data_availability_check)
    data = await state.get_data()
    flow = data.get("flow", "grant")
    question = (
        "Есть ли у тебя данные для проекта (статистика, наблюдения, цифры по проблеме)?"
        if flow == "grant"
        else "Есть ли у тебя данные для бизнес-плана (данные о рынке, цифры, финпоказатели)?"
    )
    await message.answer(question, reply_markup=data_availability_keyboard())
