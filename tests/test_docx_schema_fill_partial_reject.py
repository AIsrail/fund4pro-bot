"""Регрессионный тест для порога "частичное заполнение" в docx_schema_fill.py
и docx_template_fill.py.

РЕАЛЬНЫЙ ИНЦИДЕНТ (живой тест, 25.09.2026): условие отбраковки было
"ИЛИ" вместо "И" — форма с 5+ таблицами, у которой ПО ДИЗАЙНУ нет полей-
секций (только короткие kv-поля: название, даты, суммы — многие реальные
донорские шаблоны именно такие), заполнялась на 100% по kv-полям, но
`filled_sections < 1` истинно ВСЕГДА для такой формы (0 секций — не сбой,
а факт устройства шаблона) — весь результат выбрасывался как "частичное
заполнение", откатывался на legacy-пайплайн (тот же класс бага), и в
итоге пользователь получал документ НЕ по форме донора (markdown_to_docx)
вместо полностью и корректно заполненного оригинала.

    python -m tests.test_docx_schema_fill_partial_reject
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import docx


def _make_kv_only_template(n_tables: int = 6) -> "docx.Document":
    """Синтетический шаблон: n_tables таблиц, каждая — одна строка
    [метка, ПУСТАЯ ячейка] = kv-поле. Ни одной секции (открытого текстового
    вопроса) — воспроизводит форму донора, у которой все поля короткие."""
    doc = docx.Document()
    labels = [
        "Название организации", "Дата основания", "Юридический адрес",
        "Контактное лицо", "Запрашиваемая сумма", "Электронная почта",
        "Номер телефона", "Страна регистрации",
    ]
    for i in range(n_tables):
        table = doc.add_table(rows=1, cols=2)
        table.rows[0].cells[0].text = labels[i % len(labels)]
        table.rows[0].cells[1].text = ""
    return doc


def test_fully_filled_kv_only_form_is_not_rejected_for_having_zero_sections():
    """Шаблон без единой секции, но со 100%-заполненными kv-полями, должен
    считаться УСПЕХОМ, а не отбраковываться как 'частичное заполнение'."""
    import llm
    from docx_schema_fill import fill_donor_docx_template_v2

    async def fake_call_claude(system_prompt, user_message, history=None, max_tokens=2000, prefer_anthropic=False):
        answers = {line.split(" ", 1)[0]: "тестовое значение" for line in user_message.strip().split("\n")}
        return json.dumps(answers, ensure_ascii=False)

    original_call_claude = llm.call_claude
    llm.call_claude = fake_call_claude
    try:
        doc = _make_kv_only_template(n_tables=6)
        tmp_in = "test_kv_only_template.docx"
        tmp_out = "test_kv_only_output.docx"
        doc.save(tmp_in)
        try:
            async def run():
                session = {"project_data": {"org_info": "Test org"}}
                success, text = await fill_donor_docx_template_v2(tmp_in, tmp_out, session)
                assert success is True, (
                    "a form with zero section fields by design must not be rejected just because "
                    "filled_sections is 0 — only near-total emptiness should trigger the reject"
                )
                assert os.path.exists(tmp_out)
                assert "тестовое значение" in text

            asyncio.run(run())
        finally:
            for p in (tmp_in, tmp_out):
                if os.path.exists(p):
                    os.unlink(p)
    finally:
        llm.call_claude = original_call_claude
    print("OK: a kv-only form (0 sections by design) with 100% filled fields is accepted, not rejected")


def test_almost_empty_form_is_still_rejected():
    """Порог остаётся рабочим для настоящего сбоя: форма с 6 таблицами,
    из которых модель ответила лишь на пару полей — должна отбраковываться,
    как и раньше (защита от почти пустого документа не должна исчезнуть)."""
    import llm
    from docx_schema_fill import fill_donor_docx_template_v2

    async def fake_call_claude_mostly_empty(system_prompt, user_message, history=None, max_tokens=2000, prefer_anthropic=False):
        lines = user_message.strip().split("\n")
        answers = {}
        for i, line in enumerate(lines):
            fid = line.split(" ", 1)[0]
            answers[fid] = "значение" if i < 1 else ""  # только первое поле реально отвечено
        return json.dumps(answers, ensure_ascii=False)

    original_call_claude = llm.call_claude
    llm.call_claude = fake_call_claude_mostly_empty
    try:
        doc = _make_kv_only_template(n_tables=6)
        tmp_in = "test_almost_empty_template.docx"
        tmp_out = "test_almost_empty_output.docx"
        doc.save(tmp_in)
        try:
            async def run():
                session = {"project_data": {"org_info": "Test org"}}
                success, _ = await fill_donor_docx_template_v2(tmp_in, tmp_out, session)
                assert success is False, "a form where almost nothing got filled must still be rejected"
                assert not os.path.exists(tmp_out)

            asyncio.run(run())
        finally:
            for p in (tmp_in, tmp_out):
                if os.path.exists(p):
                    os.unlink(p)
    finally:
        llm.call_claude = original_call_claude
    print("OK: a near-empty fill result is still rejected — the guard's real purpose still works")


if __name__ == "__main__":
    test_fully_filled_kv_only_form_is_not_rejected_for_having_zero_sections()
    test_almost_empty_form_is_still_rejected()
    print("\nAll docx-schema-fill partial-reject tests passed.")
