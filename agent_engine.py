"""Агентный движок (v3 редизайн) — заменяет цепочку изолированных FSM-шагов
одним персистентным диалогом с моделью, которая сама решает, когда что
спросить/сохранить/сгенерировать, вызывая инструменты (agent_tools.py) по
собственному усмотрению вместо того, чтобы код диктовал следующий шаг.

Поддерживает несколько провайдеров сквозным образом (Anthropic Tool Use и
OpenAI-совместимый tool calling для ChatGPT/Gemini/DeepSeek) — пробует
Anthropic первым, при любой ошибке (в т.ч. исчерпанный баланс) прозрачно
переключается дальше по цепочке ChatGPT -> Gemini -> DeepSeek, тем же
порядком, что и llm.call_claude (по явному запросу пользователя, 2026-09-18).
"""

import base64
import json
import logging

import config
from agent_roadmap import build_lite_system_prompt, build_system_prompt
from agent_tools import TOOLS_OPENAI, to_anthropic_tools
from llm import _client, _chatgpt_client, _deepseek_client, _fallback_client

logger = logging.getLogger("fund4pro.agent_engine")

# РЕАЛЬНЫЙ ИНЦИДЕНТ (23.09.2026, живой тест NED): после каждого собранного
# документа код-инструкция (см. "СРАЗУ переходи к следующему" в _execute_tool
# ниже) намеренно велит модели заполнить ВСЕ недостающие документы донора в
# ОДНОМ ходу, без "продолжай" от пользователя между файлами. У NED 4
# обязательных документа: fetch_donor_page (1) + select_donor_form +
# generate_document на каждый (4×2=8) = 9 раундов за один ход — почти вдвое
# больше прежнего лимита 5. Лог подтвердил: "hit MAX_TOOL_ROUNDS (5) without
# final text" — бот заполнил часть пакета и не дошёл до последнего (самого
# важного) документа, заявки на грант. Поднято с запасом на донора с ещё
# большим пакетом документов, не превращая это в бесконечный цикл — реальные
# защиты от зацикливания (empty_reply_retries, _is_empty_promise, статусы в
# donor_documents) не зависят от этого числа.
MAX_TOOL_ROUNDS = 20  # защита от зацикливания вызовов инструментов за один ход
PROJECT_DATA_FIELDS = (
    "org_info", "donor_info", "donor_template", "problem_and_idea",
    "goal_and_objectives", "activities_and_budget", "other_notes",
)


def _classify_donor_documents(saved_files: list[dict]) -> list[dict]:
    """РЕАЛЬНАЯ ЖАЛОБА ("новичок полностью доверяет боту, а бот заполнил одну
    форму из четырёх и забыл про остальные, пока ему не напомнили"): раньше
    единственным "реестром" документов донора была память модели внутри
    разговора — ход за ходом, без кода, который бы явно считал "сколько
    всего, сколько готово". Слабая модель (сейчас на проекте — единственная
    реально доступная, см. компанию заметок про лимиты Anthropic/Gemini)
    регулярно "забывала" про недособранные документы, пока пользователь не
    напоминал вручную — то же самое, что раньше было с этапом разговора
    (compute_next_step), просто теперь для документов.

    Строит реестр КОДОМ, а не памятью модели, сразу при скачивании — без
    единого вызова LLM: и extract_template_schema (.docx), и
    extract_pdf_form_schema (.pdf), и extract_xlsx_structure (.xlsx) уже
    чисто программные (только запись значений полей — отдельный, платный
    шаг). "kind" определяет, какой из трёх пайплайнов заполнения (docx/pdf/
    xlsx) сможет сам заполнить документ, а какой — чисто содержательный
    (гайдлайны без полей, где нет смысла звать pdf/docx-схему, а нужен
    generate_document с donor_template = структура этих разделов).
    Возвращает список {filename, kind, status: "pending"} — status потом
    обновляет _execute_tool после каждого успешного generate_document."""
    import io

    docs = []
    for f in saved_files:
        filename = f.get("filename", "")
        b64 = f.get("content_b64")
        if not b64:
            continue
        try:
            content = base64.b64decode(b64)
        except Exception:
            continue
        lower = filename.lower()
        kind = "unknown"
        try:
            if lower.endswith(".xlsx"):
                from excel_fill import extract_xlsx_structure
                kind = "budget" if extract_xlsx_structure(content).strip() else "unknown"
            elif lower.endswith(".pdf"):
                from pdf_form_fill import extract_pdf_form_schema
                kind = "form" if extract_pdf_form_schema(content) else "narrative"
            elif lower.endswith(".docx"):
                import docx
                from docx_schema_fill import extract_template_schema
                doc = docx.Document(io.BytesIO(content))
                fields = extract_template_schema(doc)
                # >=3 полей в таблицах — реальная форма с местами для ответа;
                # 0-2 — почти наверняка гайдлайны/инструкция (заголовки
                # разделов без ячеек-заполнителей, как у NED).
                kind = "form" if len(fields) >= 3 else "narrative"
        except Exception:
            logger.warning("_classify_donor_documents: could not classify %r", filename, exc_info=True)
        docs.append({"filename": filename, "kind": kind, "status": "pending"})
    return docs


class AgentTurnResult:
    def __init__(
        self,
        reply: str,
        document_ready: bool = False,
        document_text: str = "",
        tool_log: list[str] | None = None,
        quick_replies: list[str] | None = None,
        file_attachments: list[dict] | None = None,
        llm_unavailable: bool = False,
        generated_documents: list[dict] | None = None,
    ):
        self.llm_unavailable = llm_unavailable
        self.reply = reply
        self.document_ready = document_ready
        self.document_text = document_text
        self.tool_log = tool_log or []
        self.quick_replies = quick_replies or []
        self.file_attachments = file_attachments or []
        # Готовые файлы, собранные ПО ХОДУ (каждый — сразу после своего
        # generate_document, не одним последним "победителем" в конце хода
        # — см. комментарий на месте вызова export_docx в run_agent_turn).
        # [{"path", "filename", "official_template", "non_latin_warning"}, ...]
        self.generated_documents = generated_documents or []


