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

    # kv vs section/table должны быть верно распознаны
    org_name_field = next(f for f in fields if "Полное название организации" in f.question and "англ" not in f.question)
    assert org_name_field.kind == "kv"
    context_field = next(f for f in fields if f.question.startswith("КОНТЕКСТ:"))
    assert context_field.kind == "section"

    # РЕАЛЬНЫЙ ИНЦИДЕНТ: "ПРОЕКТ" и "Бюджет" в реальной форме ГГФ — это не
    # абзац текста, а ВЛОЖЕННАЯ В ЯЧЕЙКУ ТАБЛИЦА (донор жёстко требует
    # построчный формат — "№ | Мероприятие | Срок | Результат" и "№ | Статья
    # расходов | Кол-во | Стоимость"). Раньше эти поля классифицировались
    # как "section", вложенная таблица оставалась пустой навсегда, и
    # пользователь совершенно справедливо жаловался, что "план и бюджет не
    # вписаны в таблицу" — это ИМЕННО то, что должен ловить этот тест.
    project_field = next(f for f in fields if f.question.startswith("ПРОЕКТ:"))
    assert project_field.kind == "table", "план мероприятий формы ГГФ должен распознаваться как вложенная таблица"
    assert project_field.columns == ["№", "Мероприятие", "Срок", "Ожидаемый результат"]

    budget_field = next(f for f in fields if "подробный бюджет проекта" in f.question)
    assert budget_field.kind == "table", "построчный бюджет формы ГГФ должен распознаваться как вложенная таблица"
    assert budget_field.columns[0] == "№" and budget_field.columns[-1] == "Стоимость"

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
        if user_message.strip().startswith("Вопрос формы:"):
            # fill_table_field-запрос — ждёт JSON-массив массивов, не dict.
            return json.dumps([["1", "TABLE-ROW-1-COL2", "TABLE-ROW-1-COL3", "100"],
                                ["2", "TABLE-ROW-2-COL2", "TABLE-ROW-2-COL3", "200"]], ensure_ascii=False)
        answers = {}
        for line in user_message.strip().split("\n"):
            fid = line.split(" ", 1)[0]
            answers[fid] = f"ANSWER-{fid}"
        return json.dumps(answers, ensure_ascii=False)

    original_call_claude = llm.call_claude
    llm.call_claude = fake_call_claude
    try:
        from docx_schema_fill import (
            extract_template_schema, build_field_answers, build_table_answers, write_answers_to_template,
        )

        async def run():
            doc = docx.Document(FIXTURE)
            fields = extract_template_schema(doc)
            session_data = {"org_info": "Test Org", "project_data": {}}
            answers = await build_field_answers(fields, session_data)
            table_answers = await build_table_answers(fields, session_data)

            # Донор-специфичное поле не должно было попасть в ответы вообще
            donor_only = next(f for f in fields if "заполняется ГГФ" in f.question)
            assert donor_only.field_id not in answers, "donor-only field must never be sent to the LLM or answered"

            filled_kv, filled_sections, filled_tables = write_answers_to_template(fields, answers, table_answers)
            assert filled_kv > 0 and filled_sections > 0
            assert filled_tables == 2, f"expected both table fields (план + бюджет) filled, got {filled_tables}"

            # Каждый ответ должен оказаться РОВНО в своей ячейке, не в чужой
            for f in fields:
                if f.kind == "table":
                    continue
                if f.field_id in answers:
                    assert f"ANSWER-{f.field_id}" in f.target.text, (
                        f"answer for {f.field_id} did not land in its own target cell"
                    )

            # РЕАЛЬНЫЙ ИНЦИДЕНТ: пользователь открыл присланный docx и увидел,
            # что план/бюджет НЕ вписаны в таблицу формы, хотя донор это
            # требует. Проверяем именно это: вложенная таблица "Рабочий план"
            # (table 9) должна теперь содержать реальные СТРОКИ с данными,
            # а не один пустой шаблонный ряд.
            project_field = next(f for f in fields if f.question.startswith("ПРОЕКТ:"))
            nested, _, _ = project_field.target
            row_texts = [[c.text.strip() for c in row.cells] for row in nested.rows]
            assert any("TABLE-ROW-1-COL2" in " ".join(r) for r in row_texts), (
                "answer row did not land inside the nested Рабочий план table"
            )
            assert not any(all(c == "" for c in r) for r in row_texts[1:]), (
                "empty template row should have been replaced by real data rows, not left blank"
            )

            # Бюджетная таблица (table 10) с строкой "ИТОГО" — сумма должна
            # быть посчитана по фактически записанным строкам (100 + 200).
            budget_field = next(f for f in fields if "подробный бюджет проекта" in f.question)
            b_nested, _, _ = budget_field.target
            total_row_text = " ".join(c.text.strip() for c in b_nested.rows[-1].cells)
            assert "300" in total_row_text, f"ИТОГО row should sum written amounts (100+200=300), got: {total_row_text!r}"

            assert len(doc.tables) == 15, "top-level template structure (table count) must survive filling untouched"
            print(
                f"OK: full pipeline filled {filled_kv} kv + {filled_sections} sections + "
                f"{filled_tables} nested tables, structure intact"
            )

        asyncio.run(run())
    finally:
        llm.call_claude = original_call_claude


