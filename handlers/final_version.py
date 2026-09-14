import os
import tempfile
import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

import config
from docgen import markdown_to_docx
from keyboards import (
    final_version_keyboard,
    paid_revision_offer_keyboard,
    red_flags_keyboard,
    restart_keyboard,
)
from llm import (
    SYSTEM_PROMPT,
    LLMEmptyResponseError,
    autofix_red_flags,
    call_claude_required,
    check_red_flags,
    doc_language_clause,
    format_issues_for_user,
    generate_final_document,
    answer_user_question,
    is_actual_document,
    looks_like_question,
    summarize_xyz_placeholders,
)
from states import ProjectFlow
from telegram_text import send_long
from typing_indicator import show_typing, show_working

router = Router()

logger = logging.getLogger("fund4pro.final_version")

CLOSING_MESSAGE = (
    "Готово! Перед подачей — обязательно перечитай документ сам и проверь "
    "все цифры/факты (я мог ошибиться). Если после этого захочешь ещё "
    "что-то поправить — просто напиши мне, что именно, я подправлю."
)

LIMIT_REACHED_MESSAGE = (
    "Готово, вот обновлённая версия. ⚠️ Обрати внимание: ты уже превысил "
    "рекомендованное число бесплатных версий на этот проект "
    "({limit}) — пока лимит не блокирует работу, но скоро он станет "
    "действующим (лимиты стоят денег), так что дальше используй правки "
    "разумно."
)