async def _execute_tool(name: str, args: dict, session: dict) -> str:
    """Исполняет один вызов инструмента, мутирует session['project_data']
    на месте, возвращает строку-результат для передачи модели обратно."""
    project_data = session.setdefault("project_data", {})

    if name == "update_project":
        updated = []
        for field in PROJECT_DATA_FIELDS:
            value = args.get(field)
            if value and value.strip():
                existing = project_data.get(field, "")
                # Копим, а не перезаписываем — тот же класс бага, что чинили
                # в старой FSM-версии (второе сообщение стирало первое).
                if existing.strip() and existing.strip() != value.strip():
                    project_data[field] = f"{existing}\n\n{value}".strip()
                else:
                    project_data[field] = value.strip()
                updated.append(field)
        return f"Сохранено: {', '.join(updated) if updated else 'нечего сохранять (пустые поля)'}"

    if name == "fetch_donor_page":
        url = args.get("url", "").strip()
        if not url:
            return "Ошибка: не указан URL"
        from donor_scrape import try_scrape_donor_forms
        from donor_form_cache import save_donor_form
        try:
            forms, page_text = await try_scrape_donor_forms(url)
        except Exception as exc:
            logger.warning("fetch_donor_page failed: %s: %s", type(exc).__name__, exc)
            return f"Не удалось загрузить страницу ({type(exc).__name__}) — сообщи пользователю и попроси прислать текст/файл вручную."
        session["donor_page_text"] = page_text
        if not forms:
            summary = page_text[:2000] if page_text else ""
            if summary:
                return f"Страница загружена, файлов формы не найдено. Текст страницы:\n{summary}"
            return "Не удалось получить содержимое страницы (защита от ботов или пустой ответ)."

        # Сохраняем все найденные файлы на диск и готовим их отправку в чат Telegram пользователю
        saved_files = []
        pending_att = session.get("_pending_file_attachments", [])
        for f in forms:
            content = f.get("content") or b""
            if content:
                # РЕАЛЬНЫЙ ИНЦИДЕНТ: один файл с патологически длинным/
                # проблемным именем (см. save_donor_form) уронил ВЕСЬ ход
                # агента необработанным исключением — включая уже успешно
                # скачанные ДРУГИЕ файлы той же страницы (например, если
                # донор публикует форму на нескольких языках, а падает
                # только одна). Один плохой файл не должен топить всю
                # страницу — пропускаем его и продолжаем с остальными.
                try:
                    path = save_donor_form(content, f["filename"])
                except OSError:
                    logger.warning("save_donor_form failed for %r, skipping this file", f.get("filename"), exc_info=True)
                    continue
                f_entry = {
                    "filename": f["filename"],
                    "path": path,
                    "text": f.get("text", ""),
                    "url": f.get("url", ""),
                    # РЕАЛЬНЫЙ ИНЦИДЕНТ: Render free-tier стирает локальный
                    # диск при каждом засыпании/рестарте контейнера, а
                    # session (FSM data) хранится в Redis и переживает
                    # рестарт. save_donor_form пишет файл ТОЛЬКО на диск —
                    # после рестарта export_docx находит path в session, но
                    # самого файла там больше нет, и бот молча откатывается
                    # на generate-with-markdown (документ уже не в форме
                    # донора). Храним сами байты здесь же, в session, как
                    # запасной источник восстановления файла.
                    "content_b64": base64.b64encode(content).decode("ascii"),
                }
                saved_files.append(f_entry)
                pending_att.append({
                    "path": path,
                    "caption": f"📎 Форма донора: {f['filename']}",
                })
        session["saved_donor_files"] = saved_files
        session["_pending_file_attachments"] = pending_att

        # Реестр документов донора — считает КОД, не модель (см. докстринг
        # _classify_donor_documents). Не перезаписываем уже известные записи
        # (по filename), чтобы не сбросить status="filled" при повторном
        # fetch_donor_page той же страницы в этой же сессии.
        existing_docs = {d["filename"]: d for d in (project_data.get("donor_documents") or [])}
        for d in _classify_donor_documents(saved_files):
            if d["filename"] not in existing_docs:
                existing_docs[d["filename"]] = d
        project_data["donor_documents"] = list(existing_docs.values())

        readable = [f for f in forms if f.get("text", "").strip()]
        session["donor_form_candidates"] = [
            {"url": f["url"], "filename": f["filename"], "text": f["text"]} for f in readable
        ]
        if not readable:
            return (
                f"Страница загружена. Найдено {len(forms)} файл(а), они скачаны и отправляются пользователю в чат, "
                f"но не удалось автоматически извлечь текст (сканы или защищённый формат)."
            )

        from llm import extract_donor_template_structure, detect_doc_language, label_donor_form

        # Если найдена ровно 1 форма — сразу извлекаем её структуру
        if len(readable) == 1:
            structure = await extract_donor_template_structure(readable[0]["text"])
            if not session.get("doc_language"):
                detected = await detect_doc_language(readable[0]["text"])
                if detected:
                    session["doc_language"] = detected
            if structure:
                project_data["donor_template"] = structure
                session["chosen_donor_form"] = readable[0]["filename"]
                return (
                    f"Форма '{readable[0]['filename']}' скачана, отправлена пользователю файлом в чат и её официальная структура "
                    f"извлечена (сохранена как donor_template) — при generate_document заполняй строго её.\n\n"
                    f"Структура:\n{structure[:1500]}"
                )
            return f"Форма '{readable[0]['filename']}' скачана и отправлена в чат, но не похожа на официальную форму заявки."

        # Несколько форм (например main + travel) — определяем их назначение
        labels = []
        for f in readable:
            try:
                lbl = await label_donor_form(f["filename"], f["text"])
            except Exception:
                lbl = f["filename"]
            labels.append(lbl)

        for idx, lbl in enumerate(labels):
            session["donor_form_candidates"][idx]["label"] = lbl

        quick_opts = [f"{i+1}. {lbl[:20]}" for i, lbl in enumerate(labels)]
        quick_opts.append("Свой вариант...")
        session["_pending_quick_replies"] = quick_opts[:4]

        listing = "\n".join(f"- {f['filename']} ({labels[i]})" for i, f in enumerate(readable))
        # РЕАЛЬНАЯ ЖАЛОБА: бот нашёл несколько форм (у NED — Форма профиля
        # организации И Форма заявки на проект, ОБЕ обязательны для подачи,
        # не альтернативы вроде "основная/travel-приложение" у ГГФ) и спросил
        # "какую заполняем как ОСНОВНУЮ" — донор потом отклонит заявку за
        # неполный пакет документов, если приложена только одна форма.
        # Раньше формулировка сама подталкивала модель выбрать ровно одну.
        return (
            f"Страница загружена. Найдено {len(readable)} документов, они скачаны и отправлены пользователю файлами в чат:\n{listing}\n\n"
            f"ВАЖНО: сначала перечитай текст страницы донора (page_text, полученный этим же вызовом) — "
            f"донор МОЖЕТ требовать ВСЕ найденные официальные формы (не альтернативы вроде разных языков "
            f"или «основная/дополнительная для поездок» — а РАЗНЫЕ обязательные документы, например «форма "
            f"профиля организации» И отдельно «форма заявки на проект»). Если по тексту требований видно, что "
            f"нужно заполнить НЕСКОЛЬКО форм — прямо скажи пользователю это ('донор требует N документов: ...'), "
            f"НЕ спрашивай 'какую заполнить как основную'. Дальше заполняй их ПО ОДНОЙ: select_donor_form + "
            f"generate_document для первой формы, затем (в следующий ход) то же самое для второй, и так для "
            f"каждой обязательной формы — каждая генерирует свой отдельный файл. Кнопки выбора уже прикреплены "
            f"на случай, если формы — реально альтернативы (например языковые версии) и нужна только одна.\n\n"
            f"РЕАЛЬНАЯ ЖАЛОБА (частая ошибка): среди найденных документов бывает и ФОРМА С ПОЛЯМИ (короткие "
            f"технические факты — название организации, даты, суммы), и ОТДЕЛЬНО ГАЙДЛАЙНЫ/ИНСТРУКЦИЯ ПО "
            f"СОДЕРЖАНИЮ (заголовки разделов вроде 'Резюме проекта', 'История проекта', 'Задачи', 'Деятельность', "
            f"'План оценки' — БЕЗ полей для заполнения, это описание того, что нужно НАПИСАТЬ). Заполнение только "
            f"формы с полями — НЕ равно готовой заявке: реальное содержание проекта (проблема, цели, деятельность, "
            f"оценка) почти всегда идёт в этот ВТОРОЙ документ. Смотри на метки в скобках после имени файла выше "
            f"(из label_donor_form): «(поля)» — заполняем как форму; «(не форма)»/гайдлайны — тоже вызови "
            f"select_donor_form на НЕЙ, но результат — это структура (donor_template) для generate_document, "
            f"который напишет содержательный текст СТРОГО по её разделам (не по общему роадмапу «3 деревьев»). "
            f"НИКОГДА не говори пользователю, что заявка готова/собрана, пока не закрыты ОБА типа документов — "
            f"и поля, и содержание. Если среди найденных документов НЕТ ни одного с реальными содержательными "
            f"вопросами (только короткие технические формы), а текст страницы донора упоминает личный кабинет/"
            f"онлайн-заявку — сначала перечитай ВЕСЬ текст страницы (page_text) на предмет других файлов/ссылок, "
            f"и только если содержательных вопросов действительно нигде нет — прямо скажи пользователю: "
            f"содержательные вопросы заявки, похоже, есть только в личном кабинете донора, попроси "
            f"зарегистрироваться там и прислать текст этих вопросов (можно скриншотом или копипастой)."
        )

    if name == "select_donor_form":
        keyword = str(args.get("filename_or_keyword", "")).strip().lower()
        candidates = session.get("donor_form_candidates", [])
        if not candidates:
            candidates = session.get("saved_donor_files", [])
        if not candidates:
            # Fallback: проверить кэш на диске
            import os
            from donor_form_cache import CACHE_DIR
            from document_reader import _extract_docx, _extract_pdf
            if os.path.exists(CACHE_DIR):
                for fn in os.listdir(CACHE_DIR):
                    p = os.path.join(CACHE_DIR, fn)
                    try:
                        with open(p, "rb") as fp:
                            raw = fp.read()
                        if fn.endswith(".docx"):
                            txt = _extract_docx(raw)
                        elif fn.endswith(".pdf"):
                            txt = _extract_pdf(raw)
                        else:
                            txt = ""
                        if txt.strip():
                            clean_fn = fn.split("_", 1)[-1] if "_" in fn else fn
                            candidates.append({
                                "filename": clean_fn, "path": p, "text": txt,
                                "content_b64": base64.b64encode(raw).decode("ascii"),
                            })
                    except Exception:
                        pass
        if not candidates:
            return "Нет сохранённых форм донора. Сначала загрузи страницу через fetch_donor_page."

        chosen = None
        if keyword in ("1", "первая", "первый", "main", "основная", "проект", "главная"):
            for c in candidates:
                fn = c.get("filename", "").lower()
                lbl = c.get("label", "").lower()
                if "main" in fn or "проект" in fn or "основн" in lbl:
                    chosen = c
                    break
            if not chosen and candidates:
                chosen = candidates[0]
        elif keyword in ("2", "вторая", "второй", "travel", "поездки", "проездной"):
            for c in candidates:
                fn = c.get("filename", "").lower()
                lbl = c.get("label", "").lower()
                if "travel" in fn or "поезд" in fn or "проездн" in lbl:
                    chosen = c
                    break
            if not chosen and len(candidates) > 1:
                chosen = candidates[1]
        else:
            for c in candidates:
                fn = c.get("filename", "").lower()
                lbl = c.get("label", "").lower()
                if keyword in fn or keyword in lbl:
                    chosen = c
                    break
            if not chosen and candidates:
                chosen = candidates[0]

        from llm import extract_donor_template_structure, detect_doc_language
        form_text = chosen.get("text", "")
        if not form_text:
            return f"Форма '{chosen.get('filename')}' выбрана, но текст в ней пуст."

        structure = await extract_donor_template_structure(form_text)
        if not session.get("doc_language"):
            detected = await detect_doc_language(form_text)
            if detected:
                session["doc_language"] = detected
        if structure:
            project_data["donor_template"] = structure
            session["chosen_donor_form"] = chosen.get("filename")
            # Сохраняем путь к выбранному файлу для export_docx
            session["chosen_donor_form_path"] = chosen.get("path")
            # export_docx ищет файл по filename именно в saved_donor_files —
            # если chosen пришёл из donor_form_candidates или дискового кэша
            # (не из saved_donor_files), его там ещё нет. Прописываем/
            # обновляем запись, чтобы path и content_b64 (переживающий
            # рестарт контейнера, в отличие от локального диска) были
            # доступны экспорту независимо от того, откуда взят выбор.
            saved_files = session.get("saved_donor_files", [])
            for i, sf in enumerate(saved_files):
                if sf.get("filename") == chosen.get("filename"):
                    saved_files[i] = {**sf, **chosen}
                    break
            else:
                saved_files.append(chosen)
            session["saved_donor_files"] = saved_files
            return (
                f"Выбрана форма '{chosen.get('filename')}'. Её официальная структура извлечена и сохранена в donor_template:\n"
                f"{structure[:1500]}\n\n"
                f"При генерации документа (generate_document) теперь строго заполняются разделы этой формы донора."
            )
        return f"Форма '{chosen.get('filename')}' выбрана, но не удалось извлечь структуру."

    if name == "send_donor_form":
        keyword = str(args.get("filename_or_keyword", "")).strip().lower()
        files = session.get("saved_donor_files", [])
        if not files:
            import os
            from donor_form_cache import CACHE_DIR
            if os.path.exists(CACHE_DIR):
                cached = os.listdir(CACHE_DIR)
                files = [{"filename": fn.split("_", 1)[-1] if "_" in fn else fn, "path": os.path.join(CACHE_DIR, fn)} for fn in cached if fn.endswith(('.docx', '.pdf', '.xlsx'))]
        if not files:
            return "Файлы форм донора пока не найдены на диске."

        to_send = []
        if not keyword or keyword in ("все", "all", "шаблон", "формы", "документ", "ворд", "чистый"):
            to_send = files
        else:
            for f in files:
                fn = f.get("filename", "").lower()
                if keyword in fn or ("main" in keyword and "main" in fn) or ("travel" in keyword and "travel" in fn):
                    to_send.append(f)
            if not to_send and files:
                to_send = files

        import os
        pending = session.get("_pending_file_attachments", [])
        sent_count = 0
        for f in to_send:
            path = f.get("path")
            if path and os.path.exists(path):
                pending.append({"path": path, "caption": f"📎 Шаблон донора: {f.get('filename')}"})
                sent_count += 1
        session["_pending_file_attachments"] = pending
        return f"Файл(ы) шаблона донора ({sent_count} шт.) прикреплены и отправляются пользователю в чат."

    if name == "search_project_data":
        query = str(args.get("query") or "").strip()
        if not query:
            return "Ошибка: не указан поисковый запрос"

        # РЕАЛЬНЫЙ ИНЦИДЕНТ: старая цепочка (SearXNG/DuckDuckGo/Startpage —
        # скрейпинг HTML через httpx+BeautifulSoup) регулярно не находила
        # ничего по нишевым локальным запросам (например живой тест: "турпоток
        # Арсланбоб Сары-Челек" — 0 релевантных результатов), отчасти из-за
        # антибот-блокировки облачных IP Render. Нативный веб-поиск Claude
        # (llm.web_search_and_summarize) работает с инфраструктуры Anthropic,
        # не подвержен этой блокировке, и сразу возвращает синтезированный
        # ответ с источниками, а не сырые ссылки. Пробуется первым; при
        # недоступности (нет ключа/сбой) молча падает на старую цепочку —
        # ничего не ломается для случая, когда Anthropic сам недоступен.
        from llm import web_search_and_summarize
        try:
            org_info = (project_data.get("org_info") or "")[:300]
            summary, sources = await web_search_and_summarize(query, context=org_info)
        except Exception as exc:
            logger.warning("web_search_and_summarize failed, falling back to scraping chain: %s: %s", type(exc).__name__, exc)
            summary, sources = "", []

        if summary:
            session.setdefault("_found_data_notes", []).append(
                {"query": query, "summary": summary, "sources": sources[:5]}
            )
            src_lines = "\n".join(f"- {s['title']}: {s['url']}" for s in sources[:5])
            return (
                f"Найдено по запросу '{query}' (живой веб-поиск через Claude):\n{summary}\n\n"
                f"Источники:\n{src_lines}\n\n"
                f"ОБЯЗАТЕЛЬНО вызови update_project В ЭТОМ ЖЕ ХОДУ и сохрани "
                f"конкретные найденные цифры/факты (с источником) в подходящее поле "
                f"(problem_and_idea, если это про масштаб проблемы, или other_notes) "
                f"— иначе они останутся только в этом сообщении чата и не попадут в "
                f"итоговый документ при generate_document. Просто упомянуть находку "
                f"пользователю в тексте ответа НЕДОСТАТОЧНО."
            )

        from data_search import try_search_statistics, format_results_for_prompt
        try:
            results = await try_search_statistics(query)
        except Exception as exc:
            logger.warning("search_project_data failed: %s: %s", type(exc).__name__, exc)
            return (
                "Поиск данных сейчас не сработал технически — сообщи пользователю "
                "честно и используй XYZ-плейсхолдер вместо выдуманной цифры."
            )
        if not results:
            return (
                f"По запросу '{query}' живых данных в открытом поиске не нашлось (пробовал "
                "и веб-поиск Claude, и резервную цепочку поисковиков). "
                "Не выдумывай цифру — используй правило XYZ-плейсхолдеров и скажи "
                "пользователю прямо, что не нашёл живых данных по этой теме, "
                "предложи прислать свои источники, если есть."
            )
        formatted = format_results_for_prompt(results)
        session.setdefault("_found_data_notes", []).append({
            "query": query,
            "summary": "\n".join(f"• {r['title']}: {r['snippet'][:200]}" for r in results[:4]),
            "sources": [{"title": r["title"], "url": r["url"]} for r in results[:4]],
        })
        return (
            f"Найдено по запросу '{query}' (best-effort веб-поиск, источники "
            f"НЕ верифицированы — прежде чем использовать конкретную цифру, "
            f"явно укажи источник рядом с ней в тексте, и предупреди пользователя, "
            f"что цифру стоит перепроверить перед подачей):\n{formatted}\n\n"
            f"ОБЯЗАТЕЛЬНО вызови update_project В ЭТОМ ЖЕ ХОДУ и сохрани "
            f"конкретные найденные цифры/факты (с источником) в подходящее поле "
            f"(problem_and_idea, если это про масштаб проблемы, или other_notes) "
            f"— иначе они останутся только в этом сообщении чата и не попадут в "
            f"итоговый документ при generate_document. Просто упомянуть находку "
            f"пользователю в тексте ответа НЕДОСТАТОЧНО."
        )

    if name == "find_matching_grants":
        query = str(args.get("query") or "").strip()
        from connect4pro_catalog import search_matching_grants
        grants = await search_matching_grants(query=query, limit=4)
        if not grants:
            return "В каталоге Connect4pro пока нет опубликованных конкурсов по этому запросу."

        lines = [f"Найдено {len(grants)} актуальных конкурсов в канале Connect4pro (@connect4_pro):"]
        quick_opts = []
        for i, g in enumerate(grants, 1):
            t = g.get("title", "Конкурс")
            amt = g.get("amount", "По условиям")
            dl = g.get("deadline", "Уточняется")
            link = g.get("link", "")
            lines.append(f"{i}. {t}\n   💸 Сумма: {amt} | 📅 Дедлайн: {dl}" + (f"\n   🔗 Ссылка: {link}" if link else ""))
            clean_t = t.split("—")[0].split(":")[0].strip()
            if len(clean_t) > 22:
                clean_t = clean_t[:20] + "..."
            quick_opts.append(f"{i}. {clean_t}")

        session["_pending_quick_replies"] = quick_opts[:4]
        lines.append("\nОпиши эти конкурсы пользователю понятным языком, укажи дедлайны и суммы, и предложи выбрать один из них кнопкой (или предложить свой).")
        return "\n".join(lines)

    if name == "generate_document":
        return "__GENERATE_DOCUMENT__"  # сборка и отправка документа обрабатываются в run_agent_turn

    if name == "suggest_quick_replies":
        options = [str(o).strip() for o in (args.get("options") or []) if str(o).strip()]
        # Защита от утечки внутренних технических названий полей профиля
        # организации (например "Заявитель", "Профиль", "Опыт и сильные
        # стороны", "Команда", "Слабые места") в кнопки для пользователя —
        # это структура данных для модели, а не понятный пользователю выбор.
        _internal_field_markers = (
            "заявитель", "профиль организации", "опыт и сильные стороны",
            "слабые места", "команда проекта",
        )
        options = [
            o for o in options
            if not any(marker in o.lower() for marker in _internal_field_markers)
        ]
        session["_pending_quick_replies"] = options[:4]
        return "Кнопки с вариантами ответа будут показаны под твоим сообщением."

    return f"Неизвестный инструмент: {name}"


