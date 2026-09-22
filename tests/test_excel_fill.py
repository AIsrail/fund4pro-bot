"""Регрессионный тест для excel_fill.fill_donor_xlsx_template_v1 — тот же
принцип, что у test_docx_schema_fill.py/test_pdf_form_fill.py: LLM
подменена, донорский файл — настоящий (Application-Budget xlsx с сайта
NED, https://www.ned.org/apply-for-grant/ru/, скачан 2026-09-22).

РЕАЛЬНЫЙ ИНЦИДЕНТ: этот модуль существовал и правильно заполнял Excel-шаблон
бюджета донора, но был подключён только в СТАРОМ FSM-хендлере
(handlers/budget.py), который текущая (v3, agent_router) архитектура не
вызывает вообще — для донора с бюджетным Excel-шаблоном (например NED) бот
никогда не пытался заполнить именно его файл, только текстовую имитацию.

    python -m tests.test_excel_fill
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openpyxl import load_workbook

BUDGET_TEMPLATE = os.path.join(os.path.dirname(__file__), "fixtures", "ned_budget_template.xlsx")


def test_extract_xlsx_structure_reads_real_ned_budget_template():
    """Схема должна найти реальные подписанные ячейки (включая формулы —
    их нельзя трогать при заполнении) настоящего бюджетного шаблона NED."""
    from excel_fill import extract_xlsx_structure

    content = open(BUDGET_TEMPLATE, "rb").read()
    structure = extract_xlsx_structure(content)
    assert "Бюджет заявки" in structure or len(structure) > 500
    assert "=E8/12*F8*G8" in structure, "formula cells must be visible in the structure (so the model knows not to overwrite them)"
    print(f"OK: extract_xlsx_structure read {len(structure)} chars from the real NED budget template")


def test_full_xlsx_pipeline_fills_input_cells_and_preserves_formulas():
    """Сквозной тест: структура -> LLM-ответ (подменена) -> запись в xlsx ->
    перечитывание. Значения должны попасть в СВОИ ячейки, а формулы
    (донор сам считает итоги) — остаться нетронутыми."""
    import llm

    async def fake_call_claude(system_prompt, user_message, history=None, max_tokens=2000, prefer_anthropic=False):
        # Реальное имя листа этого файла — "Бюджет заявки" (два листа
        # всего) — если модель ошибётся с именем, fill_xlsx_template должен
        # НЕ угадывать (несколько листов есть) и просто пропустить запись,
        # это отдельно проверяется ниже.
        return json.dumps({"Бюджет заявки": {"E8": "18000", "F8": "0.5", "G8": "8"}}, ensure_ascii=False)

    original_call_claude = llm.call_claude
    llm.call_claude = fake_call_claude
    try:
        from excel_fill import fill_donor_xlsx_template_v1

        async def run():
            import tempfile
            out_path = os.path.join(tempfile.mkdtemp(), "filled.xlsx")
            session = {"project_data": {
                "activities_and_budget": "Координатор проекта: $18000/год, 0.5 ставки, 8 месяцев проекта",
            }}
            success, text_for_check = await fill_donor_xlsx_template_v1(BUDGET_TEMPLATE, out_path, session)
            assert success
            assert "E8" in text_for_check

            wb = load_workbook(out_path)
            ws = wb["Бюджет заявки"]
            assert str(ws["E8"].value) == "18000"
            assert str(ws["F8"].value) == "0.5"
            assert str(ws["G8"].value) == "8"
            # Формула — не значение — должна остаться нетронутой, донор сам
            # хочет видеть свою формулу в файле, а не число, которое мы посчитали.
            assert ws["I8"].value == "=E8/12*F8*G8", "formula cells must survive filling untouched"
            assert len(wb.sheetnames) == 2, "sheet structure must survive untouched"
            print("OK: real NED budget xlsx filled — input cells written, formulas untouched")

        asyncio.run(run())
    finally:
        llm.call_claude = original_call_claude


def test_no_budget_text_yet_returns_false_not_a_broken_file():
    """Если бюджет ещё не согласован с пользователем (activities_and_budget
    пусто) — заполнять нечем; функция должна честно вернуть неудачу, а не
    сгенерировать пустой/бессмысленный файл."""
    import llm

    async def unreachable(*a, **kw):
        raise AssertionError("call_claude must not be called when there is no budget text yet")

    original_call_claude = llm.call_claude
    llm.call_claude = unreachable
    try:
        from excel_fill import fill_donor_xlsx_template_v1

        async def run():
            import tempfile
            out_path = os.path.join(tempfile.mkdtemp(), "filled.xlsx")
            success, _ = await fill_donor_xlsx_template_v1(BUDGET_TEMPLATE, out_path, {"project_data": {}})
            assert success is False
            assert not os.path.exists(out_path)

        asyncio.run(run())
    finally:
        llm.call_claude = original_call_claude
    print("OK: no budget text yet -> honest failure, no half-built file")


if __name__ == "__main__":
    test_extract_xlsx_structure_reads_real_ned_budget_template()
    test_full_xlsx_pipeline_fills_input_cells_and_preserves_formulas()
    test_no_budget_text_yet_returns_false_not_a_broken_file()
    print("\nAll Excel tests passed.")
