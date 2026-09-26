"""Регрессионные тесты для двух платных гейтов, добавленных 25-26.09.2026
по прямой просьбе владельца ("бесплатный тест бота"):

1. Скачивание официального шаблона донора — N раз бесплатно, дальше платно,
   но счёт выставляется ТОЛЬКО ПОСЛЕ того, как скачивание реально удалось
   (agent_engine._execute_tool, "fetch_donor_page") — иначе бот мог бы взять
   деньги за то, что донор не отдал (антибот, 404 и т.п.). Гейтится именно
   ВЫДАЧА сырого файла пользователю (_pending_file_attachments) — свою же
   копию для извлечения структуры бот сохраняет всегда, это не должно
   ломать бесплатную генерацию текста заявки.

2. Сборка итогового документа В ОФИЦИАЛЬНЫЙ ФАЙЛ шаблона донора — платно с
   первого раза (FREE_FILE_EXPORTS=0 по умолчанию), независимо от лимита
   проектов: бесплатно — готовый ТЕКСТ заявки в чате.

    python -m tests.test_paid_template_and_export_gating
"""

import asyncio
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
GGF_FORM = os.path.join(FIXTURES, "ggf_main_form.docx")


async def _fake_call_claude(system_prompt, user_message, history=None, max_tokens=2000, prefer_anthropic=False):
    # extract_donor_template_structure / detect_doc_language (вызываются
    # из fetch_donor_page для единственной читаемой формы) оба идут через
    # call_claude — подменяем, чтобы тест не стучался в реальные LLM API.
    return "ru"


def test_template_download_over_free_limit_withholds_the_file_and_queues_an_invoice():
    import agent_engine
    import billing
    import donor_scrape
    import llm

    content = open(GGF_FORM, "rb").read()

    async def fake_scrape(url):
        return [{"url": url, "filename": "shablon.docx", "text": "some text", "content": content}], "page text"

    async def fake_can_use(chat_id, resource):
        assert resource == "template_download"
        return False  # лимит уже исчерпан

    consumed = {"called": False}

    async def fake_consume(chat_id, resource):
        consumed["called"] = True

    orig_scrape = donor_scrape.try_scrape_donor_forms
    orig_can_use, orig_consume = billing.can_use, billing.consume
    orig_call_claude = llm.call_claude
    donor_scrape.try_scrape_donor_forms = fake_scrape
    billing.can_use, billing.consume = fake_can_use, fake_consume
    llm.call_claude = _fake_call_claude
    try:
        async def run():
            session = {"_chat_id": 123, "project_data": {}}
            result = await agent_engine._execute_tool("fetch_donor_page", {"url": "http://example.com"}, session)
            return session, result

        session, result = asyncio.run(run())
    finally:
        donor_scrape.try_scrape_donor_forms = orig_scrape
        billing.can_use, billing.consume = orig_can_use, orig_consume
        llm.call_claude = orig_call_claude

    assert not session.get("_pending_file_attachments"), (
        "the raw file must NOT be queued for the user once the free download limit is exhausted"
    )
    assert session.get("_pending_invoice") == "template_download"
    withheld = session.get("_pending_paid_template_files") or []
    assert len(withheld) == 1 and withheld[0]["filename"] == "shablon.docx"
    assert "content_b64" in withheld[0]
    assert base64.b64decode(withheld[0]["content_b64"]) == content
    assert not consumed["called"], "nothing should be consumed when the download is withheld — there's nothing to bill yet"
    assert session.get("saved_donor_files"), (
        "the bot must still keep its OWN copy internally (for structure/content generation) "
        "even when the raw file isn't handed to the user"
    )
    assert "оплату" in result or "счёт" in result, "the model must be told a payment is pending"
    print("OK: exhausting the free template-download limit withholds the raw file and queues an invoice")