async def _anthropic_turn(system_prompt: str, messages: list[dict]) -> dict | None:
    """Один вызов Anthropic messages.create с tools. Возвращает
    {"text": str, "tool_calls": [{"id","name","input"}], "raw_content": [...]}
    или None при неисправимой ошибке (вызывающий код переключится на fallback)."""
    try:
        response = await _client.messages.create(
            model=config.LLM_MODEL,
            max_tokens=4000,
            system=system_prompt,
            messages=messages,
            tools=to_anthropic_tools(TOOLS_OPENAI),
        )
    except Exception as exc:
        logger.warning("Anthropic agent turn failed: %s: %s", type(exc).__name__, exc)
        return None

    text_parts = []
    tool_calls = []
    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append({"id": block.id, "name": block.name, "input": block.input})
    return {
        "text": "\n".join(text_parts).strip(),
        "tool_calls": tool_calls,
        "raw_content": [b.model_dump() for b in response.content],
        "stop_reason": response.stop_reason,
    }


async def _chatgpt_turn(system_prompt: str, messages: list[dict]) -> dict | None:
    """Один вызов ChatGPT/OpenAI (нативный tool calling — TOOLS_OPENAI уже в
    нужном формате, конвертация не нужна, в отличие от Anthropic-ветки)."""
    if not _chatgpt_client:
        return None
    try:
        from llm import _openai_chat_completion

        oa_messages = [{"role": "system", "content": system_prompt}] + messages
        resp = await _openai_chat_completion(
            _chatgpt_client,
            model=config.OPENAI_MODEL,
            messages=oa_messages,
            tools=TOOLS_OPENAI,
            max_tokens=4000,
        )
        msg = resp.choices[0].message
        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except Exception:
                    args = {}
                tool_calls.append({"id": tc.id, "name": tc.function.name, "input": args})
        return {
            "text": (msg.content or "").strip(),
            "tool_calls": tool_calls,
            "raw_message": msg,
        }
    except Exception as exc:
        logger.warning("ChatGPT agent turn failed: %s: %s", type(exc).__name__, exc)
        return None


