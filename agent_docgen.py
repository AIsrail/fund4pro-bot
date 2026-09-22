"""Сборка финального документа для агентной архитектуры — переиспользует
существующую логику генерации/red-flags/docx-экспорта из llm.py и docgen.py
(эти модули менять не пришлось, они уже провайдеро-независимые благодаря
fallback в llm.call_claude)."""

import logging

logger = logging.getLogger("fund4pro.agent_docgen")


def _resolve_donor_doc_path(session: dict, tmp_dir: str) -> tuple[str | None, dict | None]:
    """Находит путь к РЕАЛЬНОМУ файлу формы донора, выбранному в этой сессии,
    восстанавливая его из content_b64 (Redis), если локальная копия на диске
    пропала — например, после рестарта контейнера Render (стирает диск, но
    не Redis) или после "Продолжить проект". Общая логика для обоих путей
    заполнения (нового и старого fallback), вынесена сюда, чтобы не
    дублироваться. Возвращает (путь_или_None, donor_entry_или_None)."""
    import base64
    import os

    donor_doc_path = None
    donor_entry = None
    chosen_fn = session.get("chosen_donor_form")
    if chosen_fn:
        for f in session.get("saved_donor_files", []):
            if f.get("filename") == chosen_fn:
                donor_entry = f
                donor_doc_path = f.get("path")
                break

    if not donor_doc_path:
        donor_doc_path = session.get("chosen_donor_form_path") or session.get("donor_template_file_path")

    if (not donor_doc_path or not os.path.exists(donor_doc_path)) and donor_entry and donor_entry.get("content_b64"):
        try:
            raw = base64.b64decode(donor_entry["content_b64"])
            restored_path = os.path.join(tmp_dir, donor_entry.get("filename") or "donor_template.docx")
            with open(restored_path, "wb") as fh:
                fh.write(raw)
            donor_doc_path = restored_path
            logger.info("Restored donor template from session content_b64 (local cache was gone)")
        except Exception:
            logger.warning("Failed to restore donor template from session content_b64", exc_info=True)
            donor_doc_path = None

    if donor_doc_path and not os.path.exists(donor_doc_path):
        donor_doc_path = None

    return donor_doc_path, donor_entry


async def _try_schema_fill(session: dict) -> str | None:
    """НОВЫЙ основной путь (см. docx_schema_fill.py): если есть реальный
    файл формы донора, читает его структуру напрямую и заполняет
    структурированным батч-вызовом по стабильному field_id — без генерации
    отдельного markdown-текста и без нечёткого сопоставления. Возвращает
    путь к готовому .docx при успехе, кладя итоговый текст (для подсчёта
    XYZ в agent_engine.py) в session["final_document_text"] и сам путь в
    session["_prebuilt_docx_path"] (откуда его забирает export_docx, не
    перезаполняя повторно). При любой неудаче возвращает None — вызывающий
    код (build_final_document) молча падает на старый путь."""
    import os
    import tempfile

    project_data = session.get("project_data", {})
    if not project_data.get("donor_template"):
        # РЕАЛЬНЫЙ ИНЦИДЕНТ: этот и следующий ранний выход раньше не логировались
        # вообще — когда v2 молча падал на legacy fuzzy-matching пайплайн (тот
        # самый источник бага "ответ попал не в ту ячейку", ради которого весь
        # v2 и писался), в логах не оставалось НИ СЛЕДА причины, и диагностика
        # требовала гадать. Теперь причина конкретного отказа видна в логе.
        logger.warning("_try_schema_fill: no donor_template in project_data — falling back to legacy pipeline")
        return None  # структура формы вообще не известна — нечего читать по схеме

    tmp_dir = tempfile.mkdtemp()
    donor_doc_path, _ = _resolve_donor_doc_path(session, tmp_dir)
    if not donor_doc_path:
        logger.warning(
            "_try_schema_fill: donor form file unavailable (chosen_donor_form=%r, "
            "saved_donor_files=%d) — falling back to legacy pipeline",
            session.get("chosen_donor_form"), len(session.get("saved_donor_files") or []),
        )
        return None

    try:
        from docx_schema_fill import fill_donor_docx_template_v2
        path = os.path.join(tmp_dir, "final_version.docx")
        success, text_for_check = await fill_donor_docx_template_v2(donor_doc_path, path, session)
    except Exception:
        logger.warning("_try_schema_fill: unexpected error, falling back to legacy pipeline", exc_info=True)
        return None

    if not success:
        logger.warning("_try_schema_fill: schema-based fill did not succeed, falling back to legacy pipeline")
        return None
    logger.info("_try_schema_fill: schema-based fill succeeded — used for %s", donor_doc_path)

    session["final_document_text"] = text_for_check
    session["_prebuilt_docx_path"] = path
    return path


