"""Регрессионный тест для реестра документов донора (agent_engine.
_classify_donor_documents + agent_roadmap.compute_donor_documents_step) —
тот же принцип, что у остальных тестов пайплайна: реальные файлы NED,
LLM не задействована (классификация полностью на коде).

РЕАЛЬНАЯ ЖАЛОБА: "новичок полностью доверяет боту — а бот заполнил одну
форму из нескольких и остановился, пока пользователь не напомнил". Раньше
единственным "реестром" найденных документов была память модели внутри
разговора. Теперь его строит и обновляет код — эти тесты проверяют именно
эту часть, не полагаясь на то, что модель правильно себя поведёт.

    python -m tests.test_donor_documents_registry
"""

import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _saved_file(filename: str) -> dict:
    content = open(os.path.join(FIXTURES, filename), "rb").read()
    return {"filename": filename, "content_b64": base64.b64encode(content).decode("ascii")}


def test_classify_donor_documents_on_real_ned_files():
    """Классификация — чисто код (переиспользует extract_pdf_form_schema/
    extract_template_schema/extract_xlsx_structure, все три уже
    программные), без единого вызова LLM. Проверяем на реальных файлах
    NED: 2 формы с полями (PDF), 1 гайдлайны без полей (.docx), 1 бюджет (.xlsx)."""
    from agent_engine import _classify_donor_documents

    saved = [
        _saved_file("ned_org_profile.pdf"),
        _saved_file("ned_proposal_form.pdf"),
        _saved_file("ned_guidelines.docx"),
        _saved_file("ned_budget_template.xlsx"),
    ]
    docs = _classify_donor_documents(saved)
    by_name = {d["filename"]: d for d in docs}

    assert by_name["ned_org_profile.pdf"]["kind"] == "form"
    assert by_name["ned_proposal_form.pdf"]["kind"] == "form"
    assert by_name["ned_guidelines.docx"]["kind"] == "narrative", (
        "a section-headings-only docx (no table fields) must be classified as narrative content, not a form"
    )
    assert by_name["ned_budget_template.xlsx"]["kind"] == "budget"
    assert all(d["status"] == "pending" for d in docs)
    print("OK: real NED documents classified correctly (form/form/narrative/budget), no LLM call")


def test_compute_donor_documents_step_drives_the_next_pending_file():
    """ЭТАП 7 должен детерминированно указать на СЛЕДУЮЩИЙ несобранный
    документ по имени — не полагаясь на то, что модель сама вспомнит,
    что осталось."""
    from agent_roadmap import compute_donor_documents_step, compute_next_step

    project_data = {
        "org_info": "x" * 300,
        "donor_info": "NED",
        "donor_template": "structure",
        "problem_and_idea": "x" * 50,
        "goal_and_objectives": "x" * 50,
        "activities_and_budget": "x" * 50,
        "donor_documents": [
            {"filename": "ned_org_profile.pdf", "kind": "form", "status": "filled"},
            {"filename": "ned_proposal_form.pdf", "kind": "form", "status": "pending"},
            {"filename": "ned_guidelines.docx", "kind": "narrative", "status": "pending"},
            {"filename": "ned_budget_template.xlsx", "kind": "budget", "status": "pending"},
        ],
    }
    step = compute_donor_documents_step(project_data)
    assert step is not None
    assert 'select_donor_form("ned_proposal_form.pdf")' in step, (
        f"must point at the first PENDING document by exact filename, got: {step!r}"
    )

    full_step = compute_next_step(project_data, "grant")
    assert "ЭТАП 7" in full_step
    assert "ned_proposal_form.pdf" in full_step

    # Всё собрано -> None, ЭТАП 7 сам скажет "всё собрано" вместо директивы по файлу.
    all_filled = {**project_data, "donor_documents": [
        {**d, "status": "filled"} for d in project_data["donor_documents"]
    ]}
    assert compute_donor_documents_step(all_filled) is None
    finished_step = compute_next_step(all_filled, "grant")
    assert "Всё собрано" in finished_step
    print("OK: compute_donor_documents_step drives the next pending file by name, and clears when all done")


if __name__ == "__main__":
    test_classify_donor_documents_on_real_ned_files()
    test_compute_donor_documents_step_drives_the_next_pending_file()
    print("\nAll donor-documents-registry tests passed.")
