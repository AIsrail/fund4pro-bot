"""Согласование бюджета — вставлено между одобрением концепта (Шаг 6) и
сборкой финальной версии (Шаг 7).

Раньше бюджетный раздел в финальном документе был сплошь заполнен
плейсхолдерами XYZ — формально следовало правилу "не выдумывай цифры", но
выглядело как непроработанный документ (и не давало пользователю ничего
конкретного, от чего оттолкнуться). Здесь бот САМ предлагает
ориентировочные суммы (как обычный Claude сделал бы в прямом диалоге) и
даёт пользователю согласовать/поправить их перед тем, как они попадут в
финальный документ.
"""

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from keyboards import budget_ready_keyboard
from llm import LLMEmptyResponseError, generate_budget_proposal, user_facing_llm_error_message
from states import ProjectFlow
from telegram_text import send_long
from typing_indicator import show_typing, show_working

router = Router()


async def start_budget_discussion(message: Message, state: FSMContext) -> None:
    """Точка входа после одобрения концепта — заменяет прежний прямой
    переход к send_final_version."""
    data = await state.get_data()
    try:
        async with show_working(message, "⏳ Прикидываю ориентировочный бюджет..."):
            async with show_typing(message.bot, message.chat.id):
                proposal = await generate_budget_proposal(data)
    except LLMEmptyResponseError as e:
        await message.answer(user_facing_llm_error_message(e, "Напиши что-нибудь, чтобы попробовать снова."))
        return
    await state.update_data(budget_text=proposal, budget_history=[
        {"role": "assistant", "content": proposal},
    ])
    await state.set_state(ProjectFlow.budget_discussion)
    await send_long(message, proposal, reply_markup=budget_ready_keyboard())


@router.message(ProjectFlow.budget_discussion)
async def receive_budget_message(message: Message, state: FSMContext):
    data = await state.get_data()
    history = data.get("budget_history", [])
    try:
        async with show_typing(message.bot, message.chat.id):
            reply = await generate_budget_proposal(data, message.text or "", history)
    except LLMEmptyResponseError as e:
        await message.answer(user_facing_llm_error_message(e))
        return
    new_history = history + [
        {"role": "user", "content": message.text or ""},
        {"role": "assistant", "content": reply},
    ]
    # Последний ответ модели считаем актуальной версией бюджета — именно
    # он попадёт в финальный документ (budget_text в _session_summary).
    await state.update_data(budget_text=reply, budget_history=new_history[-10:])
    await send_long(message, reply, reply_markup=budget_ready_keyboard())


@router.callback_query(ProjectFlow.budget_discussion, F.data == "budget:hint_comment")
async def budget_hint_comment(callback: CallbackQuery, state: FSMContext):
    # Подсказывающая кнопка (см. keyboards.budget_ready_keyboard) — просто
    # приглашает написать текстом; сам текст обрабатывается уже
    # существующим receive_budget_message (свободный текст на этом
    # состоянии работает и без этой кнопки, она только подсказка).
    await callback.answer()
    await callback.message.answer(
        "Напиши комментарий или уточнение по бюджету текстом — учту его."
    )


@router.callback_query(ProjectFlow.budget_discussion, F.data == "budget:done")
async def budget_done(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()

    # Если донор дал бюджет именно в Excel-шаблоне — заполняем СРАЗУ ЕГО
    # напрямую (сохраняя структуру/формулы/остальные листы), а не только
    # пересобираем markdown-версию в .docx. Раньше даже при наличии
    # xlsx-шаблона финальный документ выходил только как Word — донор же
    # ожидает именно свой Excel-файл с цифрами.
    xlsx_hex = data.get("donor_budget_xlsx_content")
    if xlsx_hex:
        from excel_fill import extract_xlsx_structure, fill_xlsx_template, generate_budget_cell_mapping
        from donor_form_cache import save_donor_form
        from aiogram.types import FSInputFile

        content = bytes.fromhex(xlsx_hex)
        filename = data.get("donor_budget_xlsx_filename") or "budget.xlsx"
        try:
            async with show_working(callback.message, "⏳ Заполняю бюджетный Excel-шаблон донора..."):
                structure = extract_xlsx_structure(content)
                mapping = await generate_budget_cell_mapping(structure, data.get("budget_text", ""))
                if mapping:
                    filled = fill_xlsx_template(content, mapping)
                    path = save_donor_form(filled, f"filled_{filename}")
                    await callback.message.answer_document(
                        FSInputFile(path),
                        caption=f"📊 {filename} — заполнен бюджетными цифрами выше, проверь перед отправкой донору.",
                    )
                else:
                    await callback.message.answer(
                        f"⚠️ Не смог уверенно определить, в какие именно ячейки "
                        f"{filename} вписывать суммы (структура шаблона нестандартная) "
                        "— заполни его вручную цифрами из бюджета выше."
                    )
        except Exception as exc:
            await callback.message.answer(
                f"⚠️ Не удалось автоматически заполнить {filename} ({exc}) — "
                "заполни его вручную цифрами из бюджета выше."
            )

    from handlers.final_version import send_final_version
    async with show_working(callback.message, "⏳ Готовлю финальный документ, это может занять до пары минут..."):
        await send_final_version(callback.message, state)