async def _gemini_turn(system_prompt: str, messages: list[dict]) -> dict | None:
    """Один вызов Gemini (OpenAI-совместимый tool calling). messages здесь
    в OpenAI chat-completions формате (роли system/user/assistant/tool).

    РЕАЛЬНЫЙ ИНЦИДЕНТ: эта функция была БЕЗ try/except — единственная из
    четырёх *_turn-функций. Пока Gemini был ПОСЛЕДНИМ в цепочке (до
    сегодняшнего переупорядочивания на Anthropic->ChatGPT->Gemini->DeepSeek),
    это было менее заметно — DeepSeek успевал сработать раньше. После
    переупорядочивания Gemini оказался ПЕРЕД DeepSeek, и когда у Gemini
    кончился собственный месячный лимit трат (RESOURCE_EXHAUSTED, отдельно
    от лимитов Anthropic/OpenAI), необработанное исключение уронило ВЕСЬ ход
    агента, даже не дав DeepSeek — настоящему последнему резерву — попытаться
    ответить. Живой инцидент: все четыре провайдера оказались одновременно
    без доступа/бюджета (Anthropic — лимит до 1 октября, OpenAI — $0
    кредитов, Gemini — исчерпан spend cap), но DeepSeek, скорее всего, ещё
    был жив — просто не получил шанса, потому что цепочка упала раньше."""
    try:
        oa_messages = [{"role": "system", "content": system_prompt}] + messages
        resp = await _fallback_client.chat.completions.create(
            model=config.FALLBACK_LLM_MODEL,
            messages=oa_messages,
            tools=TOOLS_OPENAI,
            max_tokens=4000,
            extra_body={"reasoning_effort": "none"},
        )
        msg = resp.choices[0].message
        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except Exception:
                    args = {}
                tool_calls.append({"id": tc.id, "name": tc.function.name, "input": args})
        return {
            "text": (msg.content or "").strip(),
            "tool_calls": tool_calls,
            "raw_message": msg,
        }
    except Exception as exc:
        logger.warning("Gemini agent turn failed: %s: %s", type(exc).__name__, exc)
        return None


async def _deepseek_turn(system_prompt: str, messages: list[dict]) -> dict | None:
    """Один вызов DeepSeek (OpenAI-совместимый tool calling)."""
    if not _deepseek_client:
        return None
    try:
        oa_messages = [{"role": "system", "content": system_prompt}] + messages
        resp = await _deepseek_client.chat.completions.create(
            model=getattr(config, "DEEPSEEK_MODEL", "deepseek-chat"),
            messages=oa_messages,
            tools=TOOLS_OPENAI,
            max_tokens=4000,
        )
        msg = resp.choices[0].message
        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except Exception:
                    args = {}
                tool_calls.append({"id": tc.id, "name": tc.function.name, "input": args})
        return {
            "text": (msg.content or "").strip(),
            "tool_calls": tool_calls,
            "raw_message": msg,
        }
    except Exception as exc:
        logger.warning("DeepSeek agent turn failed: %s: %s", type(exc).__name__, exc)
        return None


def _anthropic_messages_to_openai(messages: list[dict]) -> list[dict]:
    """Конвертирует накопленную anthropic-style историю (content blocks) в
    плоский OpenAI chat формат — используется только если пришлось
    переключиться на fallback ПОСРЕДИ хода (редкий случай)."""
    result = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            result.append({"role": m["role"], "content": content})
            continue
        # content — список блоков (text/tool_use/tool_result)
        text_bits = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_bits.append(block["text"])
        result.append({"role": m["role"], "content": "\n".join(text_bits)})
    return result


