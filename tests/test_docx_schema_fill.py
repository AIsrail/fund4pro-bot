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
            # Суммы намеренно НЕ круглые (103+211=314) — круглая сумма
            # запускает _nudge_round_total и меняет одну из строк (см.
            # test_round_budget_total_gets_nudged_to_a_non_round_number
            # ниже), это отдельная проверка, не должна путаться с этой.
            return json.dumps([["1", "TABLE-ROW-1-COL2", "TABLE-ROW-1-COL3", "103"],
                                ["2", "TABLE-ROW-2-COL2", "TABLE-ROW-2-COL3", "211"]], ensure_ascii=False)
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
            assert "314" in total_row_text, f"ИТОГО row should sum written amounts (103+211=314), got: {total_row_text!r}"

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


def test_project_prose_goes_above_plan_table_and_contacts_are_sanitized():
    """Реальный инцидент (заявка на ГГФ): описание проекта в ячейке с вложенной
    таблицей плана не писалось вообще; в контактах стояли «Германия»/«2026»."""
    from docx_schema_fill import (
        extract_template_schema, write_answers_to_template, sanitize_contact_answers,
    )

    doc = docx.Document(FIXTURE)
    fields = extract_template_schema(doc)

    prose = next(f for f in fields if f.kind == "section" and f.before_table)
    assert prose.question.startswith("[только текстовая часть]"), prose.question
    assert prose.target.tables, "prose field must live in the cell that holds the nested plan table"

    answers = {prose.field_id: "PROJECT-PROSE-MARKER"}
    write_answers_to_template(fields, answers, {})
    cell_xml = list(prose.target._tc.iterchildren())
    tbl_idx = next(i for i, el in enumerate(cell_xml) if el.tag.endswith("}tbl"))
    prose_idx = next(i for i, el in enumerate(cell_xml) if "PROJECT-PROSE-MARKER" in "".join(el.itertext()))
    assert prose_idx < tbl_idx, "project description must be ABOVE the nested plan table"

    def kv(label_part):
        return next(f for f in fields if f.kind == "kv" and label_part in f.question.lower())

    contact, site, phone, email = kv("контактное лицо"), kv("вебсайт"), kv("телефон"), kv("электронной почты")
    ans = {contact.field_id: "Германия", site.field_id: "2026", phone.field_id: "2026", email.field_id: "2026"}
    assert sanitize_contact_answers(fields, ans) == 4
    assert all(v == "XYZ" for v in ans.values()), ans

    good = {contact.field_id: "Азамат Токтогулов", site.field_id: "destinatsiya.kg",
            phone.field_id: "+996 555 123 456", email.field_id: "a@b.kg"}
    assert sanitize_contact_answers(fields, dict(good)) == 0
    print("OK: project prose above plan table; junk contacts replaced with XYZ")


def test_truncated_table_json_keeps_complete_rows():
    """Ответ модели по плану обрезался по max_tokens — раньше терялась ВСЯ
    таблица (план оставался пустым). Теперь берутся все закрытые строки."""
    from llm import _parse_json_rows

    raw = 'Вот план: [["1", "Тренинг", "март", "20 чел."], ["2", "Сайт", "апрель", "1 сайт"], ["3", "Кру'
    rows = _parse_json_rows(raw)
    assert rows == [["1", "Тренинг", "март", "20 чел."], ["2", "Сайт", "апрель", "1 сайт"]], rows
    full = _parse_json_rows('[["1","a","b","c"],["2","d","e","f"]]')
    assert len(full) == 2
    print("OK: truncated table JSON still yields complete rows")


