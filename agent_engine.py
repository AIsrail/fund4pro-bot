"""Агентный движок (v3 редизайн) — заменяет цепочку изолированных FSM-шагов
одним персистентным диалогом с моделью, которая сама решает, когда что
спросить/сохранить/сгенерировать, вызывая инструменты (agent_tools.py) по
собственному усмотрению вместо того, чтобы код диктовал следующий шаг.

Поддерживает оба провайдера сквозным образом (Anthropic Tool Use и OpenAI-
совместимый tool calling для Gemini-fallback) — пробует Anthropic первым,
при любой ошибке (в т.ч. текущий исчерпанный баланс) прозрачно переключается
на Gemini, тем же путём, что и llm.call_claude.
"""

import base64
import json
import logging

import config
from agent_roadmap import build_system_prompt
from agent_tools import TOOLS_OPENAI, to_anthropic_tools
from llm import _client, _deepseek_client, _fallback_client

logger = logging.getLogger("fund4pro.agent_engine")

MAX_TOOL_ROUNDS = 5  # защита от зацикливания вызовов инструментов за один ход
PROJECT_DATA_FIELDS = (
    "org_info", "donor_info", "donor_template", "problem_and_idea",
    "goal_and_objectives", "activities_and_budget", "other_notes",
)


class AgentTurnResult:
    def __init__(
        self,
        reply: str,
        document_ready: bool = False,
        document_text: str = "",
        tool_log: list[str] | None = None,
        quick_replies: list[str] | None = None,
        file_attachments: list[dict] | None = None,
    ):
        self.reply = reply
        self.document_ready = document_ready
        self.document_text = document_text
        self.tool_log = tool_log or []
        self.quick_replies = quick_replies or []
        self.file_attachments = file_attachments or []


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
                path = save_donor_form(content, f["filename"])
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
        return (
            f"Страница загружена. Найдено {len(readable)} документов, они скачаны и отправлены пользователю файлами в чат:\n{listing}\n\n"
            f"Кнопки выбора уже прикреплены. Спроси пользователя, какую из них заполняем как основную заявку "
            f"(или сразу вызови select_donor_form, если выбор уже понятен из контекста)."
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
                f"По запросу '{query}' живых данных в открытом поиске не нашлось. "
                "Не выдумывай цифру — используй правило XYZ-плейсхолдеров и скажи "
                "пользователю прямо, что не нашёл живых данных по этой теме, "
                "предложи прислать свои источники, если есть."
            )
        formatted = format_results_for_prompt(results)
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


async def _gemini_turn(system_prompt: str, messages: list[dict]) -> dict:
    """Один вызов Gemini (OpenAI-совместимый tool calling). messages здесь
    в OpenAI chat-completions формате (роли system/user/assistant/tool)."""
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
    )
    if any(marker in text.lower() for marker in _recap_markers):
        return []

    # Автоизвлечение кнопок оправдано ТОЛЬКО когда текст реально предлагает
    # пользователю выбор (вопрос или явная просьба выбрать вариант) —
    # иначе рискуем превратить в кнопки заголовки обычных утверждений/фактов.
    _choice_markers = (
        "?", "выберите", "выбери", "какой вариант", "какой из",
        "что выбрать", "уточните", "подтвердите", "нужно ли",
        "нажмите кнопку", "отправьте цифру",
    )
    if not any(marker in text.lower() for marker in _choice_markers):
        return []

    lines = [line.strip() for line in text.strip().split("\n") if line.strip()]

    # 1. Сначала ищем маркированные списки (- / * / •), особенно с жирным текстом или под разделом вариантов
    bullets = []
    for line in lines:
        m_bullet = re.match(r"^[-*•]\s+(.+)$", line)
        if m_bullet:
            raw = m_bullet.group(1).strip()
            bold_m = re.match(r"^\*\*([^*]+)\*\*(.*)$", raw)
            if bold_m:
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

    history = session.setdefault("history_openai", [])  # плоский OpenAI-формат, провайдеро-независимый
    history.append({"role": "user", "content": user_text})
    _trim_history(history)

    tool_log: list[str] = []
    document_ready = False
    document_text = ""

    for round_i in range(MAX_TOOL_ROUNDS):
        # Основной провайдер — DeepSeek (быстрый, стабильный и с активным балансом).
        # Если DeepSeek недоступен — пробуем Anthropic, затем Gemini.
        turn = None
        if _deepseek_client:
            turn = await _deepseek_turn(system_prompt, history)
        if turn is None and config.ANTHROPIC_API_KEY:
            anthropic_messages = _openai_history_to_anthropic(history)
            turn = await _anthropic_turn(system_prompt, anthropic_messages)
        if turn is None and _fallback_client:
            turn = await _gemini_turn(system_prompt, history)
        if turn is None:
            return AgentTurnResult(reply="⚠️ Ни один провайдер модели недоступен сейчас. Попробуй через минуту.")

        if not turn["tool_calls"]:
            reply = turn["text"] or "Понял, продолжаем."
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
                from agent_docgen import build_final_document
                document_text = await build_final_document(session)
                document_ready = True
                result_str = "Документ собран и отправлен пользователю файлом — не пересказывай его содержимое текстом, просто кратко подтверди, что документ готов, и что дальше делать (проверить перед отправкой, можно попросить правки)."

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
