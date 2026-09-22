"""Регрессионные тесты для pdf_form_fill.py — тот же принцип, что
tests/test_docx_schema_fill.py: без Telegram и без реального API-ключа
(LLM-вызов подменяется), на настоящих донорских файлах.

Фикстуры — реальные официальные PDF-формы NED (National Endowment for
Democracy), скачанные с https://www.ned.org/apply-for-grant/ru/ 2026-09-22:
публичные шаблоны, не персональные данные.

    python -m tests.test_pdf_form_fill
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pypdf import PdfReader

PROPOSAL_FORM = os.path.join(os.path.dirname(__file__), "fixtures", "ned_proposal_form.pdf")
ORG_PROFILE = os.path.join(os.path.dirname(__file__), "fixtures", "ned_org_profile.pdf")


def test_extract_pdf_form_schema_finds_real_ned_fields():
    """РЕАЛЬНАЯ ЖАЛОБА: у NED (в отличие от ГГФ) официальная форма — не
    .docx с таблицами, а заполняемый PDF (AcroForm). Бот раньше вообще не
    умел такое заполнять — только читать текст. Схема должна найти реальные
    текстовые и choice-поля обеих форм NED и правильно их разметить."""
    from pdf_form_fill import extract_pdf_form_schema

    content = open(PROPOSAL_FORM, "rb").read()
    fields = extract_pdf_form_schema(content)
    assert len(fields) >= 20, f"expected at least 20 fields in the real NED proposal form, got {len(fields)}"

    by_name = {f.name: f for f in fields}
    assert "Amount Requested" in by_name and by_name["Amount Requested"].kind == "text"
    assert "Project Country" in by_name and by_name["Project Country"].kind == "choice"
    assert by_name["Project Country"].options and "Kyrgyz Republic" in by_name["Project Country"].options

    # "Click here" — кнопка отправки формы по email со встроенным JS-действием,
    # не поле для ответа заявителя — НЕ должна попасть в схему вообще.
    assert "Click here" not in by_name, "the embedded email-submit button must never be offered as an answerable field"

    content2 = open(ORG_PROFILE, "rb").read()
    fields2 = extract_pdf_form_schema(content2)
    assert len(fields2) >= 80, f"expected at least 80 fields in the real NED org profile form, got {len(fields2)}"
    print(f"OK: extract_pdf_form_schema found {len(fields)} + {len(fields2)} real NED fields")


def test_full_pdf_pipeline_fills_real_fields_and_snaps_choice_values():
    """Сквозной тест: схема -> батч-ответы (LLM подменена) -> запись в PDF ->
    перечитывание записанного файла. Проверяет то же, что и docx-пайплайн:
    ответ должен попасть в СВОЁ поле, а choice-поле обязано стать РОВНО
    одним из допустимых значений формы, а не тем, что вернула модель
    дословно."""
    import llm

    async def fake_call_claude(system_prompt, user_message, history=None, max_tokens=2000, prefer_anthropic=False):
        answers = {}
        for line in user_message.strip().split("\n"):
            fid = line.split(" ", 1)[0]
            if "Amount Requested" in line:
                answers[fid] = "$7,182"
            elif "Project Country" in line:
                answers[fid] = "Kyrgyz Republic"  # ровно один из вариантов формы
            elif "Organization Legal Name" in line:
                answers[fid] = 'ОО "Дестинация Ош"'
            elif "APPLIED" in line or "RECIEVED" in line:
                answers[fid] = "No"
            elif "choice" in line:
                answers[fid] = ""
            else:
                answers[fid] = "XYZ"
        return json.dumps(answers, ensure_ascii=False)

    original_call_claude = llm.call_claude
    llm.call_claude = fake_call_claude
    try:
        from pdf_form_fill import extract_pdf_form_schema, build_pdf_field_answers, fill_pdf_form

        async def run():
            content = open(PROPOSAL_FORM, "rb").read()
            fields = extract_pdf_form_schema(content)
            answers = await build_pdf_field_answers(fields, {"project_data": {"org_info": "Test"}})

            new_bytes, filled = fill_pdf_form(content, fields, answers)
            assert filled > 0

            reader = PdfReader(__import__("io").BytesIO(new_bytes))
            written = reader.get_fields()
            assert written["Amount Requested"]["/V"] == "$7,182"
            assert written["Organization Legal Name"]["/V"] == 'ОО "Дестинация Ош"'
            # choice-поле: должно стать РОВНО валидным вариантом формы
            assert written["Project Country"]["/V"] == "Kyrgyz Republic"
            assert written["Have you ever APPLIED for a grant from our organization?"]["/V"] == "No"

            # AcroForm/NeedAppearances выставлен — иначе часть PDF-читалок
            # покажет заполненные поля пустыми, пока пользователь не кликнет
            # в них руками.
            acro = reader.trailer["/Root"].get("/AcroForm")
            assert acro is not None and bool(acro.get("/NeedAppearances"))

            assert len(reader.pages) == 2, "top-level PDF structure (page count) must survive filling untouched"
            print(f"OK: full PDF pipeline filled {filled} real NED fields, choice field snapped correctly")

        asyncio.run(run())
    finally:
        llm.call_claude = original_call_claude


def test_choice_field_with_no_matching_option_is_left_blank_not_guessed():
    """Безопасность важнее полноты: если модель дала значение, которого нет
    в списке вариантов донора (например не то написание страны), поле лучше
    оставить пустым, чем гадать похожее — неверная страна в заявке хуже
    пустого поля."""
    from pdf_form_fill import _snap_to_option

    options = ["Kyrgyz Republic", "Kazakhstan", "Tajikistan", "Uzbekistan"]
    assert _snap_to_option("Kyrgyz Republic", options) == "Kyrgyz Republic"
    assert _snap_to_option("kyrgyz republic", options) == "Kyrgyz Republic"  # регистр не важен
    assert _snap_to_option("Kyrgyzstan", options) == "", "a near-miss country name must not silently snap to a different country"
    assert _snap_to_option("", options) == ""
    print("OK: unmatched choice values are left blank instead of guessed")


if __name__ == "__main__":
    test_extract_pdf_form_schema_finds_real_ned_fields()
    test_full_pdf_pipeline_fills_real_fields_and_snaps_choice_values()
    test_choice_field_with_no_matching_option_is_left_blank_not_guessed()
    print("\nAll PDF tests passed.")
