import re

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from document_reader import UnsupportedFormatError, extract_text_from_telegram_file
from donor_form_cache import save_donor_form
from donor_scrape import try_scrape_donor_forms
from keyboards import (
    donor_form_choice_keyboard,
    donor_manual_keyboard,
    eligibility_check_keyboard,
    idea_defined_keyboard,
    more_or_continue_keyboard,
)
from llm import (
    answer_user_question,
    assess_eligibility_against_org,
    detect_doc_language,
    extract_donor_eligibility_criteria,
    extract_donor_template_structure,
    generate_ideas,
    label_donor_form,
    looks_like_question,
    match_intent,
    summarize_understanding,
)
from states import ProjectFlow
from typing_indicator import show_typing, show_working

router = Router()

URL_RE = re.compile(r"https?://\S+")


@router.message(ProjectFlow.waiting_donor_info)
async def receive_donor_info(message: Message, state: FSMContext):
    text = message.text or message.caption or ""

    # Короткий вопрос без документа/ссылки ("этого достаточно?", "а что
    # дальше?") — раньше молча проглатывался как donor_info и бот просто
    # ехал дальше, игнорируя вопрос. Отвечаем коротко и ждём реальные данные.
    if not message.document and looks_like_question(text) and not URL_RE.search(text):
        async with show_typing(message.bot, message.chat.id):
            answer = await answer_user_question(text, "приём информации о доноре/конкурсе")
        if answer:
            await message.answer(answer)
        return

    # РЕАЛЬНЫЙ ИНЦИДЕНТ: сообщение "почему ты остановился и завис? продолжай
    # с последнего места" (жалоба, не заканчивающаяся вопросительным знаком
    # в конце — 'зависший?' был в середине) прошло МИМО проверки выше и
    # ПЕРЕЗАПИСАЛО уже собранные donor_info/donor_forms_text этим текстом —
    # бот потом честно доложил на шаге бюджета 'на месте donor_info буквально
    # стоит текст «почему ты остановился...»'. looks_like_question() ловит
    # только короткие сообщения, ЗАКАНЧИВАЮЩИЕСЯ на '?' — реальные жалобы
    # часто не такие. Здесь — вторая линия защиты для сообщений БЕЗ URL и
    # БЕЗ документа, когда уже ЕСТЬ накопленные данные о доноре: явно
    # классифицируем, реальные ли это данные или разговорная реплика,
    # вместо того чтобы слепо считать текст данными и затирать существующие.
    if not message.document and not URL_RE.search(text):
        existing = (await state.get_data()).get("donor_info", "")
        if existing.strip():
            idx = await match_intent(text, [
                "Реальные данные о доноре — название, описание конкурса, критерии",
                "Разговорное сообщение боту — жалоба, вопрос, команда, не данные о доноре",
            ])
            if idx == 1:
                async with show_typing(message.bot, message.chat.id):
                    answer = await answer_user_question(text, "приём информации о доноре/конкурсе")
                if answer:
                    await message.answer(answer)
                else:
                    await message.answer(
                        "Данные о доноре уже сохранены — если хочешь что-то "
                        "добавить, пришли ссылку/файл/описание, или нажми "
                        "«дальше», если этого достаточно."
                    )
                return

    # Прикреплённый документ (в т.ч. ветка "manual_upload" — пользователь
    # присылает форму донора файлом вместо ссылки). Раньше это поле молча
    # игнорировалось: donor_info оставался пустой строкой, а бот на шаге
    # генерации идей потом честно жаловался "не хватает содержимого",
    # хотя пользователь файл уже прислал несколько шагов назад.
    if message.document:
        filename_lower = (message.document.file_name or "").lower()
        if filename_lower.endswith((".xlsx", ".xls")):
            # Пользователь прислал бюджетный Excel-шаблон донора вручную —
            # тот же путь, что и для xlsx, скачанных по ссылке: сохраняем
            # сырые байты, чтобы заполнить именно этот файл на шаге
            # согласования бюджета, а не терять его структуру, прогоняя
            # через обычный текстовый пайплайн.
            try:
                file = await message.bot.get_file(message.document.file_id)
                buf = await message.bot.download_file(file.file_path)
                content = buf.read()
            except Exception as exc:
                await message.answer(f"⚠️ Не смог скачать файл: {exc}")
                return
            from donor_scrape import _extract_xlsx_text
            xlsx_text = _extract_xlsx_text(content)
            await state.update_data(
                donor_budget_xlsx_content=content.hex(),
                donor_budget_xlsx_filename=message.document.file_name,
                donor_forms_text=xlsx_text,
            )
            await message.answer(
                f"✅ Принял бюджетный шаблон {message.document.file_name} — "
                "заполню его напрямую (с сохранением формул/структуры) на "
                "шаге согласования бюджета."
            )
            await _go_to_idea_check(message, state)
            return
        try:
            async with show_typing(message.bot, message.chat.id):
                doc_text = await extract_text_from_telegram_file(message.bot, message.document)
        except UnsupportedFormatError as e:
            await message.answer(f"⚠️ {e}")
            return
        if doc_text.strip():
            text += f"\n\n[Содержимое файла {message.document.file_name}]:\n{doc_text}"
            async with show_typing(message.bot, message.chat.id):
                template = await extract_donor_template_structure(doc_text)
            # Пользователь уже сам прислал этот файл — сохраняем локальную
            # копию (тот же кэш, что donor_scrape.py использует для
            # скачанных по ссылке форм), чтобы приложить его снова рядом с
            # финальным документом в конце, единым способом для обеих веток.
            donor_form_paths = []
            if template:
                try:
                    file = await message.bot.get_file(message.document.file_id)
                    buf = await message.bot.download_file(file.file_path)
                    saved_path = save_donor_form(buf.read(), message.document.file_name)
                    donor_form_paths = [saved_path]
                except Exception:
                    pass  # не критично — просто не сможем повторно приложить файл в финале
            await state.update_data(
                donor_info=text,
                donor_forms_text=doc_text,
                donor_template=template,
                donor_form_paths=donor_form_paths,
            )
            if template:
                await message.answer(
                    f"✅ Прочитал {message.document.file_name} — это официальная форма "
                    "донора, финальный документ будет заполнен строго по её структуре."
                )
            else:
                await message.answer(
                    f"✅ Прочитал содержимое {message.document.file_name}, но это похоже "
                    "не сама форма заявки (структура разделов не найдена) — финальный "
                    "документ будет в универсальной структуре. Если у донора есть "
                    "отдельная форма заявки — пришли именно её."
                )
            await _go_to_idea_check(message, state)
        else:
            await message.answer(
                f"⚠️ Не смог извлечь текст из {message.document.file_name} "
                "(файл повреждён, пустой, или это скан без текстового слоя). "
                "Пришли другой файл или опиши донора текстом."
            )
        return

    # Копим donor_info через несколько сообщений вместо перезаписи — тот же
    # класс бага, что раньше чинили в org_info.py (второе сообщение стирало
    # первое).
    existing_donor_info = (await state.get_data()).get("donor_info", "")
    combined_donor_info = f"{existing_donor_info}\n\n{text}".strip() if existing_donor_info.strip() else text
    await state.update_data(donor_info=combined_donor_info)

    url_match = URL_RE.search(text)
    if url_match:
        async with show_typing(message.bot, message.chat.id):
            forms, page_text = await try_scrape_donor_forms(url_match.group(0))
        await state.update_data(donor_page_text=page_text)
        if forms:
            readable = [f for f in forms if f["text"].strip()]
            unreadable = [f for f in forms if not f["text"].strip()]
            lines = []
            if unreadable:
                lines.append(
                    "⚠️ Нашёл, но не смог извлечь текст (формат .doc или "
                    "защищённый PDF) — потребуется скопировать вручную:\n"
                    + "\n".join(f["url"] for f in unreadable)
                )
            if lines:
                await message.answer("\n\n".join(lines))

            # Пересылаем пользователю РЕАЛЬНЫЙ скачанный файл формы (не
            # просто описание содержимого) и сохраняем путь на диске, чтобы
            # снова приложить его рядом с финальным документом позже.
            saved_paths = []
            budget_xlsx_content = None
            budget_xlsx_filename = None
            for f in forms:
                content = f.get("content") or b""
                if not content:
                    continue
                path = save_donor_form(content, f["filename"])
                saved_paths.append(path)
                if f.get("format") == "xlsx" and budget_xlsx_content is None:
                    # Бюджетный/финансовый Excel-шаблон донора — сохраняем
                    # сырые байты отдельно: раньше такой файл проходил через
                    # обычный текстовый пайплайн (markdown -> .docx) и в
                    # итоге терялась ЕГО структура (ячейки/формулы), донор же
                    # ожидает именно заполненный .xlsx. Заполняется позже, на
                    # шаге согласования бюджета (handlers/budget.py).
                    budget_xlsx_content = content
                    budget_xlsx_filename = f["filename"]
                try:
                    await message.answer_document(
                        FSInputFile(path),
                        caption=f"📎 Форма донора: {f['filename']}",
                    )
                except Exception:
                    pass  # пересылка не критична — текст уже в forms_text/donor_template
            if budget_xlsx_content is not None:
                await state.update_data(
                    donor_budget_xlsx_content=budget_xlsx_content.hex(),
                    donor_budget_xlsx_filename=budget_xlsx_filename,
                )

            if not readable:
                await message.answer(
                    "Прочитать содержимое найденных файлов не удалось (похоже, это "
                    "не сама форма заявки) — финальный документ будет в универсальной "
                    "структуре. Если у донора есть отдельная форма заявки, пришли её "
                    "файлом или ссылкой отдельно."
                )
                await state.update_data(donor_forms=[f["url"] for f in forms], donor_form_paths=saved_paths)
                await _go_to_idea_check(message, state)
                return

            await state.update_data(donor_forms=[f["url"] for f in forms], donor_form_paths=saved_paths)

            if len(readable) == 1:
                await _finalize_single_donor_form(message, state, readable[0])
                return

            # Донор дал НЕСКОЛЬКО разных форм на одной странице (частый
            # случай: основная заявка + форма командировочных/бюджетная) —
            # раньше все тексты слепо склеивались в один promt и структура
            # извлекалась из этой смеси, из-за чего в финале бот выдавал
            # то ли смесь двух форм, то ли не ту форму. Теперь классифицируем
            # каждую отдельно и явно спрашиваем пользователя, какую заполнять.
            await message.answer(
                f"Нашёл {len(readable)} разных документа по ссылке — донор мог "
                "опубликовать несколько форм (например, основную заявку и "
                "отдельно форму расходов на поездки). Смотрю, что это за формы..."
            )
            async with show_typing(message.bot, message.chat.id):
                labels = [await label_donor_form(f["filename"], f["text"]) for f in readable]
            # РЕАЛЬНЫЙ ИНЦИДЕНТ: readable — список dict с ключом "content"
            # (СЫРЫЕ БАЙТЫ файла из donor_scrape.py) — сохранение этого
            # целиком в FSM state крашило file_storage.py (JSON не умеет
            # сериализовать bytes) без ЛЮБОГО ответа пользователю: сообщение
            # просто пропадало ("Object of type bytes is not JSON
            # serializable" в логе), выглядело как будто бот завис намертво.
            # Раньше на MemoryStorage это молча работало (объект держался в
            # памяти процесса, JSON не требовался), баг всплыл только после
            # перехода на файловое хранилище. Храним только "url"/"filename"/
            # "text"/"format" — bytes не нужны на этом шаге (файл уже
            # переслан пользователю и сохранён на диск через save_donor_form
            # чуть выше).
            candidates_safe = [
                {"url": f["url"], "filename": f["filename"], "text": f["text"], "format": f.get("format", "other")}
                for f in readable
            ]
            await state.update_data(donor_form_candidates=candidates_safe, donor_form_candidate_labels=labels)
            await state.set_state(ProjectFlow.donor_form_selection)
            listing = "\n".join(f"{i + 1}. {label}" for i, label in enumerate(labels))
            await message.answer(
                f"Нашёл несколько документов:\n{listing}\n\nКакую из них "
                "заполняем как основную заявку на этот проект?",
                reply_markup=donor_form_choice_keyboard(labels),
            )
            return
        await message.answer(
            "Не смог скачать формы автоматически (сайт защищён от ботов "
            "или требует авторизации). Выбери:",
            reply_markup=donor_manual_keyboard(),
        )
        return

    # Нет URL в сообщении — считаем, что это либо документ, либо текст
    # донор-информации (в т.ч. вручную скопированный текст формы после
    # manual_upload/manual_copy), и идём дальше.
    async with show_typing(message.bot, message.chat.id):
        summary = await summarize_understanding("Донор/инвестор", text)
        template = await extract_donor_template_structure(text)
    if template:
        await state.update_data(donor_forms_text=text, donor_template=template)
        await message.answer(
            "📋 Это похоже на официальную форму заявки донора — финальный "
            "документ будет заполнен строго по её структуре."
        )
    elif summary:
        await message.answer(f"✅ Понял: {summary}")
    # РАНЬШЕ бот сразу и безусловно ехал дальше по сценарию — теперь даём
    # пользователю решить, есть ли ещё что добавить, вместо принудительного
    # перехода (та же логика, что и на шаге org_info).
    await message.answer(
        "Есть ещё что добавить про донора, или переходим дальше?",
        reply_markup=more_or_continue_keyboard(),
    )


