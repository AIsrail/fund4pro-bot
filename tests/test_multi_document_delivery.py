"""Регрессионный тест для доставки НЕСКОЛЬКИХ документов донора за один ход.

РЕАЛЬНЫЙ ИНЦИДЕНТ (живой тест NED, 23-24.09.2026): донор требует несколько
документов (профиль организации + заявка на грант + бюджет), agent_engine
корректно заполняет и помечает "filled" КАЖДЫЙ из них в реестре, но
пользователю физически уходил только ОДИН файл за весь ход. Причина:
agent_docgen.export_docx() читает session["_prebuilt_pdf_path"] (и
_xlsx_path/_docx_path) — скалярные поля, которые каждый следующий
generate_document в том же ходу молча ПЕРЕЗАПИСЫВАЛ, а export_docx
вызывался ОДИН раз, в конце всего хода (agent_router._send_docx). Из
пакета в несколько PDF-документов реально уходил только последний
(или первый — export_docx проверяет pdf/xlsx/docx в этом порядке и
возвращается на первом найденном), остальные — заполненные, отмеченные
"filled" в реестре, но никогда не отправленные — оставались невидимыми
и для пользователя, и для системы (реестр уже не даст их перегенерировать).

Фикс (agent_engine.run_agent_turn): export_docx вызывается СРАЗУ после
каждого отдельного generate_document, пока prebuilt-путь ЭТОГО документа
ещё не затёрт следующим вызовом — результат складывается в список
generated_documents, а не в одно скалярное поле. Этот тест проверяет
именно то, что делает фикс корректным: export_docx, вызванный сразу после
каждого _try_schema_fill, должен вернуть СВОЙ, а не чужой файл, даже когда
оба документа — PDF (тот самый частный случай, где старый export_docx
находил PDF-путь первым и никогда не добирался до xlsx/docx).

    python -m tests.test_multi_document_delivery
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
ORG_PROFILE = os.path.join(FIXTURES, "ned_org_profile.pdf")
PROPOSAL_FORM = os.path.join(FIXTURES, "ned_proposal_form.pdf")


def _fake_call_claude_factory():
    async def fake_call_claude(system_prompt, user_message, history=None, max_tokens=2000, prefer_anthropic=False):
        answers = {}
        for line in user_message.strip().split("\n"):
            fid = line.split(" ", 1)[0]
            answers[fid] = "" if "choice" in line else "test value"
        return json.dumps(answers, ensure_ascii=False)
    return fake_call_claude


def test_two_pdf_documents_in_one_turn_each_get_their_own_file():
    """Два PDF-документа (профиль организации + форма заявки), заполненные
    один за другим в рамках одного хода: export_docx, вызванный СРАЗУ после
    каждого, должен вернуть ДВА РАЗНЫХ файла — не молча схлопнуться в один,
    как было до фикса (export_docx() в конце хода видел бы только последний
    записанный session["_prebuilt_pdf_path"])."""
    import llm
    from agent_docgen import _try_schema_fill, export_docx

    original_call_claude = llm.call_claude
    llm.call_claude = _fake_call_claude_factory()
    try:
        async def run():
            session = {
                "project_data": {"donor_template": "some structure", "org_info": "Test org"},
                "saved_donor_files": [
                    {"filename": "ned_org_profile.pdf", "path": ORG_PROFILE},
                    {"filename": "ned_proposal_form.pdf", "path": PROPOSAL_FORM},
                ],
            }

            # Документ 1: профиль организации
            session["chosen_donor_form"] = "ned_org_profile.pdf"
            prebuilt_1 = await _try_schema_fill(session)
            assert prebuilt_1 is not None, "schema-fill must succeed on the real org-profile fixture"
            text_1 = session["final_document_text"]
            # ФИКС: export_docx вызывается СРАЗУ, пока путь документа 1 ещё не затёрт.
            path_1, official_1 = await export_docx(text_1, session)
            assert os.path.exists(path_1)
            assert official_1 is True

            # Документ 2: форма заявки — тоже PDF, тот самый коллизионный случай
            # (до фикса export_docx всегда находил "_prebuilt_pdf_path" первым).
            session["chosen_donor_form"] = "ned_proposal_form.pdf"
            prebuilt_2 = await _try_schema_fill(session)
            assert prebuilt_2 is not None, "schema-fill must succeed on the real proposal-form fixture"
            text_2 = session["final_document_text"]
            path_2, official_2 = await export_docx(text_2, session)
            assert os.path.exists(path_2)
            assert official_2 is True

            assert path_1 != path_2, (
                "each document generated in the same turn must produce its OWN file — "
                "if this fails, the multi-document delivery bug is back"
            )
            # Оба файла реально существуют одновременно (не один уже стёрт/перезаписан).
            assert os.path.exists(path_1) and os.path.exists(path_2)

            # export_docx() ПОПАЕТ prebuilt-путь — после обоих вызовов в session
            # не должно остаться "зависшего" пути документа 1, который могла бы
            # по ошибке подхватить какая-то ТРЕТЬЯ, никак не связанная генерация.
            assert "_prebuilt_pdf_path" not in session

            os.unlink(path_1)
            os.unlink(path_2)

        asyncio.run(run())
    finally:
        llm.call_claude = original_call_claude
    print("OK: two PDF documents generated in one turn each produce their own file, not a shared/overwritten one")


if __name__ == "__main__":
    test_two_pdf_documents_in_one_turn_each_get_their_own_file()
    print("\nAll multi-document-delivery tests passed.")
