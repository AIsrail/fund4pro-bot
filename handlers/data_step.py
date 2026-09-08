from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from data_search import format_results_for_prompt, try_search_statistics
from handlers.goal_review import propose_goal_and_objectives
from llm import answer_user_question, generate_search_queries, looks_like_question, summarize_understanding
from states import ProjectFlow
from typing_indicator import show_typing

router = Router()


@router.callback_query(ProjectFlow.data_availability_check, F.data == "data:have")
async def data_have(callback: CallbackQuery, state: FSMContext):
    await state.update_data(has_data=True)
    await state.set_state(ProjectFlow.waiting_data)
    await callback.message.answer("Пришли данные и/или ссылки на источники")
    await callback.answer()


@router.message(ProjectFlow.waiting_data)
async def receive_data(message: Message, state: FSMContext):
    raw_text = message.text or message.caption or ""
    user_question = raw_text if (not message.document and looks_like_question(raw_text)) else ""
    project_data = "" if user_question else raw_text
    await state.update_data(project_data=project_data)
    async with show_typing(message.bot, message.chat.id):
        summary = await summarize_understanding("Данные проекта", project_data)
    if summary:
        await message.answer(f"✅ Понял: {summary}")
    if user_question:
        async with show_typing(message.bot, message.chat.id):
            answer = await answer_user_question(user_question, "приём данных проекта")
        if answer:
            await message.answer(answer)
        return
    await propose_goal_and_objectives(message, state)


@router.callback_query(ProjectFlow.data_availability_check, F.data == "data:none")
async def data_none(callback: CallbackQuery, state: FSMContext):
    # РЕАЛЬНЫЙ ИНЦИДЕНТ: callback.answer() стоял в КОНЦЕ функции, после
    # долгого веб-поиска + нескольких LLM-вызовов (генерация запросов,
    # анализ найденного, реально занимало 20-30+ секунд в логах) — Telegram
    # отклоняет подтверждение callback-запроса примерно через 15 секунд
    # ('query is too old and response timeout expired'), это необработанное
    # исключение падало в глобальный error-handler и показывало пользователю
    # 'техническая ошибка' ПОСЛЕ того, как реальная работа уже была сделана
    # и результат отправлен — выглядело как случайный технический сбой на
    # ровном месте. Подтверждаем callback СРАЗУ, до долгой работы.
    await callback.answer()
    await state.update_data(has_data=False)
    await callback.message.answer(
        "Постараюсь найти данные сам, но учти — многие сайты блокируют "
        "автоматический поиск, может понадобиться твоя помощь."
    )

    data = await state.get_data()
    context_text = data.get("selected_idea") or data.get("org_info", "")
    context_text = (context_text or "").strip()

    found_text = ""
    if context_text:
        # Раньше искали по всему тексту идеи/организации целиком одним
        # запросом — длинный абзац как поисковый запрос почти не даёт
        # релевантных результатов (выглядело как "зацикливание на одних и
        # тех же стандартных сайтах", хотя на деле поиск просто не находил
        # ничего конкретного). Теперь формируем 2-3 КОРОТКИХ, разных по
        # фокусу поисковых запроса (конкретная статистика/цифры, а не
        # пересказ идеи) и объединяем результаты по всем.
        async with show_typing(callback.bot, callback.message.chat.id):
            queries = await generate_search_queries(context_text)
        seen_urls = set()
        all_results = []
        for q in queries:
            results = await try_search_statistics(q)
            for r in results:
                if r["url"] not in seen_urls:
                    seen_urls.add(r["url"])
                    all_results.append(r)
        if all_results:
            found_text = format_results_for_prompt(all_results[:8])

    if found_text:
        await callback.message.answer(
            "Нашёл несколько источников, которые могут пригодиться "
            "(проверь их перед использованием — это не гарантированно "
            "релевантные данные):\n\n" + found_text
        )
        project_data = (
            "Найдено веб-поиском (требует проверки пользователем перед "
            f"использованием):\n{found_text}"
        )
    else:
        await callback.message.answer(
            "Не нашёл ничего конкретного самостоятельно. Буду использовать "
            "оценочный подход — применю признанный международный бенчмарк "
            "(WHO/World Bank/ILO и т.п., где применимо) к местному охвату, "
            "с явной пометкой, что это оценка, а не измеренный факт."
        )
        project_data = "(данные не предоставлены — оценка на основе международного бенчмарка)"

    await state.update_data(project_data=project_data)
    await propose_goal_and_objectives(callback.message, state)