async def send_final_version(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        async with show_typing(message.bot, message.chat.id):
            final_text = await generate_final_document(data)

            # Red-flags аудит (§1.2 плейбука) перед показом документа пользователю.
            # Не расходует лимит full_version_count — это внутренний цикл качества,
            # а не отдельная "версия" в смысле ТЗ.
            final_text = await _run_red_flags_gate(state, message, final_text, data)
    except LLMEmptyResponseError as e:
        logger.warning("send_final_version failed: %s", e)
        if "отказалась генерировать" in str(e):
            # Модель написала текстовый отказ вместо документа — обычно
            # значит, что donor_forms_text/donor_template содержит не сам
            # текст формы, а служебную заглушку/пустоту (например, из
            # сломанного шага скрапинга донора). Раньше это молча уходило
            # пользователю оформленным как готовый .docx — теперь честно
            # объясняем, что не хватает, вместо непонятного "перегрузка API".
            await message.answer(
                "⚠️ Не смог собрать финальный документ — модели не хватило "
                "данных о структуре формы донора/бюджете, чтобы уверенно "
                "заполнить заявку (а не выдумывать). Проверь на шаге 2, "
                "что форма донора была реально прочитана (бот должен был "
                "написать 'заполняю форму по её структуре'), и что бюджет "
                "на предыдущем шаге был согласован полностью, без обрывов. "
                "Если что-то не так — пришли текст/файл формы донора ещё "
                "раз и попробуй снова."
            )
        else:
            await message.answer(
                "⚠️ Не удалось получить финальную версию — модель Anthropic "
                "не ответила после нескольких попыток (перегрузка API или "
                "сетевой сбой). Подожди минуту и нажми кнопку ещё раз."
            )
        return
    if final_text is None:
        # Пользователь выбрал "я пришлю правки сам" — ждём его сообщение,
        # см. handlers/final_version.py::receive_red_flag_manual_fix ниже.
        await state.update_data(final_document_text_pending=None)
        return

    # РЕАЛЬНЫЙ ИНЦИДЕНТ: то же, что и в _apply_final_revision — модель может
    # вернуть текстовый отказ/уточняющий вопрос вместо документа (например,
    # если donor_template/бюджет неполные), и это молча упаковывалось в .docx
    # с враньём "готов по структуре донора".
    if not await is_actual_document(final_text):
        await send_long(message, final_text, reply_markup=restart_keyboard())
        return

    count = data.get("full_version_count", 0) + 1
    await state.update_data(final_document_text=final_text, full_version_count=count)
    await state.set_state(ProjectFlow.final_version)
    await _send_docx(message, final_text, data)
    await _send_donor_form_reminder(message, data)

    # Пользователь просил: явно, но КРАТКО показывать, в каких местах
    # финального документа остались XYZ-плейсхолдеры (не общей фразой,
    # а конкретными точками) — иначе пользователю приходится вычитывать
    # весь документ вручную в поисках недостающих данных.
    xyz_summary = summarize_xyz_placeholders(final_text)
    if xyz_summary:
        await send_long(message, xyz_summary)

    over_soft_limit = count >= config.FULL_VERSION_LIMIT
    limit_reached = over_soft_limit and config.ENFORCE_FULL_VERSION_LIMIT
    if limit_reached and config.PAYMENT_ENABLED:
        caption = (
            "Готовый документ выше. Бесплатный лимит версий "
            f"({config.FULL_VERSION_LIMIT}) достигнут — можешь завершить "
            "или купить ещё одну итерацию правок."
        )
        await message.answer(caption, reply_markup=paid_revision_offer_keyboard())
    elif limit_reached:
        caption = (
            "Готовый документ выше. Бесплатный лимит версий "
            f"({config.FULL_VERSION_LIMIT}) достигнут."
        )
        await message.answer(caption, reply_markup=final_version_keyboard(count, limit_reached=True))
    elif over_soft_limit:
        caption = (
            "Готовый документ выше. ⚠️ Ты уже превысил рекомендованное "
            f"число бесплатных версий ({config.FULL_VERSION_LIMIT}) — пока "
            "это не блокирует работу, но скоро лимит станет действующим "
            "(он стоит денег), используй правки разумно."
        )
        await message.answer(caption, reply_markup=final_version_keyboard(count))
    else:
        await message.answer("Готовый документ выше.", reply_markup=final_version_keyboard(count))


async def _send_donor_form_reminder(message: Message, session_data: dict) -> None:
    """Если у донора была официальная форма (найденная на шаге 2), просто
    подтверждаем, что итоговый final_version.docx уже заполнен по её
    структуре.

    РАНЬШЕ здесь повторно прикладывался ПУСТОЙ оригинальный файл формы с
    подписью "переноси текст выше именно в неё" — это была логическая
    ошибка: final_version.docx УЖЕ является формой, заполненной ботом
    (generate_final_document получает donor_template и пишет прямо в её
    структуру), а не отдельным текстом, который пользователь должен сам
    куда-то переносить. Пересылка пустого шаблона рядом с уже заполненным
    документом только путала (выглядело как будто бот прислал 'домашнее
    задание' вместо готовой работы) — и если донор давал несколько разных
    форм (например, основная заявка + форма командировочных), пересылались
    ОБЕ, даже те, что вообще не заполнялись.
    """
    if not session_data.get("donor_template"):
        return
    await message.answer(
        "📋 Документ выше уже заполнен по структуре формы донора — "
        "проверь перед отправкой, но переносить текст вручную никуда не нужно."
    )


async def _run_red_flags_gate(state: FSMContext, message: Message, document_text: str, session_data: dict) -> str | None:
    """Прогоняет документ через red-flags аудит. При найденных проблемах
    пытается автоисправить (до config.RED_FLAG_AUTOFIX_ATTEMPTS раз), затем
    если проблемы всё ещё есть — показывает их пользователю с выбором
    "исправь сам" / "я пришлю правки" и возвращает None (документ пока не
    отправляется). Возвращает финальный (возможно исправленный) текст, если
    в итоге всё чисто."""
    text = document_text
    for _ in range(max(config.RED_FLAG_AUTOFIX_ATTEMPTS, 0) + 1):
        result = await check_red_flags(text, session_data)
        if result.get("passed"):
            return text
        issues = result.get("issues", [])
        if not issues:
            return text
        text = await autofix_red_flags(text, issues, session_data)

    # Автоисправление не помогло за отведённое число попыток — показываем
    # пользователю найденные проблемы и даём выбор, не отправляя документ.
    final_check = await check_red_flags(text, session_data)
    if final_check.get("passed"):
        return text

    return await _offer_red_flags_choice(state, message, text, final_check.get("issues", []))


async def _offer_red_flags_choice(state: FSMContext, message: Message, document_text: str, issues: list[dict]) -> None:
    # Сохраняем текст+проблемы, чтобы redflags_autofix мог сделать
    # ТОЧЕЧНОЕ исправление вместо перегенерации документа с нуля.
    await state.update_data(
        final_document_text_pending=document_text,
        final_document_pending_issues=issues,
    )
    await send_long(message, format_issues_for_user(issues), reply_markup=red_flags_keyboard())
    return None


@router.callback_query(F.data == "redflags:autofix")
async def redflags_autofix(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    text = data.get("final_document_text_pending") or data.get("final_document_text", "")
    issues = data.get("final_document_pending_issues", [])
    if not text or not issues:
        # Нет сохранённого текста/проблем (например, состояние потерялось
        # между перезапусками бота) — откатываемся к полной перегенерации
        # как раньше, но это уже крайний случай, не основной путь.
        async with show_working(callback.message, "⏳ Перегенерирую документ с учётом замечаний..."):
            await send_final_version(callback.message, state)
        return
    async with show_working(callback.message, "⏳ Исправляю найденные места..."):
        try:
            fixed = await autofix_red_flags(text, issues, data)
        except Exception as e:
            logger.warning("redflags_autofix failed: %s", e)
            await callback.message.answer(
                "⚠️ Не удалось исправить автоматически. Нажми «Я пришлю правки» "
                "и опиши, что поправить, текстом."
            )
            return
        # Одна повторная проверка red-flags, БЕЗ дальнейшего авто-цикла —
        # если и сейчас есть проблемы, показываем их пользователю сразу,
        # не гоняя по кругу автоматически (это и было причиной зацикливания).
        result = await check_red_flags(fixed, data)

    # РЕАЛЬНЫЙ ИНЦИДЕНТ: та же защита, что и в send_final_version /
    # _apply_final_revision — не упаковывать в .docx текстовый отказ модели.
    if not await is_actual_document(fixed):
        await send_long(callback.message, fixed, reply_markup=restart_keyboard())
        return

    count = data.get("full_version_count", 0) + 1
    await state.update_data(final_document_text=fixed, full_version_count=count)
    await state.set_state(ProjectFlow.final_version)
    await _send_docx(callback.message, fixed, data)
    await _send_donor_form_reminder(callback.message, data)
    xyz_summary = summarize_xyz_placeholders(fixed)
    if xyz_summary:
        await send_long(callback.message, xyz_summary)
    if not result.get("passed") and result.get("issues"):
        await callback.message.answer(
            "Исправил, что смог. Ещё осталось на что обратить внимание "
            "(можешь поправить вручную или прислать новые правки):"
        )
        await send_long(callback.message, format_issues_for_user(result["issues"]))
    else:
        await callback.message.answer("Готово, вот исправленная версия.", reply_markup=final_version_keyboard(count))


@router.callback_query(F.data == "redflags:manual")
async def redflags_manual(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProjectFlow.waiting_final_revision)
    await callback.message.answer(
        "Хорошо, пришли свои правки — учту их и попробую снова."
    )
    await callback.answer()


@router.callback_query(ProjectFlow.final_version, F.data == "final:done")
async def final_done(callback: CallbackQuery, state: FSMContext):
    # РАНЬШЕ здесь state.clear() полностью стирал сессию сразу после
    # "Финально" — реальный инцидент: пользователь написал "в смысле, это
    # должен сделать ты, а не я" в ответ на прежний текст CLOSING_MESSAGE,
    # но попал в общий fallback-хендлер приветствия (состояние уже было
    # очищено), а не получил ответ по существу. Теперь сессия остаётся
    # активной ещё некоторое время — если пользователь напишет что-то ещё,
    # это по-прежнему обработается как правка/вопрос по документу, а не
    # проигнорируется общим приветствием. Полный сброс — только через явный
    # /start или "Начать заново".
    await callback.message.answer(CLOSING_MESSAGE)
    await callback.answer()


@router.callback_query(ProjectFlow.final_version, F.data == "final:revise")
async def final_revise(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if config.ENFORCE_FULL_VERSION_LIMIT and data.get("full_version_count", 0) >= config.FULL_VERSION_LIMIT:
        await callback.answer("Лимит версий достигнут", show_alert=True)
        return
    await state.set_state(ProjectFlow.waiting_final_revision)
    await callback.message.answer("Какие правки внести в финальную версию?")
    await callback.answer()


@router.callback_query(ProjectFlow.final_version, F.data == "final:pay_revise")
async def final_pay_revise(callback: CallbackQuery, state: FSMContext):
    # Активна только если config.PAYMENT_ENABLED=true (иначе эта кнопка
    # никогда не показывается — см. keyboards.paid_revision_offer_keyboard
    # и send_final_version выше).
    if not config.PAYMENT_ENABLED:
        await callback.answer("Оплата пока не подключена", show_alert=True)
        return
    from payments import send_paid_revision_invoice
    await send_paid_revision_invoice(callback.bot, callback.message.chat.id)
    await callback.answer()


@router.message(ProjectFlow.waiting_final_revision)
async def receive_final_revision(message: Message, state: FSMContext):
    text = message.text or message.caption or ""
    # РЕАЛЬНЫЙ ИНЦИДЕНТ: пользователь прислал сообщение без текста (стикер/
    # фото без подписи/голосовое) в состоянии ожидания правок — пустая
    # строка ушла в call_claude как user-контент, Anthropic API отклоняет
    # ПУСТОЙ user-message ('messages.0: user messages must have non-empty
    # content') с 400 Bad Request, что после 3 ретраев показывалось
    # пользователю как невнятная 'модель не ответила' ошибка.
    if not text.strip():
        await message.answer(
            "Не увидел текста в этом сообщении — опиши правки словами, "
            "текстом."
        )
        return
    await _apply_final_revision(message, state, text)


# РАНЬШЕ на состоянии final_version (документ уже показан, бот ждёт кнопку
# "Финально"/"Версия #2") любой текст полностью игнорировался с ответом
# "жду кнопку" — даже содержательная жалоба на качество документа. Это и
# есть "жёсткие рамки": бот не понимал обычный язык, только клики. Теперь
# СВОБОДНЫЙ ТЕКСТ здесь сразу трактуется как правки (тот же путь, что и
# явное нажатие "Версия #2") — кнопки остаются для быстрого выбора, но
# не единственный канал ввода.
# РЕАЛЬНЫЙ ИНЦИДЕНТ: пользователь написал "начни сначала" в состоянии
# final_version — бот принял это за ПРАВКУ к готовому документу, ушёл к LLM
# с промптом "внеси правки: начни сначала", получил честный текстовый отказ
# модели ("не вижу файлов/истории, пришлите шаблон заново") и всё равно
# упаковал этот отказ в .docx, отправив с враньём "документ уже заполнен по
# структуре донора". Теперь явные фразы намерения перезапуска перехватываются
# ДО ветки правок и ведут на настоящий сброс сессии (как кнопка "Начать
# заново"), а не молча становятся содержимым документа.
RESTART_PHRASES_RE = (
    "начни сначала", "начни с начала", "начать сначала", "начать с начала",
    "начать заново", "начни заново", "с нуля", "заново все", "заново всё",
    "сбрось", "сброс", "новый проект", "перезапусти",
)


def _looks_like_restart_request(text: str) -> bool:
    t = text.strip().lower()
    return any(p in t for p in RESTART_PHRASES_RE) and len(t) <= 60


@router.message(ProjectFlow.final_version)
async def final_version_free_text(message: Message, state: FSMContext):
    text = message.text or ""
    data = await state.get_data()

    if _looks_like_restart_request(text):
        from handlers.start import WELCOME
        from keyboards import start_keyboard
        await state.clear()
        await message.answer(
            "Хорошо, начинаем с чистого листа — прошлая сессия и документ сброшены."
        )
        await message.answer(WELCOME, reply_markup=start_keyboard())
        return

    # РЕАЛЬНЫЙ ИНЦИДЕНТ: этот хендлер отвечал КАНОНИЧЕСКОЙ фразой про лимит
    # версий на ЛЮБОЙ текст в этом состоянии — включая "привет" и "не понял
    # что это, объясни", полностью игнорируя, что реально написал
    # пользователь. Это ровно та жёсткая привязка (тут — не к кнопке, а к
    # одному жёстко закодированному ответу), от которой пользователь просил
    # избавиться. Теперь: если это похоже на вопрос/реплику, а не на
    # содержательную просьбу правки — отвечаем по существу через LLM.
    if looks_like_question(text) or len(text.strip()) < 8:
        async with show_typing(message.bot, message.chat.id):
            answer = await answer_user_question(
                text, "документ уже готов, ждём финального решения пользователя", data
            )
        if answer:
            await message.answer(answer, reply_markup=final_version_keyboard(
                data.get("full_version_count", 0),
                limit_reached=data.get("full_version_count", 0) >= config.FULL_VERSION_LIMIT,
            ))
            return

    if data.get("full_version_count", 0) >= config.FULL_VERSION_LIMIT and config.ENFORCE_FULL_VERSION_LIMIT:
        await message.answer(
            f"Ты уже получил {config.FULL_VERSION_LIMIT} бесплатных версии "
            "этого документа (генерация + правки) — это лимит на проект, "
            "не связан с работой самого ИИ. Твой текст учёл бы как ещё одну "
            "правку, но лимит исчерпан. Нажми «Финально», если документ "
            "устраивает как есть."
        )
        return
    await _apply_final_revision(message, state, text)


async def _apply_final_revision(message: Message, state: FSMContext, revision_request: str):
    data = await state.get_data()
    prompt = (
        f"{SYSTEM_PROMPT}\n\nВот текущая финальная версия:\n"
        f"{data.get('final_document_text', '')}\n\nВнеси правки: {revision_request}\n"
        f"Верни обновлённый документ целиком, с теми же markdown-заголовками."
        f"{doc_language_clause(data)}"
    )
    try:
        async with show_typing(message.bot, message.chat.id):
            revised = await call_claude_required(prompt, revision_request or "Внеси правки как описано выше.", max_tokens=6000)

            # Red-flags аудит применяется и к отредактированной версии — правки
            # пользователя тоже не должны молча протащить проблему в документ.
            revised = await _run_red_flags_gate(state, message, revised, data)
    except LLMEmptyResponseError as e:
        logger.warning("receive_final_revision failed: %s", e)
        await message.answer(
            "⚠️ Не удалось получить обновлённую версию — модель Anthropic "
            "не ответила после нескольких попыток. Подожди минуту и повтори "
            "сообщение с правками."
        )
        return
    if revised is None:
        return

    # РЕАЛЬНЫЙ ИНЦИДЕНТ: модель вернула текстовый отказ/уточняющий вопрос
    # (например, в ответ на "начни сначала", ошибочно принятое за правку)
    # вместо обновлённого документа — бот раньше молча упаковывал ЛЮБОЙ
    # ответ модели в .docx и заявлял "готово, по структуре донора", даже
    # когда внутри был текст вида "я не вижу файлов, пришлите шаблон".
    # Теперь такой ответ показывается пользователю как обычное сообщение
    # LLM, документ НЕ отправляется и счётчик версий не тратится.
    if not await is_actual_document(revised):
        await send_long(message, revised, reply_markup=restart_keyboard())
        return

    count = data.get("full_version_count", 0) + 1
    await state.update_data(final_document_text=revised, full_version_count=count)
    await state.set_state(ProjectFlow.final_version)
    await _send_docx(message, revised, data)
    await _send_donor_form_reminder(message, data)
    xyz_summary = summarize_xyz_placeholders(revised)
    if xyz_summary:
        await send_long(message, xyz_summary)

    over_soft_limit = count >= config.FULL_VERSION_LIMIT
    limit_reached = over_soft_limit and config.ENFORCE_FULL_VERSION_LIMIT
    if limit_reached and config.PAYMENT_ENABLED:
        await message.answer(
            "Готово, вот обновлённая версия. Бесплатный лимит достигнут — "
            "можешь завершить или купить ещё одну итерацию.",
            reply_markup=paid_revision_offer_keyboard(),
        )
    elif limit_reached:
        await message.answer(LIMIT_REACHED_MESSAGE.format(limit=config.FULL_VERSION_LIMIT), reply_markup=final_version_keyboard(count, limit_reached=True))
    elif over_soft_limit:
        await message.answer(LIMIT_REACHED_MESSAGE.format(limit=config.FULL_VERSION_LIMIT), reply_markup=final_version_keyboard(count))
    else:
        await message.answer("Готово, вот обновлённая версия.", reply_markup=final_version_keyboard(count))


async def _send_docx(message: Message, text: str, session_data: dict | None = None):
    if not text.strip():
        await message.answer(
            "⚠️ Итоговый текст оказался пустым — не отправляю файл. "
            "Попробуй сгенерировать версию заново."
        )
        return
    # Если у донора есть своя форма (donor_template) — документ УЖЕ содержит
    # оригинальный заголовок/структуру донора внутри text (см. llm.py::
    # generate_final_document). Добавлять поверх фиксированный заголовок
    # "Проект / Бизнес-план" в этом случае неверно — донор не примет
    # переименованный заголовок формы, и это выглядело как будто бот
    # самовольно меняет формулировки донора.
    has_donor_template = bool((session_data or {}).get("donor_template"))
    title = None if has_donor_template else "Проект / Бизнес-план"
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "final_version.docx")
        markdown_to_docx(text, title, path)
        await message.answer_document(FSInputFile(path))