def test_xyz_share_is_capped_by_second_pass():
    """Пользователь: «90% должны быть данные, только 10% можно XYZ». Если
    доля XYZ выше 10%, enforce_xyz_budget должен заменить XYZ оценками."""
    import llm
    from docx_schema_fill import enforce_xyz_budget, xyz_ratio

    async def fake_call_claude(system_prompt, user_message, history=None, max_tokens=2000, prefer_anthropic=False):
        out = {}
        for line in user_message.strip().split(chr(10)):
            key, _, text = line.partition(": ")
            out[key] = text.replace("XYZ", "около 120")
        return json.dumps(out, ensure_ascii=False)

    original = llm.call_claude
    llm.call_claude = fake_call_claude
    try:
        answers = {"f1": "XYZ туристов, рост XYZ%, XYZ тонн", "f2": "Проект длится 12 месяцев, 4 семинара"}
        table_answers = {"f3": [["1", "Семинары", "XYZ мес.", "XYZ"]]}
        assert xyz_ratio(list(answers.values())) > 0.10
        final = asyncio.run(enforce_xyz_budget(answers, table_answers, {"org_info": "x"}))
        assert final <= 0.10, final
        assert "XYZ" not in answers["f1"] and "около 120" in answers["f1"]
        assert "XYZ" not in table_answers["f3"][0][2]
        print("OK: XYZ share capped at <=10% via second pass")
    finally:
        llm.call_claude = original


def test_all_providers_down_rolls_back_history_and_flags_outage():
    """Все LLM-провайдеры недоступны: реплика пользователя не должна
    оставаться в истории (иначе путаница шагов при повторе), ответ —
    понятное сообщение, llm_unavailable=True (для уведомления владельца)."""
    import agent_engine

    async def none_turn(*a, **k):
        return None

    saved = (agent_engine._anthropic_turn, agent_engine._chatgpt_turn,
             agent_engine._gemini_turn, agent_engine._deepseek_turn)
    agent_engine._anthropic_turn = none_turn
    agent_engine._chatgpt_client = agent_engine._fallback_client = agent_engine._deepseek_client = None
    agent_engine.config.ANTHROPIC_API_KEY = "x"
    try:
        session = {"history_openai": [{"role": "user", "content": "старое"}, {"role": "assistant", "content": "ответ"}]}
        result = asyncio.run(agent_engine.run_agent_turn(session, "новое сообщение"))
        assert result.llm_unavailable is True
        assert [m["content"] for m in session["history_openai"]] == ["старое", "ответ"], session["history_openai"]
        assert "потеряно" in result.reply
        print("OK: provider outage rolls back history and flags llm_unavailable")
    finally:
        (agent_engine._anthropic_turn, agent_engine._chatgpt_turn,
         agent_engine._gemini_turn, agent_engine._deepseek_turn) = saved


if __name__ == "__main__":
    test_extract_template_schema_finds_all_real_fields()
    test_donor_only_fields_are_filtered_before_llm_call()
    test_full_pipeline_with_mocked_llm_preserves_structure_and_places_answers_correctly()
    test_xyz_share_is_capped_by_second_pass()
    test_all_providers_down_rolls_back_history_and_flags_outage()
    print("\nAll tests passed.")