async def build_final_document(session: dict) -> str:
    """Генерирует текст финального документа из session['project_data'],
    прогоняет через red-flags аудит с одной попыткой автоисправления (тот
    же цикл, что был в handlers/final_version.py, но БЕЗ бесконечных
    повторов — см. комментарий в _run_red_flags_gate ниже).

    АРХИТЕКТУРНЫЙ АУДИТ (после серии багов за неделю: заявка не по форме,
    дублирующиеся ответы, потерянные разделы): если в сессии есть РЕАЛЬНЫЙ
    файл формы донора, пробуем сначала НОВЫЙ путь — docx_schema_fill.py
    читает структуру шаблона напрямую и заполняет по стабильному field_id
    структурированным батч-вызовом, без нечёткого сопоставления текста.
    Это быстрее (не нужен отдельный проход генерации полного markdown-текста
    заявки) и надёжнее (нет угадывания, куда что вписать). Старый путь
    (generate_final_document + fuzzy-matching в docx_template_fill.py)
    остаётся ниже как fallback — если реального файла нет, схема не
    извлеклась, или структурированный батч не смог ничего заполнить."""
    donor_docx_path = await _try_schema_fill(session)
    if donor_docx_path:
        return session["final_document_text"]

    from llm import generate_final_document, LLMEmptyResponseError

    project_data = session.get("project_data", {})
    # generate_final_document (llm.py) ожидает session_data в старом формате
    # ключей (_session_summary читает org_info/donor_info/donor_forms_text/
    # selected_idea/goal_and_objectives/action_trees/concept_text/
    # budget_text) — часть уже совпадает, остальное мапим из новых полей.
    legacy_session = {
        "org_info": project_data.get("org_info", ""),
        "donor_info": project_data.get("donor_info", ""),
        "donor_template": project_data.get("donor_template", ""),
        "selected_idea": project_data.get("problem_and_idea", ""),
        "goal_and_objectives": project_data.get("goal_and_objectives", ""),
        "concept_text": project_data.get("activities_and_budget", ""),
        "budget_text": project_data.get("activities_and_budget", ""),
        "ui_language": session.get("ui_language", "ru"),
        "doc_language": session.get("doc_language"),
    }

    try:
        text = await generate_final_document(legacy_session)
    except LLMEmptyResponseError as e:
        logger.warning("build_final_document failed: %s", e)
        return f"⚠️ Не удалось собрать документ: {e}"

    text = await _run_red_flags_gate(text, legacy_session)
    session["final_document_text"] = text
    return text


async def _run_red_flags_gate(text: str, session_data: dict) -> str:
    """Одна попытка автоисправления red-flags — БЕЗ показа пользователю
    отдельного экрана с кнопками "исправь сам"/"пришли правки" (тот
    механизм зацикливался в старой FSM-версии). В агентной архитектуре
    документ отправляется сразу, а найденные (неисправленные) проблемы
    агент просто упоминает в следующей реплике — пользователь решает
    текстом, что делать, без отдельного жёсткого экрана.

    РЕАЛЬНЫЙ ИНЦИДЕНТ: autofix_red_flags иногда возвращает СПИСОК
    РЕКОМЕНДАЦИЙ ('Для раздела Бюджет: укажите конкретные суммы вместо
    XYZ...') вместо переписанного документа — тот же класс бага, что уже
    чинили в старой версии (llm.is_actual_document), но здесь эта защита
    не применялась вообще, и autofix мог молча ПОДМЕНИТЬ рабочий документ
    (с реальными цифрами бюджета) на текст советов. Теперь при таком
    исходе просто отбрасываем результат autofix и возвращаем исходный
    (непеределанный, но настоящий) документ."""
    from llm import check_red_flags, autofix_red_flags, is_actual_document

    result = await check_red_flags(text, session_data)
    if result.get("passed") or not result.get("issues"):
        return text
    fixed = await autofix_red_flags(text, result["issues"], session_data)
    if not fixed or not await is_actual_document(fixed):
        logger.warning("autofix_red_flags returned non-document text — keeping original")
        return text
    return fixed


async def export_docx(text: str, session: dict) -> tuple[str, bool]:
    """Рендерит текст в .docx и возвращает (путь_к_файлу, official_template).
    Если build_final_document уже собрал файл новым путём (schema-fill) —
    просто возвращает его, ничего не переделывая. Иначе, если в сессии
    выбран конкретный файл формы донора — заполняет поля внутри него через
    старый (fuzzy-matching) пайплайн, сохраняя 100% таблиц/стилей донора.
    official_template=False значит, что реальный файл донора был ожидаем
    (has_donor_template), но недоступен/не смог быть заполнен — итоговый
    .docx лишь имитирует структуру формы обычным текстом, и вызывающий код
    должен явно предупредить пользователя, что это не тот файл, который
    примет донор."""
    import os
    import tempfile
    from docgen import markdown_to_docx
    from docx_template_fill import fill_donor_docx_template

    prebuilt = session.pop("_prebuilt_docx_path", None)
    if prebuilt and os.path.exists(prebuilt):
        return prebuilt, True

    project_data = session.get("project_data", {})
    has_donor_template = bool(project_data.get("donor_template"))
    title = None if has_donor_template else "Проект / Бизнес-план"

    tmp_dir = tempfile.mkdtemp()
    path = os.path.join(tmp_dir, "final_version.docx")

    donor_doc_path, _ = _resolve_donor_doc_path(session, tmp_dir)
    chosen_fn = session.get("chosen_donor_form")

    if donor_doc_path:
        success = await fill_donor_docx_template(donor_doc_path, text, path, session=session)
        if success:
            logger.info("Successfully populated chosen donor template docx (legacy pipeline): %s", donor_doc_path)
            return path, True
        logger.warning("fill_donor_docx_template returned False for %s — falling back to markdown_to_docx", donor_doc_path)
    elif has_donor_template:
        logger.warning(
            "donor_template expected but no usable file on disk/session for this session "
            "(chosen_donor_form=%r) — falling back to markdown_to_docx", chosen_fn,
        )

    markdown_to_docx(text, title, path)
    return path, not has_donor_template