def test_round_budget_total_gets_nudged_to_a_non_round_number():
    """Жалоба пользователя: бюджет заканчивается РОВНО на 5000 — для донора
    это выглядит как «заявитель не считал, подогнал под лимит». Если модель
    всё же вернула круглую (кратную 100) сумму, код должен молча поправить
    одну строку так, чтобы итог стал НЕ круглым (…5140/…5250 из примера
    пользователя), а не тем же ровным числом."""
    from docx_schema_fill import _nudge_round_total, _extract_number

    columns = ["№", "Мероприятие, статья расходов", "Единица измерения, количество", "Стоимость"]
    rows = [
        ["1", "Контейнеры", "9 шт", "$1 800"],
        ["2", "Таблички", "20 шт", "$400"],
        ["3", "Экостандарт", "1 комплект", "$600"],
        ["4", "Семинар", "1 семинар", "$800"],
        ["5", "Встречи", "3 встречи", "$250"],
        ["6", "Админ расходы", "12% от бюджета", "$600"],
        ["7", "M&E", "6% от бюджета", "$300"],
        ["8", "Контингенси", "4% от бюджета", "$250"],
    ]
    total_before = sum(_extract_number(r[-1]) for r in rows)
    assert total_before == 5000, total_before  # круглая — как в реальной жалобе

    _nudge_round_total(rows, columns)

    total_after = sum(_extract_number(r[-1]) for r in rows)
    assert total_after != 5000, "total must no longer be the suspiciously round number"
    assert total_after % 100 != 0, f"nudged total should not still be round, got {total_after}"
    assert 5000 < total_after < 5300, f"nudge should be a small realistic adjustment, got {total_after}"
    # Не денежная колонка (план мероприятий) — не трогаем вообще.
    plan_rows = [["1", "Тренинг", "март", "20 чел."]]
    plan_before = list(plan_rows)
    _nudge_round_total(plan_rows, ["№", "Мероприятие", "Срок", "Ожидаемый результат"])
    assert plan_rows == plan_before, "non-money table must be left untouched"
    print(f"OK: round budget total {total_before} nudged to non-round {total_after}")


def test_truncated_form_fields_batch_recovers_complete_fields_not_the_whole_batch():
    """РЕАЛЬНЫЙ ИНЦИДЕНТ (Render, эта же сессия): батч из 12 полей на
    max_tokens=4000 обрезался ДО закрывающей "}" — весь батч (включая раздел
    КОНТЕКСТ, историю, цели организации) терялся целиком. Проверяем прямо
    воспроизведённый фрагмент того самого обрезанного ответа из лога."""
    from llm import _parse_json_object

    raw = (
        '```json\n{\n'
        '  "f25": "2019 год",\n'
        '  "f26": "г. Ош",\n'
        '  "f27": "Да, зарегистрирована",\n'
        '  "f28": "",\n'
        '  "f29": "Цель ОО «Дестинация'
    )
    result = _parse_json_object(raw)
    assert result["f25"] == "2019 год"
    assert result["f26"] == "г. Ош"
    assert result["f27"] == "Да, зарегистрирована"
    assert result["f28"] == ""
    assert "f29" not in result, "the field cut off mid-string must not be recovered as garbage"
    assert len(result) == 4, f"expected the 4 complete fields before the cut, got {result}"

    try:
        _parse_json_object("это не JSON вообще, скобок тоже нет")
        raised = False
    except ValueError:
        raised = True
    assert raised, "garbage input with no '{' must raise, not silently return {}"
    print("OK: truncated fill_form_fields_batch response recovers complete fields, not the whole batch")


if __name__ == "__main__":
    test_extract_template_schema_finds_all_real_fields()
    test_donor_only_fields_are_filtered_before_llm_call()
    test_full_pipeline_with_mocked_llm_preserves_structure_and_places_answers_correctly()
    test_xyz_share_is_capped_by_second_pass()
    test_all_providers_down_rolls_back_history_and_flags_outage()
    test_project_prose_goes_above_plan_table_and_contacts_are_sanitized()
    test_truncated_table_json_keeps_complete_rows()
    test_round_budget_total_gets_nudged_to_a_non_round_number()
    test_truncated_form_fields_batch_recovers_complete_fields_not_the_whole_batch()
    print("\nAll tests passed.")
