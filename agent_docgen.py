"""Сборка финального документа для агентной архитектуры — переиспользует
существующую логику генерации/red-flags/docx-экспорта из llm.py и docgen.py
(эти модули менять не пришлось, они уже провайдеро-независимые благодаря
fallback в llm.call_claude)."""

import logging

logger = logging.getLogger("fund4pro.agent_docgen")


async def build_final_document(session: dict) -> str:
    """Генерирует текст финального документа из session['project_data'],
    прогоняет через red-flags аудит с одной попыткой автоисправления (тот
    же цикл, что был в handlers/final_version.py, но БЕЗ бесконечных
    повторов — см. комментарий в _run_red_flags_gate ниже)."""
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


async def export_docx(text: str, session: dict) -> str:
    """Рендерит текст в .docx и возвращает путь к временному файлу.
    Если в сессии выбран конкретный файл формы донора (chosen_donor_form
    + saved_donor_files) — заполняет поля прямо внутри этого файла,
    сохраняя все 100% таблиц, стилей и логотипов донора без изменений."""
    import os
    import tempfile
    from docgen import markdown_to_docx
    from docx_template_fill import fill_donor_docx_template

    project_data = session.get("project_data", {})
    has_donor_template = bool(project_data.get("donor_template"))
    title = None if has_donor_template else "Проект / Бизнес-план"

    tmp_dir = tempfile.mkdtemp()
    path = os.path.join(tmp_dir, "final_version.docx")

    # Ищем путь к ВЫБРАННОМУ пользователем/моделью файлу формы донора
    donor_doc_path = None
    chosen_fn = session.get("chosen_donor_form")
    if chosen_fn:
        for f in session.get("saved_donor_files", []):
            if f.get("filename") == chosen_fn:
                donor_doc_path = f.get("path")
                break

    # Fallback: если выбор не сохранён, но есть donor_template_file_path (старая логика)
    if not donor_doc_path:
        donor_doc_path = session.get("donor_template_file_path")

    if donor_doc_path and os.path.exists(donor_doc_path):
        success = await fill_donor_docx_template(donor_doc_path, text, path, session=session)
        if success:
            logger.info("Successfully populated chosen donor template docx: %s", donor_doc_path)
            return path

    markdown_to_docx(text, title, path)
    return path