@router.callback_query(ProjectFlow.waiting_donor_info, F.data == "step:more")
async def donor_info_more(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("Хорошо, пришли ещё информацию о доноре.")


@router.callback_query(ProjectFlow.waiting_donor_info, F.data == "step:continue")
async def donor_info_continue(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _go_to_idea_check(callback.message, state)


@router.callback_query(ProjectFlow.donor_form_selection, F.data.startswith("donorform:select:"))
async def donor_form_selected(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    candidates = data.get("donor_form_candidates", [])
    idx = int(callback.data.split(":")[-1])
    if idx < 0 or idx >= len(candidates):
        await callback.message.answer("Не понял выбор, попробуй ещё раз.")
        return
    chosen = candidates[idx]
    await _finalize_single_donor_form(callback.message, state, chosen)


async def _finalize_single_donor_form(message: Message, state: FSMContext, form: dict) -> None:
    """Общий финальный шаг после того, как определилась ОДНА конкретная
    форма для заполнения (либо она была единственной найденной, либо
    пользователь выбрал её из нескольких): извлекает структуру именно
    этой формы, сохраняет её путь как единственный donor_form_paths (не
    все скачанные файлы — иначе в финале бот прикладывал бы лишние формы,
    которые не заполнялись, как это было раньше), и идёт дальше."""
    async with show_typing(message.bot, message.chat.id):
        template = await extract_donor_template_structure(form["text"])
    data = await state.get_data()
    saved_paths = data.get("donor_form_paths", [])
    # Из всех скачанных файлов оставляем путь только к ВЫБРАННОЙ форме —
    # соответствие по filename (save_donor_form кладёт файл по этому имени).
    chosen_paths = [p for p in saved_paths if form["filename"] in p] or saved_paths[:1]
    await state.update_data(
        donor_forms_text=form["text"],
        donor_template=template,
        donor_form_paths=chosen_paths,
    )
    if template:
        await message.answer(
            f"📋 Заполняю «{form['filename']}» — финальный документ будет "
            "по её структуре, а не в свободном формате."
        )
    else:
        await message.answer(
            "⚠️ Не нашёл в этом файле структуру формы заявки (похоже, это не "
            "сама форма) — финальный документ будет в универсальной структуре."
        )
    await _go_to_idea_check(message, state)


@router.callback_query(ProjectFlow.waiting_donor_info, F.data == "donor:manual_upload")
async def donor_manual_upload(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Хорошо, пришли файлы формы сообщением.")
    await callback.answer()


@router.callback_query(ProjectFlow.waiting_donor_info, F.data == "donor:manual_copy")
async def donor_manual_copy(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "Обычно нужно зарегистрироваться на портале — доступ к форме "
        "придёт на почту. После регистрации скопируй текст всех вопросов "
        "формы и пришли мне сообщением."
    )
    await callback.answer()


async def _go_to_idea_check(message: Message, state: FSMContext):
    """Раньше вела прямо к вопросу 'проблема/идея уже определена?'. Теперь,
    если со страницы донора удалось вытащить конкретные критерии отбора
    (кто может подавать, география, размер гранта и т.п.), СНАЧАЛА
    сверяет их с уже присланной информацией об организации и даёт СВОЮ
    рекомендацию (это же его работа — сверять критерии, а не заставлять
    пользователя гадать самому), а затем в любом случае явно спрашивает,
    продолжать ли с этим донором — финальное решение всегда за
    пользователем, бот не блокирует прогресс сам."""
    data = await state.get_data()

    # РЕАЛЬНЫЙ ИНЦИДЕНТ: раньше бот отдельным шагом СПРАШИВАЛ пользователя,
    # на каком языке должен быть готовый документ — избыточно, т.к. заявка
    # подаётся на языке сайта/формы самого донора (реальное требование
    # конкурса), а не на языке, выбранном пользователем произвольно.
    # Определяем язык автоматически по любому доступному тексту донора
    # (страница конкурса приоритетнее — это первоисточник требования;
    # если её нет, используем текст скачанной/присланной формы) — только
    # один раз за сессию (doc_language ещё не установлен).
    if not data.get("doc_language"):
        donor_text_for_lang = data.get("donor_page_text", "") or data.get("donor_forms_text", "")
        if donor_text_for_lang.strip():
            detected = await detect_doc_language(donor_text_for_lang)
            if detected:
                await state.update_data(doc_language=detected)
                data = await state.get_data()

    page_text = data.get("donor_page_text", "")
    if page_text.strip() and not data.get("eligibility_checked"):
        async with show_typing(message.bot, message.chat.id):
            criteria = await extract_donor_eligibility_criteria(page_text)
        if criteria:
            await state.update_data(eligibility_criteria=criteria, eligibility_checked=True)
            await state.set_state(ProjectFlow.eligibility_check)
            org_info = data.get("org_info", "")
            assessment = ""
            if org_info.strip():
                async with show_typing(message.bot, message.chat.id):
                    assessment = await assess_eligibility_against_org(criteria, org_info)
            text_parts = [f"📋 На странице донора нашёл такие критерии отбора:\n\n{criteria}"]
            if assessment:
                text_parts.append(f"🔎 Моя оценка по твоей организации:\n{assessment}")
            text_parts.append("Продолжаем разработку проекта под этого донора?")
            await message.answer("\n\n".join(text_parts), reply_markup=eligibility_check_keyboard())
            return
        await state.update_data(eligibility_checked=True)
    await _ask_idea_defined(message, state)


@router.callback_query(ProjectFlow.eligibility_check, F.data == "eligibility:yes")
async def eligibility_yes(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("✅ Отлично, продолжаем разработку проекта под этого донора.")
    await _ask_idea_defined(callback.message, state)


@router.callback_query(ProjectFlow.eligibility_check, F.data == "eligibility:no")
async def eligibility_no(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    await callback.message.answer(
        "Понял — тогда под этого донора подавать напрямую не стоит. Но есть "
        "два варианта:\n\n"
        "1️⃣ Найти организацию/компанию, которая соответствует критериям и "
        "может подать от своего имени (например, как партнёр или "
        "исполнитель) — тогда ты сможешь реализовать идею через неё.\n"
        "2️⃣ Посмотреть других доноров под твой профиль.\n\n"
        "А пока — вот несколько направлений идей, которые могут подойти "
        "именно для твоей организации/тебя лично, независимо от этого "
        "конкретного донора:"
    )
    async with show_working(callback.message, "⏳ Генерирую идеи, это может занять до минуты..."):
        async with show_typing(callback.bot, callback.message.chat.id):
            note, ideas = await generate_ideas(
                data.get("org_info", ""),
                "",  # без привязки к донору, чьим критериям не соответствуем
                data.get("flow", "grant"),
                "",
            )
    from keyboards import idea_select_keyboard
    await state.update_data(generated_ideas=ideas, idea_defined=False)
    ideas_text = "\n\n".join(f"{i + 1}. {idea}" for i, idea in enumerate(ideas))
    intro = f"{note}\n\n" if note else ""
    await callback.message.answer(
        f"{intro}Вот несколько идей:\n\n{ideas_text}\n\nВыбери одну, а дальше "
        "можем вместе поискать донора/организацию под неё:",
        reply_markup=idea_select_keyboard(ideas),
    )
    await state.set_state(ProjectFlow.idea_defined_check)


async def _ask_idea_defined(message: Message, state: FSMContext):
    await state.set_state(ProjectFlow.idea_defined_check)
    data = await state.get_data()
    flow = data.get("flow", "grant")
    question = (
        "Проблема проекта уже определена?"
        if flow == "grant"
        else "Бизнес-идея уже определена?"
    )
    await message.answer(question, reply_markup=idea_defined_keyboard())