def _extract_options_from_reply(text: str) -> list[str]:
    """Автоматическое извлечение кнопок-подсказок, если модель вывела список
    вариантов (1. ..., 2. ... или маркеры - **...**) в тексте, но забыла вызвать suggest_quick_replies."""
    import re

    # Защита от ложного срабатывания на пересказ/сводку уже известных фактов
    # (например "Важная информация, которую я выяснил из формы: - Фонд GGF
    # поддерживает... - Заявки подаются через..." или "Ключевой опыт: ...,
    # Сильные стороны: ...") — такие сообщения информируют пользователя,
    # а не предлагают ему выбор, и автоизвлечение не должно превращать
    # заголовки утверждений в кнопки.
    _recap_markers = (
        "сохраняю эт", "сохраню эт", "сохраняю это в проект",
        "сохраню это в проект", "записал в проект", "сохранил в проект",
        "важная информация", "что я выяснил", "выяснил из формы",
        "узнал из формы", "из формы следует", "из требований следует",
        "запишу точную структуру", "запишу структуру",
        # РЕАЛЬНЫЙ ИНЦИДЕНТ: "Отлично, реквизиты получил и сохранил. Итак, у
        # нас есть: - **Директор**: Иванов... - **Адрес**: ..." — сводка уже
        # известных фактов из загруженного файла, но правило ниже (п.1,
        # маркированный список с жирным текстом) превратило эти пункты в
        # кнопки "Директор"/"Адрес" — бессмысленные как варианты ответа на
        # РЕАЛЬНЫЙ вопрос сообщения (который был вообще про другое, про
        # уточнение адреса).
        "получил и сохранил", "итак, у нас есть", "у нас есть:",
        "вот что уже известно", "вот что у нас есть",
    )
    if any(marker in text.lower() for marker in _recap_markers):
        return []

    # То же самое для случая, когда сообщение прямо просит прислать НЕСКОЛЬКО
    # фактов ОДНИМ сообщением (см. ЭТАП 2 в agent_roadmap.py, чек-лист базовых
    # данных заявителя) — нумерованный список здесь это ПЕРЕЧЕНЬ того, что
    # нужно продиктовать вместе, а не меню "выберите один вариант". Кнопки
    # вида "1. ФИО и должность..." тут не имеют смысла — тапнуть можно только
    # одну, а нужны все пункты сразу.
    _combined_answer_markers = (
        "одним сообщением", "в одном сообщении", "продиктуйте",
        "двух строк хватит", "и в том же сообщении допишите",
    )
    if any(marker in text.lower() for marker in _combined_answer_markers):
        return []

    # Автоизвлечение кнопок оправдано ТОЛЬКО когда текст реально предлагает
    # пользователю выбор (вопрос или явная просьба выбрать вариант) —
    # иначе рискуем превратить в кнопки заголовки обычных утверждений/фактов.
    # РЕАЛЬНАЯ ЖАЛОБА: "Напишите, пожалуйста, что вам удобнее: - «Предложи
    # варианты сам»... - или кратко своим текстом..." — явное предложение
    # выбора из 2 буллетов, но без "?" и без любого из старых маркеров ниже
    # — гейт отсеивал сообщение до того, как парсинг буллетов вообще
    # запускался, хотя сам список разобрался бы нормально.
    _choice_markers = (
        "?", "выберите", "выбери", "какой вариант", "какой из",
        "что выбрать", "уточните", "подтвердите", "нужно ли",
        "нажмите кнопку", "отправьте цифру", "удобнее", "как вам проще",
    )
    if not any(marker in text.lower() for marker in _choice_markers):
        return []

    # РЕАЛЬНЫЙ ИНЦИДЕНТ: сообщение может содержать блок контента "для
    # копирования" (например уже заполненные поля формы донора, со своей
    # СОБСТВЕННОЙ нумерацией — "16) Является ли кто-либо из сотрудников
    # выборным должностным лицом...") ПЕРЕД настоящим вопросом/призывом к
    # действию в конце ("Напишите коротко: «дальше» — и продолжу"). Пункты 1
    # и 2 ниже раньше сканировали ВЕСЬ текст — нумерация из скопированного
    # контента превращалась в кнопки, никак не связанные с тем, что бот
    # реально спрашивает. По UX-паттерну бота (agent_roadmap.py) настоящее
    # меню выбора всегда стоит СРАЗУ перед "Нажмите кнопку.../отправьте
    # цифру" — поэтому ищем пункты меню только в последних 1-2 абзацах.
    paragraphs = [p for p in text.strip().split("\n\n") if p.strip()]
    tail_text = "\n\n".join(paragraphs[-2:]) if len(paragraphs) > 1 else text

    lines = [line.strip() for line in tail_text.strip().split("\n") if line.strip()]

    def _is_fact_not_option(tail: str) -> bool:
        """"**Директор** (контактное лицо): Иванов..." — то, что идёт после
        жирной подписи, это ЗНАЧЕНИЕ факта (после необязательной скобки —
        двоеточие и сам факт), а не пояснение к варианту выбора. Настоящие
        пункты меню в этом боте оформляются через тире/без разделителя
        (см. USER_ONBOARDING_UX), не через двоеточие сразу после метки."""
        t = re.sub(r"^\([^)]*\)\s*", "", tail.strip())
        return t.startswith(":")

    # 1. Сначала ищем маркированные списки (- / * / •), особенно с жирным текстом или под разделом вариантов
    bullets = []
    for line in lines:
        m_bullet = re.match(r"^[-*•]\s+(.+)$", line)
        if m_bullet:
            raw = m_bullet.group(1).strip()
            bold_m = re.match(r"^\*\*([^*]+)\*\*(.*)$", raw)
            if bold_m:
                if _is_fact_not_option(bold_m.group(2)):
                    continue
                label = bold_m.group(1).strip().rstrip(":-— ")
            else:
                clean = re.sub(r"[*_`]", "", raw).strip().rstrip("?:.")
                clean = re.split(r"\s+[—-]\s+|:\s+", clean)[0]
                words = clean.split()
                label = " ".join(words[:4])
            if len(label) > 24:
                label = label[:22] + "..."
            if label:
                bullets.append(label)

    if 2 <= len(bullets) <= 6:
        return [f"{i+1}. {b}" for i, b in enumerate(bullets[:5])]

    # 2. Ищем нумерованные списки: 1. / 1) / **1.** / **1)** / - 1.
    numbered = []
    for line in lines:
        m = re.match(r"^(?:[-*•]\s*)?(?:\*\*)?(\d+)[.)](?:\*\*)?\s*(.+)$", line)
        if m:
            num = m.group(1)
            raw = m.group(2).strip()
            bold_m = re.match(r"^\*\*([^*]+)\*\*(.*)$", raw)
            if bold_m:
                if _is_fact_not_option(bold_m.group(2)):
                    continue
                label = bold_m.group(1).strip().rstrip(":-— ")
            else:
                clean = re.sub(r"[*_`]", "", raw).strip().rstrip("?:.")
                clean = re.split(r"\s+[—-]\s+|:\s+", clean)[0]
                words = clean.split()
                label = " ".join(words[:4])
            if len(label) > 24:
                label = label[:22] + "..."
            if label:
                numbered.append(f"{num}. {label}")

    if 2 <= len(numbered) <= 5 and numbered[0].startswith("1."):
        return numbered

    # 3. Простой бинарный вопрос без списка вообще — жалоба: "куда пропала
    # способность создавать кнопки?" — модель задаёт ровно тот вопрос,
    # который предписан роадмапом (например ЭТАП 3.5: "есть ли у вас
    # данные — или их лучше поискать мне?"), но свободной прозой вместо
    # нумерованного списка, и забывает вызвать suggest_quick_replies — тогда
    # пп. 1-2 выше ничего не находят. Разбираем последнее предложение с "?"
    # по союзу "или" на два варианта — грубее, чем нормальный список, но
    # кнопка с неидеальной подписью лучше, чем её полное отсутствие.
    idx = text.rfind("?")
    if idx != -1:
        start = max(text.rfind(".", 0, idx), text.rfind("\n", 0, idx), text.rfind("!", 0, idx))
        question = text[start + 1:idx + 1].strip()
        parts = re.split(r"\s+или\s+", question, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2:
            opts = []
            for part in parts:
                clean = re.sub(r"[*_`]", "", part).strip().strip("?,.—- ")
                clean = re.sub(r"^(есть ли|нужно ли|хотите ли|стоит ли)\s+", "", clean, flags=re.IGNORECASE)
                words = clean.split()
                label = " ".join(words[:4])
                if len(label) > 24:
                    label = label[:22] + "..."
                if label:
                    opts.append(label)
            if len(opts) == 2:
                return [f"1. {opts[0]}", f"2. {opts[1]}"]

    return []


def _clean_button_promises(reply: str, has_buttons: bool) -> str:
    """Если кнопок нет, убирает из текста ложные обещания нажать кнопку."""
    import re
    if has_buttons:
        return reply
    reply = re.sub(
        r"👇?\s*(?:выберите|нажмите)\s+(?:кнопк[а-я\s/]+|вариант\s+кнопк[а-я\s/]+)(?:или\s+[а-я\s]+)?",
        "👇 Напишите ответ своими словами:",
        reply,
        flags=re.IGNORECASE,
    )
    return reply


FOLLOWUP_TOOLS = {
    "fetch_donor_page",
    "download_and_extract_donor_files",
    "select_donor_form",
    "generate_document",
    "find_matching_grants",
    "search_project_data",
}

# Сколько XYZ-плейсхолдеров в готовом документе считается "слишком много,
# бюджет/мероприятия по факту не проработаны" (см. блокировку generate_document
# ниже). Единичные XYZ (1-2 неизвестные мелкие детали) — нормальное,
# предусмотренное поведение по PLACEHOLDER_RULE, не блокируются.
XYZ_BLOCK_THRESHOLD = 6


_PROMISE_MARKERS = (
    "сразу разверну", "сейчас разверну", "сейчас пришлю",
    "сразу пришлю", "дальше опишу", "далее опишу",
    "сейчас опишу", "сразу опишу", "сейчас сформулирую",
    "сразу сформулирую", "сейчас составлю", "сразу составлю",
    "сейчас подготовлю", "сразу подготовлю", "сейчас предложу",
    "сразу предложу", "ниже разверну", "ниже опишу",
    "теперь разверну", "теперь опишу", "теперь сформулирую",
)

# Универсальный шаблон "обещаю действие в будущем времени 1-го лица" —
# ловит любые формулировки типа "сохраню и покажу", "давайте сохраню",
# "запишу и разверну" и т.п., даже не входящие в список выше дословно.
# Глагольные основы, характерные для незавершённых обещаний консультанта.
_PROMISE_VERB_STEMS = (
    "сохран", "покаж", "разверн", "опиш", "составл", "подготовл",
    "предлож", "сформулир", "запиш", "продолж",
)


def _is_empty_promise(text: str | None) -> bool:
    """True, если текст — связная фраза-обещание ("сейчас пришлю",
    "сразу разверну концепцию", "давайте сохраню и покажу" и т.п.) без
    реального развёрнутого содержания следом. Модель иногда пишет такую
    фразу и останавливается, вместо того чтобы сразу продолжить полным
    ответом — это создаёт для пользователя ощущение зависшего бота."""
    if not text:
        return False
    _t = text.strip().lower()
    if len(_t) >= 400:
        return False
    if any(marker in _t for marker in _PROMISE_MARKERS):
        return True
    # Короткий текст (нет реального развёрнутого контента) + обещание
    # действия глаголом будущего времени 1-го лица = скорее всего пустышка.
    if len(_t) < 200 and any(stem in _t for stem in _PROMISE_VERB_STEMS):
        return True
    return False


MAX_HISTORY_MESSAGES = 60
MAX_HISTORY_CHARS = 60000  # ~15k токенов с запасом — держит счёт токенов провайдера в разумных пределах


def _message_len(m: dict) -> int:
    content = m.get("content")
    total = len(content) if isinstance(content, str) else 0
    for tc in m.get("tool_calls", []) or []:
        total += len(json.dumps(tc, ensure_ascii=False))
    return total


def _trim_history(history: list[dict]) -> None:
    """Обрезает историю диалога спереди (старые ходы), когда она разрастается
    сверх разумного объёма — БЕЗ потери фактов: build_system_prompt заново
    вставляет ЖИВОЙ снепшот project_data в каждый системный промпт (см.
    build_system_prompt), так что все сохранённые факты видны модели
    независимо от истории. История нужна только для тона/непрерывности
    разговора, поэтому её можно безопасно укорачивать.

    РЕАЛЬНЫЙ РИСК ("бот то читает, то не может читать содержимое чата"):
    history_openai раньше рос без ограничений весь срок сессии — с
    полными текстами загруженных файлов (до 8000 симв. каждый) и
    результатами fetch_donor_page/search_project_data в истории, длинная
    сессия легко перерастала контекст провайдера, и в зависимости от того,
    какой провайдер (DeepSeek/Anthropic/Gemini) в этот момент отвечал —
    поведение "помнит/не помнит" становилось непредсказуемым.

    Режем ТОЛЬКО по границе хода (role == "user"), чтобы никогда не
    разорвать пару tool_calls/tool-result — разрыв такой пары ломает формат
    запроса у обоих провайдеров."""
    if len(history) <= MAX_HISTORY_MESSAGES and sum(_message_len(m) for m in history) <= MAX_HISTORY_CHARS:
        return

    # Всегда оставляем хотя бы последние MAX_HISTORY_MESSAGES сообщений целиком,
    # и режем дополнительно с начала, пока не уложимся в char-бюджет.
    cut = max(0, len(history) - MAX_HISTORY_MESSAGES)
    while cut < len(history) and history[cut].get("role") != "user":
        cut += 1

    remaining_chars = sum(_message_len(m) for m in history[cut:])
    while remaining_chars > MAX_HISTORY_CHARS and cut < len(history) - 1:
        removed = history[cut]
        next_cut = cut + 1
        while next_cut < len(history) and history[next_cut].get("role") != "user":
            next_cut += 1
        remaining_chars -= sum(_message_len(m) for m in history[cut:next_cut])
        cut = next_cut

    if cut > 0:
        logger.info("Обрезаю историю диалога: удаляю %d старых сообщений (осталось %d)", cut, len(history) - cut)
        del history[:cut]


async def run_agent_turn(session: dict, user_text: str) -> AgentTurnResult:
    """Главная точка входа — один ход диалога: добавляет сообщение
    пользователя, крутит цикл модель<->инструменты до финального текстового
    ответа (или до сборки документа), обновляет session на месте."""
    project_data = session.setdefault("project_data", {})
    flow = session.get("flow", "grant")
    ui_language = session.get("ui_language", "ru")
    doc_language = session.get("doc_language")

    system_prompt = build_system_prompt(project_data, flow, ui_language, doc_language)
    # Слабым резервным моделям (Gemini flash, DeepSeek) — короткий промпт с вычисленным
    # кодом шагом: на полном 60-КБ роадмапе они теряли нить и не помнили проект.
    lite_prompt = build_lite_system_prompt(project_data, flow, ui_language, doc_language)

    history = session.setdefault("history_openai", [])  # плоский OpenAI-формат, провайдеро-независимый
    # Точка отката: если ни один провайдер не ответил, реплика пользователя и
    # всё дописанное в этом ходу удаляются из истории — иначе при повторе то же
    # сообщение окажется в истории дважды, а модель увидит "ответ без вопроса"
    # (реальная причина путаницы шагов при отказе провайдеров).
    history_rollback_len = len(history)
    history.append({"role": "user", "content": user_text})
    _trim_history(history)

    tool_log: list[str] = []
    document_ready = False
    document_text = ""
    generated_documents: list[dict] = []
    empty_reply_retries = 0

    for round_i in range(MAX_TOOL_ROUNDS):
        # РЕАЛЬНЫЙ ИНЦИДЕНТ: DeepSeek был поставлен основным провайдером ещё
        # тогда, когда на аккаунте Anthropic был исчерпан лимит расходов —
        # разумно в тот момент, но с тех пор почти ВСЕ баги поведения бота за
        # неделю (переспрашивает факты, не те кнопки, обрывается без
        # продолжения, путает шаги) обнаруживались именно на DeepSeek — он не
        # так надёжно следует детальным инструкциям роадмапа, как Claude.
        # Порядок фолбэка (по явному запросу пользователя, 2026-09-18):
        # Anthropic -> ChatGPT -> Gemini -> DeepSeek последним — тот же
        # порядок, что и в llm.call_claude/call_fallback_llm.
        turn = None
        if config.ANTHROPIC_API_KEY:
            anthropic_messages = _openai_history_to_anthropic(history)
            turn = await _anthropic_turn(system_prompt, anthropic_messages)
        if turn is None and _chatgpt_client:
            turn = await _chatgpt_turn(system_prompt, history)
        if turn is None and _fallback_client:
            turn = await _gemini_turn(lite_prompt, history)
        if turn is None and _deepseek_client:
            turn = await _deepseek_turn(lite_prompt, history)
        if turn is None:
            del history[min(history_rollback_len, len(history)):]
            return AgentTurnResult(
                reply=(
                    "⚠️ Сейчас у бота нет доступа ни к одной ИИ-модели (исчерпан лимит или баланс "
                    "у провайдеров). Это не ошибка в вашем сообщении — оно НЕ потеряно: просто "
                    "отправьте его ещё раз, когда доступ восстановят. Владельцу бота уже отправлено "
                    "уведомление."
                ),
                llm_unavailable=True,
            )

        if not turn["tool_calls"] and not (turn["text"] or "").strip():
            # Пустой ответ модели раньше подменялся заглушкой "Понял, продолжаем." —
            # бот делал вид, что шаг пройден, и шаги диалога расходились с
            # реальным состоянием. Теперь: один повтор запроса, затем честная ошибка.
            if empty_reply_retries < 1:
                empty_reply_retries += 1
                logger.warning("Empty model reply (no text, no tool calls) — retrying once")
                continue
            del history[min(history_rollback_len, len(history)):]
            return AgentTurnResult(
                reply="⚠️ Модель вернула пустой ответ. Повторите, пожалуйста, ваше сообщение ещё раз.",
                llm_unavailable=True,
            )

        if not turn["tool_calls"]:
            reply = turn["text"]
            quick_replies = session.pop("_pending_quick_replies", [])
            file_attachments = session.pop("_pending_file_attachments", [])

            # Защита от пустых заглушек "Жду ваш ответ 👇"
            if reply.strip() in {"Жду ваш ответ 👇", "Жду ваш ответ", "Жду ответ 👇", "Жду ответ", "Жду ваш выбор 👇", "Жду выбор 👇"}:
                for m in reversed(history[:-1]):
                    if m.get("role") == "assistant" and m.get("content") and isinstance(m["content"], str):
                        c = m["content"].strip()
                        if len(c) > 20 and not any(c.startswith(w) for w in ["Жду ваш ответ", "Жду ответ", "Жду выбор"]):
                            reply = c
                            break

            # Защита от "пустых обещалок" без вызова инструментов: модель
            # написала фразу вида "сейчас пришлю", "сразу разверну концепцию",
            # но реального содержания не дала. Не отдаём такой ответ
            # пользователю — просим модель сразу же продолжить в этом ходу.
            if _is_empty_promise(reply) and round_i < MAX_TOOL_ROUNDS - 1:
                history.append({"role": "assistant", "content": reply})
                history.append({
                    "role": "user",
                    "content": "Продолжай прямо сейчас — заверши то, что обещал(а) выше, полным содержанием, без новых обещаний.",
                })
                continue

            # Если модель не вызвала suggest_quick_replies, но перечислила варианты 1, 2, 3... или маркеры в тексте —
            # автоматически создаём кнопки и добавляем понятную новичкам подсказку
            if not quick_replies and reply:
                auto_opts = _extract_options_from_reply(reply)
                if auto_opts:
                    quick_replies = auto_opts
                    if "кнопк" not in reply.lower() and "цифр" not in reply.lower():
                        reply = reply + "\n\n👇 Нажмите кнопку ниже или отправьте цифру в ответ:"
                else:
                    reply = _clean_button_promises(reply, has_buttons=False)

            history.append({"role": "assistant", "content": reply})
            return AgentTurnResult(
                reply=reply,
                document_ready=document_ready,
                document_text=document_text,
                tool_log=tool_log,
                quick_replies=quick_replies,
                file_attachments=file_attachments,
                generated_documents=generated_documents,
            )

        # Модель вызвала инструмент(ы) — фиксируем вызов в истории
        assistant_tool_call_msg = {
            "role": "assistant",
            "content": turn["text"] or None,
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": json.dumps(tc["input"], ensure_ascii=False)},
                }
                for tc in turn["tool_calls"]
            ],
        }
        history.append(assistant_tool_call_msg)

        needs_followup = False
        for tc in turn["tool_calls"]:
            tool_log.append(f"{tc['name']}({json.dumps(tc['input'], ensure_ascii=False)[:200]})")
            if tc["name"] in FOLLOWUP_TOOLS:
                needs_followup = True

            result_str = await _execute_tool(tc["name"], tc["input"], session)
            if result_str == "__GENERATE_DOCUMENT__":
                # РЕАЛЬНЫЙ ИНЦИДЕНТ ("он всё равно составил в своей форме...
                # ты его ухудшил"): donor_template (текстовая СТРУКТУРА формы)
                # живёт внутри project_data и может пережить множество
                # "Продолжить проект" циклов, случившихся ДО того, как
                # появился фикс, сохраняющий сам ФАЙЛ формы (chosen_donor_form/
                # saved_donor_files) через такие же резюме. Если тот файл был
                # потерян ОДИН раз в прошлом (даже задолго до сегодняшних
                # фиксов) — текущий фикс корректно продолжает нести вперёд
                # "файла нет", потому что его на самом деле уже нет в сессии.
                # Раньше это обнаруживалось только ПОСЛЕ полной генерации
                # (~60-160 секунд) — молчаливым предупреждением уже под
                # готовым документом, которое легко пропустить. Проверяем
                # ДО генерации: если структура формы известна, а самого файла
                # нет — не тратим время на генерацию заведомо не по форме,
                # сразу просим прислать файл заново.
                has_template_text = bool(project_data.get("donor_template"))
                chosen_fn = session.get("chosen_donor_form")
                saved_files = session.get("saved_donor_files", [])
                has_file_ref = bool(chosen_fn) and any(
                    f.get("filename") == chosen_fn for f in saved_files
                )
                # РЕАЛЬНЫЙ ИНЦИДЕНТ (тот же день, живой тест): пользователь
                # присылает .docx ПРЯМЫМ ВЛОЖЕНИЕМ (agent_router.receive_document),
                # модель читает текст, зовёт update_project(donor_template=...)
                # вместо select_donor_form — donor_template заполнен, но
                # chosen_donor_form остаётся пустым, хотя файл СЕЙЧАС лежит в
                # saved_donor_files. Промпт это лечит (см. agent_roadmap.py), но
                # промпт-инструкции и раньше оказывались ненадёжными на практике
                # (см. коммит про проактивные базовые факты) — если это тот самый
                # частный, однозначный случай (СТРУКТУРА только что извлечена и в
                # сессии есть РОВНО ОДИН сохранённый файл формы), незачем гонять
                # пользователя за уже присланным файлом — просто линкуем его как
                # выбранный, а не блокируем генерацию.
                if has_template_text and not has_file_ref and len(saved_files) == 1:
                    session["chosen_donor_form"] = saved_files[0].get("filename")
                    session["chosen_donor_form_path"] = saved_files[0].get("path")
                    chosen_fn = session["chosen_donor_form"]
                    has_file_ref = bool(chosen_fn)
                if has_template_text and not has_file_ref and not session.get("_donor_file_missing_warned"):
                    session["_donor_file_missing_warned"] = True
                    result_str = (
                        "СТОП — НЕ вызывай генерацию сейчас. Структура формы донора известна "
                        "(donor_template сохранён), но самого ФАЙЛА формы в этой сессии больше "
                        "нет (например, после давнего перезапуска или продолжения проекта, ещё "
                        "до того, как это чинили). Если сгенерировать документ сейчас — он выйдет "
                        "НЕ по официальному файлу донора, а просто с похожими вопросами в "
                        "свободном тексте, донор такое не примет. Прямо сейчас честно объясни это "
                        "пользователю и попроси прислать файл формы донора ЕЩЁ РАЗ (тот же файл, "
                        "что раньше) — как только он придёт, сохранится заново, и тогда вызови "
                        "generate_document снова, уже по официальному файлу."
                    )
                else:
                    from agent_docgen import build_final_document
                    document_text = await build_final_document(session)

                    # РЕАЛЬНЫЙ ИНЦИДЕНТ ("заявка не по форме, данные не нашёл,
                    # везде XYZ — такая заявка не нужна вообще"): раньше документ
                    # ВСЕГДА отправлялся, сколько бы XYZ в нём ни было — правило
                    # плейсхолдеров рассчитано на единичные недостающие детали, а
                    # не на пустой бюджет/мероприятия. Один раз за сессию, если
                    # плейсхолдеров подозрительно много (бюджет и/или действия по
                    # факту не проработаны), НЕ отправляем такой документ —
                    # заставляем модель сначала узнать сумму гранта и данные (см.
                    # ВЫБОР КОНКРЕТНОЙ СУММЫ / ОБРАТНЫЙ ХОД «3 ДЕРЕВЬЕВ» в
                    # agent_roadmap.py). Только один блок за сессию — иначе рискуем
                    # вернуть старую жалобу "бот отказывается генерировать вообще",
                    # если у донора реально нет данных для части полей.
                    xyz_count = document_text.count("XYZ")
                    if xyz_count > XYZ_BLOCK_THRESHOLD and not session.get("_xyz_block_used"):
                        session["_xyz_block_used"] = True
                        result_str = (
                            f"СТОП — НЕ отправляй этот документ пользователю: в черновике {xyz_count} "
                            "плейсхолдеров XYZ, это значит бюджет и/или мероприятия по факту не "
                            "проработаны, такой документ бесполезен для подачи. Вместо того чтобы "
                            "показать его пользователю, сделай сейчас, в этом же ответе: "
                            "1) если известен диапазон суммы гранта у донора — прямо покажи его "
                            "пользователю и спроси, какую сумму он закладывает (suggest_quick_replies "
                            "с 3-4 суммами из диапазона); 2) если ещё не пытался найти реальные данные "
                            "по масштабу проблемы — вызови search_project_data сейчас, а не отправляй "
                            "документ с пустыми полями. Как только сумма выбрана (или ты взял разумную "
                            "дефолтную) — посчитай бюджетную разбивку и примерные мероприятия ИМЕННО "
                            "под неё (ОБРАТНЫЙ ХОД «3 ДЕРЕВЬЕВ»), покажи это пользователю текстом, и "
                            "только после этого — если он подтвердит или сам попросит документ ещё раз — "
                            "вызови generate_document заново."
                        )
                    else:
                        document_ready = True
                        # РЕАЛЬНЫЙ ИНЦИДЕНТ (живой тест NED, 23-24.09.2026): когда
                        # донор требует НЕСКОЛЬКО документов, этот блок раньше
                        # только запоминал document_text/document_ready — сам файл
                        # материализовался ОДИН раз, В КОНЦЕ ВСЕГО ХОДА (agent_router
                        # ._send_docx -> export_docx), а export_docx читает session
                        # ["_prebuilt_pdf_path"/"_prebuilt_xlsx_path"/"_prebuilt_docx_
                        # _path"] — скалярные поля, которые каждый следующий
                        # generate_document в этом же ходу молча ПЕРЕЗАПИСЫВАЛ. Итог:
                        # из пакета в 3-4 документов реально уходил пользователю
                        # только ПОСЛЕДНИЙ (или первый PDF — export_docx проверяет
                        # pdf/xlsx/docx в этом порядке), хотя каждый предыдущий был
                        # честно заполнен и отмечен "filled" в реестре — само же
                        # тело этого result_str заявляло модели "собран и отправлен
                        # пользователю файлом", хотя физически файл ещё не уходил.
                        # Фикс: материализуем файл ЭТОГО документа СЕЙЧАС же (пока
                        # его prebuilt-путь не затёрт следующим вызовом), а не
                        # откладываем на конец хода — export_docx именно так и
                        # рассчитан (pop, не read), просто раньше вызывался разом.
                        from agent_docgen import export_docx
                        try:
                            doc_path, official_template = await export_docx(document_text, session)
                        except Exception:
                            logger.exception("Failed to materialize document for %r mid-turn", chosen_fn)
                        else:
                            # "Собеседник-эксперт, который смотрит на доки глазами
                            # донора" (просьба владельца) — отдельный вызов БЕЗ
                            # истории разговора (llm.donor_perspective_review), см.
                            # её докстринг. Только для документов с реальным
                            # содержательным текстом — короткая PDF-форма из чистых
                            # kv-полей (имя/сумма/дата) не даёт рецензенту ничего
                            # содержательного проверять, а лишний вызов на неё —
                            # чистая трата времени и денег. Отдельный try/except:
                            # сбой обзора не должен топить уже готовый документ —
                            # donor_perspective_review и сама ловит свои ошибки
                            # (возвращает ""), но не полагаемся на это здесь.
                            donor_review = ""
                            if len(document_text) > 500:
                                try:
                                    from llm import donor_perspective_review
                                    donor_review = await donor_perspective_review(
                                        document_text, project_data.get("donor_info", ""),
                                    )
                                except Exception:
                                    logger.warning("donor_perspective_review call failed", exc_info=True)
                            generated_documents.append({
                                "path": doc_path,
                                "filename": chosen_fn,
                                "official_template": official_template,
                                "non_latin_warning": session.pop("_pdf_non_latin_warning", False),
                                "donor_review": donor_review,
                            })
                        # Реестр документов донора (см. _classify_donor_documents) —
                        # отмечаем именно ЭТОТ файл готовым, кодом, а не памятью
                        # модели, и сразу же явно говорим модели, сколько ещё
                        # осталось — иначе слабая модель (сейчас единственная
                        # реально доступная) может "забыть" и остановиться,
                        # решив, что раз ЭТОТ документ готов, то и всё готово.
                        docs = project_data.get("donor_documents") or []
                        has_xyz = "XYZ" in document_text
                        for d in docs:
                            if d.get("filename") == chosen_fn:
                                d["status"] = "filled_with_gaps" if has_xyz else "filled"
                                break
                        remaining = [d for d in docs if d.get("status") == "pending"]
                        if remaining:
                            names = ", ".join(f"«{d['filename']}»" for d in remaining)
                            result_str = (
                                f"Документ '{chosen_fn}' собран и отправлен пользователю файлом. "
                                f"У донора ЕЩЁ {len(remaining)} несобранных документ(ов): {names}. "
                                f"НЕ говори пользователю, что заявка готова. В этом же ответе кратко "
                                f"подтверди, что этот файл готов, и СРАЗУ переходи к следующему: вызови "
                                f"select_donor_form на первом из оставшихся, затем generate_document."
                            )
                        else:
                            gaps = [d["filename"] for d in docs if d.get("status") == "filled_with_gaps"]
                            # РЕАЛЬНАЯ ЖАЛОБА: раньше эта подсказка звучала как "перечисли...
                            # что нужно вписать" — слабая модель превращала это в допрос по
                            # одному полю за раз ("напишите первым сообщением 2-3 поля из
                            # Organization Profile..."), хотя документ уже отправлен и
                            # пользователь ничего не просил уточнять. Формулировка теперь
                            # прямо запрещает интерактивный сбор данных в этом же ходу.
                            gap_note = (
                                f" В файлах есть незаполненные места (XYZ) — ОДНИМ коротким "
                                f"списком укажи, в каких документах ({', '.join(gaps)}) и что "
                                f"именно осталось пустым, как FYI-напоминание 'проверьте и "
                                f"дозаполните сами перед подачей'. НЕ проси прислать эти данные "
                                f"сейчас, не спрашивай по одному пункту и не жди ответа — просто "
                                f"сообщи и на этом закончи ход. Если пользователь сам пришлёт "
                                f"недостающее позже — тогда update_project и пересобери документ."
                            ) if gaps else ""
                            result_str = (
                                "Документ собран и отправлен пользователю файлом. Это ПОСЛЕДНИЙ "
                                "недостающий документ донора — весь пакет теперь собран." + gap_note +
                                " Не пересказывай содержимое текстом, просто кратко подтверди готовность "
                                "всего пакета."
                            )

            history.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result_str,
            })

        # ЗАЩИТА ОТ ГАЛЛЮЦИНАЦИЙ: если модель НЕ вызвала generate_document,
        # но в своём тексте утверждает, что документ «готов», «собран», «отправлен» —
        # это ложное обещание. Принудительно считаем ход незаконченным, чтобы
        # модель за следующим витком вызвала инструмент, и не отдаём такой текст пользователю.
        hallucination_detected = False
        if not document_ready:
            text_lower = (turn["text"] or "").lower()
            false_ready_markers = (
                "документ готов", "документ собран", "документ сформирован",
                "файл готов", "файл собран", "word-файл готов",
                "отправил документ", "отправлен файл", "прикреплен файл",
                "готов к отправке", "собрался документ", "сгенерировал документ",
                "заполнил шаблон", "заполнил форму", "документ по шаблону",
            )
            if any(marker in text_lower for marker in false_ready_markers):
                hallucination_detected = True
                is_incomplete = True
                # Перезаписываем ответ модели на честное продолжение
                turn["text"] = "Собираю финальный документ..."

        # Если вызовы были чисто локальными (сохранение данных, прикрепление кнопок, отправка файлов)
        # И модель УЖЕ вернула содержательный ответ в этом ходу — НЕ делаем лишний запрос к LLM,
        # отдаём ответ пользователю сразу, сохраняя полный текст описания и кнопок.
        # НО если текст явно оборван на ':' или '...', или слишком короткий — продолжаем цикл,
        # чтобы дать модели возможность закончить формулировку вопроса!
        # НО если была галлюцинация — НЕ сбрасываем is_incomplete
        if not hallucination_detected:
            is_incomplete = bool(
                turn["text"] and (
                    turn["text"].strip().endswith((":", "...", "—", "-"))
                    or len(turn["text"].strip()) < 30
                )
            )

        # Защита от "пустых обещалок" в ходе с вызовом инструментов —
        # см. _is_empty_promise ниже.
        if not is_incomplete and _is_empty_promise(turn["text"]):
            is_incomplete = True

        if not needs_followup and turn["text"] and not is_incomplete:
            reply = turn["text"]
            quick_replies = session.pop("_pending_quick_replies", [])
            file_attachments = session.pop("_pending_file_attachments", [])

            if not quick_replies and reply:
                auto_opts = _extract_options_from_reply(reply)
                if auto_opts:
                    quick_replies = auto_opts
                    if "кнопк" not in reply.lower() and "цифр" not in reply.lower():
                        reply = reply + "\n\n👇 Нажмите кнопку ниже или отправьте цифру в ответ:"
                else:
                    reply = _clean_button_promises(reply, has_buttons=False)

            return AgentTurnResult(
                reply=reply,
                document_ready=document_ready,
                document_text=document_text,
                tool_log=tool_log,
                quick_replies=quick_replies,
                file_attachments=file_attachments,
                generated_documents=generated_documents,
            )

    # Слишком много раундов инструментов подряд — защита от зацикливания,
    # не должно случаться в норме, но лучше явный ответ, чем зависание.
    logger.warning("run_agent_turn: hit MAX_TOOL_ROUNDS (%d) without final text", MAX_TOOL_ROUNDS)
    fallback_reply = "Собрал часть информации, но не смог сформулировать ответ с первой попытки — повтори сообщение, пожалуйста."
    history.append({"role": "assistant", "content": fallback_reply})
    session.pop("_pending_quick_replies", None)
    file_attachments = session.pop("_pending_file_attachments", [])
    return AgentTurnResult(
        reply=fallback_reply,
        document_ready=document_ready,
        document_text=document_text,
        tool_log=tool_log,
        file_attachments=file_attachments,
        generated_documents=generated_documents,
    )


