"""Регрессионные тесты для docx_schema_fill.py — единственный автотест,
который можно прогнать за секунды локально, БЕЗ Telegram и без реального
API-ключа (LLM-вызов подменяется). Раньше единственным способом заметить
регрессию в заполнении формы донора было живое тестирование в Telegram +
скачивание файла + ручной осмотр — цикл на день. Прогонять:

    python -m tests.test_docx_schema_fill

(или через pytest, если он установлен — файл не завязан на pytest-специфику,
только на assert, так что работает и без него).
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import docx

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "ggf_main_form.docx")


def test_extract_template_schema_finds_all_real_fields():
    """Живой реальный донорский файл (GGF, программа малых грантов для ЦА,
    публичный шаблон с smallgrantca.org) — 15 таблиц. Схема должна найти
    все содержательные поля и правильно разметить их вид (kv/section)."""
    from docx_schema_fill import extract_template_schema

    doc = docx.Document(FIXTURE)
    fields = extract_template_schema(doc)

    assert len(fields) >= 40, f"expected at least 40 fields in the real GGF form, got {len(fields)}"

    by_id = {f.field_id: f for f in fields}
    questions = {f.question for f in fields}

    # Несколько специфичных, всегда обязанных найтись полей — если это
    # ломается, значит extract_template_schema перестал видеть структуру
    # таблиц правильно.
    assert any("Полное название организации" in q for q in questions)
    assert any("Запрашиваемая сумма" in q for q in questions)
    assert any(q.startswith("ПРОЕКТ:") for q in questions)
    assert any(q.startswith("КОНТЕКСТ:") for q in questions)

    # kv vs section должны быть верно распознаны
    org_name_field = next(f for f in fields if "Полное название организации" in f.question and "англ" not in f.question)
    assert org_name_field.kind == "kv"
    project_field = next(f for f in fields if f.question.startswith("ПРОЕКТ:"))
    assert project_field.kind == "section"

    print(f"OK: extract_template_schema found {len(fields)} fields")


def test_donor_only_fields_are_filtered_before_llm_call():
    """'Рекомендующий Адвайзер (заполняется ГГФ)' — поле для донора, не для
    заявителя. fill_form_fields_batch не должен даже спрашивать модель про
    такие поля (looks_like_applicant_field), не то что писать туда ответ."""
    from llm import looks_like_applicant_field

    assert looks_like_applicant_field("Полное название организации") is True
    assert looks_like_applicant_field("Рекомендующий Адвайзер (заполняется ГГФ)") is False


def test_full_pipeline_with_mocked_llm_preserves_structure_and_places_answers_correctly():
    """Сквозной тест: схема -> структурированные ответы (LLM подменена) ->
    запись -> файл. Проверяет ИМЕННО то, что было источником всех багов на
    старом (fuzzy-matching) пайплайне: правильный ответ должен попасть В
    ПРАВИЛЬНУЮ ячейку, ни одна ячейка не должна получить чужой ответ, и
    структура документа (число таблиц) не должна пострадать."""
    import llm

    async def fake_call_claude(system_prompt, user_message, history=None, max_tokens=2000, prefer_anthropic=False):
        answers = {}
        for line in user_message.strip().split("\n"):
            fid = line.split(" ", 1)[0]
            answers[fid] = f"ANSWER-{fid}"
        return json.dumps(answers, ensure_ascii=False)

    original_call_claude = llm.call_claude
    llm.call_claude = fake_call_claude
    try:
        from docx_schema_fill import extract_template_schema, build_field_answers, write_answers_to_template

        async def run():
            doc = docx.Document(FIXTURE)
            fields = extract_template_schema(doc)
            answers = await build_field_answers(fields, {"org_info": "Test Org", "project_data": {}})

            # Донор-специфичное поле не должно было попасть в ответы вообще
            donor_only = next(f for f in fields if "заполняется ГГФ" in f.question)
            assert donor_only.field_id not in answers, "donor-only field must never be sent to the LLM or answered"

            filled_kv, filled_sections = write_answers_to_template(fields, answers)
            assert filled_kv > 0 and filled_sections > 0

            # Каждый ответ должен оказаться РОВНО в своей ячейке, не в чужой
            for f in fields:
                if f.field_id in answers:
                    assert f"ANSWER-{f.field_id}" in f.target.text, (
                        f"answer for {f.field_id} did not land in its own target cell"
                    )

            assert len(doc.tables) == 15, "template structure (table count) must survive filling untouched"
            print(f"OK: full pipeline filled {filled_kv} kv + {filled_sections} sections, structure intact")

        asyncio.run(run())
    finally:
        llm.call_claude = original_call_claude


if __name__ == "__main__":
    test_extract_template_schema_finds_all_real_fields()
    test_donor_only_fields_are_filtered_before_llm_call()
    test_full_pipeline_with_mocked_llm_preserves_structure_and_places_answers_correctly()
    print("\nAll tests passed.")
