from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from document_reader import UnsupportedFormatError, extract_text_from_telegram_file
from keyboards import more_or_continue_keyboard
from llm import answer_user_question, looks_like_question, summarize_understanding
from org_profile import save_org_profile
from states import ProjectFlow
from typing_indicator import show_typing

router = Router()


@router.message(ProjectFlow.waiting_org_info)
async def receive_org_info(message: Message, state: FSMContext):
    raw_text = message.text or message.caption or ""
    # Пользователь мог прислать документ ИЛИ текст, а caption/текст при
    # этом — это вопрос ("этого достаточно?"), а не сами данные. Раньше
    # такое сообщение молча съедалось как org_info, вопрос игнорировался,
    # и бот просто ехал дальше по сценарию — выглядело так, будто он не
    # слушает. Если это документ с коротким вопросом-подписью — отвечаем
    # на вопрос коротко, а данные всё равно принимаем из документа.
    user_question = raw_text if (not message.document and looks_like_question(raw_text)) else (
        message.caption if (message.document and message.caption and looks_like_question(message.caption)) else ""
    )
    org_info = "" if (user_question and not message.document) else raw_text
    if message.document:
        try:
            async with show_typing(message.bot, message.chat.id):
                text = await extract_text_from_telegram_file(message.bot, message.document)
        except UnsupportedFormatError as e:
            await message.answer(f"⚠️ {e}")
            return
        if text.strip():
            org_info += f"\n\n[Содержимое файла {message.document.file_name}]:\n{text}"
        else:
            await message.answer(
                f"⚠️ Не смог извлечь текст из {message.document.file_name} "
                "(файл повреждён, пустой, или это скан без текстового слоя). "
                "Пришли другой файл или опиши организацию текстом."
            )
            return

    # Копим информацию по организации через несколько сообщений (документ +
    # текст + ещё документ и т.п.), а не перезаписываем org_info каждым новым
    # сообщением — раньше второй присланный файл стирал текст первого.
    data = await state.get_data()
    existing = data.get("org_info", "")
    if not org_info.strip():
        org_info = "(получено сообщение без текста и без читаемого документа)"
    combined_org_info = f"{existing}\n\n{org_info}".strip() if existing else org_info
    await state.update_data(org_info=combined_org_info)

    async with show_typing(message.bot, message.chat.id):
        summary = await summarize_understanding("Организация/бизнес", org_info)
    if summary:
        await message.answer(f"✅ Понял: {summary}")

    if user_question:
        async with show_typing(message.bot, message.chat.id):
            answer = await answer_user_question(user_question, "приём информации об организации", data)
        if answer:
            await message.answer(answer)

    # РАНЬШЕ здесь бот сразу и безусловно ехал к следующему вопросу
    # ("Пришли ссылку о доноре...") — даже если сам только что ответил на
    # вопрос пользователя "информации пока не хватает". Получалась каша из
    # двух несвязанных реплик подряд, и бот выглядел так, будто навязывает
    # свой сценарий поверх диалога. Теперь вместо принудительного перехода
    # показываем кнопки — пользователь сам решает, когда двигаться дальше.
    await message.answer(
        "Есть ещё что добавить об организации, или переходим дальше?",
        reply_markup=more_or_continue_keyboard(),
    )


@router.callback_query(ProjectFlow.waiting_org_info, F.data == "step:more")
async def org_info_more(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("Хорошо, пришли ещё информацию.")


@router.callback_query(ProjectFlow.waiting_org_info, F.data == "step:continue")
async def org_info_continue(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    # Запоминаем профиль организации ДОЛГОСРОЧНО (переживает /start и
    # "Начать заново") — раньше эта информация терялась при любом сбросе
    # сессии, и приходилось присылать документы заново для каждого нового
    # проекта/донора той же самой организации.
    save_org_profile(callback.from_user.id, data.get("org_info", ""))
    flow = data.get("flow", "grant")
    label = "доноре/конкурсе" if flow == "grant" else "инвесторе/банке/акселераторе"
    await state.set_state(ProjectFlow.waiting_donor_info)
    await callback.message.answer(f"📎 Пришли ссылку и/или документы о {label}")