def test_template_download_within_free_limit_delivers_normally_and_consumes_one_credit():
    import agent_engine
    import billing
    import donor_scrape
    import llm

    content = open(GGF_FORM, "rb").read()

    async def fake_scrape(url):
        return [{"url": url, "filename": "shablon.docx", "text": "some text", "content": content}], "page text"

    async def fake_can_use(chat_id, resource):
        return True

    consumed = {"resource": None}

    async def fake_consume(chat_id, resource):
        consumed["resource"] = resource

    orig_scrape = donor_scrape.try_scrape_donor_forms
    orig_can_use, orig_consume = billing.can_use, billing.consume
    orig_call_claude = llm.call_claude
    donor_scrape.try_scrape_donor_forms = fake_scrape
    billing.can_use, billing.consume = fake_can_use, fake_consume
    llm.call_claude = _fake_call_claude
    try:
        async def run():
            session = {"_chat_id": 123, "project_data": {}}
            await agent_engine._execute_tool("fetch_donor_page", {"url": "http://example.com"}, session)
            return session

        session = asyncio.run(run())
    finally:
        llm.call_claude = orig_call_claude
        donor_scrape.try_scrape_donor_forms = orig_scrape
        billing.can_use, billing.consume = orig_can_use, orig_consume

    assert session.get("_pending_file_attachments"), "within the free limit, the raw file must reach the user as before"
    assert not session.get("_pending_invoice")
    assert consumed["resource"] == "template_download"
    print("OK: a download within the free limit is delivered normally and consumes exactly one credit")


def test_file_export_over_free_limit_sends_text_only_and_queues_an_invoice():
    import agent_engine
    import billing

    async def fake_can_use(chat_id, resource):
        assert resource == "file_export"
        return False

    orig_can_use = billing.can_use
    billing.can_use = fake_can_use
    try:
        async def run():
            session = {"_chat_id": 123}
            generated_documents = []
            delivered_as_file = await agent_engine._deliver_generated_document(
                "shablon.docx", "текст готовой заявки" * 50, {}, session, generated_documents,
            )
            return session, generated_documents, delivered_as_file

        session, generated_documents, delivered_as_file = asyncio.run(run())
    finally:
        billing.can_use = orig_can_use

    assert delivered_as_file is False
    assert generated_documents == [], "no file must be produced when file_export is not paid for"
    pending_texts = session.get("_pending_text_documents") or []
    assert len(pending_texts) == 1 and pending_texts[0]["filename"] == "shablon.docx"
    assert session.get("_pending_invoice") == "file_export"
    assert session.get("_pending_file_export") == {"filename": "shablon.docx"}
    print("OK: exhausting the free file-export limit delivers text only and queues an invoice, no file produced")


def test_file_export_within_free_limit_would_attempt_export_and_consume_one_credit():
    """Не собираем реальный docx здесь (это уже покрыто test_docx_schema_fill.py
    и test_multi_document_delivery.py) — проверяем именно КОНТРАКТ гейта:
    при can_use=True списывается ровно один кредит file_export, и функция
    пытается материализовать файл (а не молча уходит в текстовую ветку)."""
    import agent_engine
    import billing

    async def fake_can_use(chat_id, resource):
        return True

    consumed = {"resource": None}

    async def fake_consume(chat_id, resource):
        consumed["resource"] = resource

    orig_can_use, orig_consume = billing.can_use, billing.consume
    billing.can_use, billing.consume = fake_can_use, fake_consume
    try:
        async def run():
            session = {"_chat_id": 123}  # без chosen_donor_form/saved_donor_files -> export_docx падает на markdown_to_docx
            generated_documents = []
            delivered_as_file = await agent_engine._deliver_generated_document(
                "shablon.docx", "# Заявка\n\nСодержимое заявки для теста.", {}, session, generated_documents,
            )
            return session, generated_documents, delivered_as_file

        session, generated_documents, delivered_as_file = asyncio.run(run())
    finally:
        billing.can_use, billing.consume = orig_can_use, orig_consume

    assert consumed["resource"] == "file_export", "a credit must be consumed as soon as the export is allowed"
    assert delivered_as_file is True
    assert len(generated_documents) == 1 and generated_documents[0]["filename"] == "shablon.docx"
    assert os.path.exists(generated_documents[0]["path"])
    os.unlink(generated_documents[0]["path"])
    os.rmdir(os.path.dirname(generated_documents[0]["path"]))
    print("OK: an allowed file-export consumes exactly one credit and produces a real file")


if __name__ == "__main__":
    test_template_download_over_free_limit_withholds_the_file_and_queues_an_invoice()
    test_template_download_within_free_limit_delivers_normally_and_consumes_one_credit()
    test_file_export_over_free_limit_sends_text_only_and_queues_an_invoice()
    test_file_export_within_free_limit_would_attempt_export_and_consume_one_credit()
    print("\nAll paid-template/export gating tests passed.")