def _openai_history_to_anthropic(history: list[dict]) -> list[dict]:
    """Конвертирует плоскую OpenAI-style историю (роли user/assistant/tool,
    tool_calls как отдельное поле) в Anthropic content-blocks формат.
    Нужно, потому что канонический формат истории в session — OpenAI-style
    (проще для fallback-провайдера), а Anthropic ожидает content blocks для
    tool_use/tool_result."""
    raw_msgs = []
    for m in history:
        role = m["role"]
        if role == "user":
            c = m["content"]
            blocks = [{"type": "text", "text": c}] if isinstance(c, str) else c
            raw_msgs.append({"role": "user", "content": blocks})
        elif role == "assistant":
            if m.get("tool_calls"):
                blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                for tc in m["tool_calls"]:
                    args = tc["function"]["arguments"]
                    try:
                        args = json.loads(args) if isinstance(args, str) else args
                    except Exception:
                        args = {}
                    blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["function"]["name"], "input": args})
                raw_msgs.append({"role": "assistant", "content": blocks})
            else:
                c = m["content"]
                blocks = [{"type": "text", "text": c}] if isinstance(c, str) else c
                raw_msgs.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            # Anthropic требует tool_result блоки внутри USER-сообщения,
            # сразу после assistant-сообщения с соответствующим tool_use.
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            if raw_msgs and raw_msgs[-1]["role"] == "user":
                raw_msgs[-1]["content"].append(block)
            else:
                raw_msgs.append({"role": "user", "content": [block]})

    # Слияние подряд идущих сообщений с одинаковой ролью (требование Anthropic: чередование user/assistant)
    merged = []
    for msg in raw_msgs:
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["content"] = merged[-1]["content"] + msg["content"]
        else:
            merged.append(msg)
    return merged
