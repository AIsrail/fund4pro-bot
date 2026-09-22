"""Reads text out of documents the user attaches in Telegram.

Handles the real fix for a recurring UX bug: earlier code only stored the
*file name* of an attached document (org_info.py, donor_info.py manual
upload path) and never its content — so downstream LLM calls saw a bare
filename like "[файл: profile.docx]" with zero actual information, and
would honestly (but confusingly for the user) complain "не хватает
содержимого" while generating ideas, several steps later where the
complaint made no sense to the user who'd already uploaded the file.

Supported: .docx, .xlsx, .pdf, .pptx. Legacy binary .doc/.xls/.ppt formats
have no lightweight pure-Python reader (would need LibreOffice/antiword) —
callers should tell the user immediately (right after upload, not several
steps later) that the old binary format can't be read and ask for a
re-export as .docx/.pdf.
"""

import io
import re

MAX_CHARS = 8000  # keep a single attached doc from blowing the LLM context

SUPPORTED_EXTENSIONS = (".docx", ".xlsx", ".pdf", ".pptx")
LEGACY_UNSUPPORTED_EXTENSIONS = (".doc", ".xls", ".ppt")


class UnsupportedFormatError(Exception):
    """Raised for a recognized-but-unreadable format (legacy binary Office)."""


async def extract_text_from_telegram_file(bot, document) -> str:
    """Downloads a Telegram `document` (aiogram types.Document) and extracts
    its text. Raises UnsupportedFormatError for legacy .doc/.xls/.ppt.
    Returns "" if the format is unknown or extraction fails for another
    reason (corrupt file, scanned/image-only PDF, etc.) — callers should
    treat empty text as "couldn't read this one" and say so to the user
    right away, not silently pretend the file was empty content.
    """
    filename = (document.file_name or "").lower()
    file = await bot.get_file(document.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, destination=buf)
    content = buf.getvalue()

    if filename.endswith(LEGACY_UNSUPPORTED_EXTENSIONS):
        raise UnsupportedFormatError(
            f"Формат {filename.rsplit('.', 1)[-1]} (старый бинарный Office) "
            "не читается напрямую. Пересохрани файл в .docx/.xlsx/.pdf/.pptx "
            "и пришли заново."
        )

    try:
        if filename.endswith(".docx"):
            return _extract_docx(content)
        if filename.endswith(".xlsx"):
            return _extract_xlsx(content)
        if filename.endswith(".pdf"):
            return _extract_pdf(content)
        if filename.endswith(".pptx"):
            return _extract_pptx(content)
    except Exception:
        return ""
    return ""


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_WEBSITE_RE = re.compile(r"(?:https?://)?(?:www\.)?[\w-]+\.[a-zA-Z]{2,}(?:/[^\s,;]*)?")
_PHONE_RE = re.compile(r"(?:\+?\d[\d\s\-()]{6,}\d)")

# Метка в профиле организации -> наше поле. Ищем построчно "Метка: значение"
# — почти все реальные профили организаций (как и этот) оформлены именно
# так, это надёжнее генеративного пересказа моделью.
_LABEL_MAP = {
    "org_email": ("e-mail", "email", "эл. почта", "электронная почта", "почта"),
    "org_website": ("веб-сайт", "вебсайт", "сайт", "website"),
    "org_phone": ("телефон", "тел.", "тел:", "phone"),
    "org_address_legal": ("юридический адрес",),
    "org_address_actual": ("фактический адрес",),
    "org_address": ("адрес",),
    "org_founded": ("дата основания", "создана", "основана"),
}


def extract_contact_facts(text: str) -> dict[str, str]:
    """Детерминированно (регэкспами/по меткам, БЕЗ модели) вытаскивает
    контактные факты организации из текста профиля/приложенного файла.

    РЕАЛЬНАЯ ЖАЛОБА: "абсолютное большинство доноров просят контактные
    данные, бот должен заранее выносить их в отдельный data-room по
    организации, а не полагаться на то, что перескажет модель". Раньше
    текст файла шёл прямо в чат единым куском, и то, попадут ли телефон/
    email/сайт в org_info, зависело от того, не срежет ли их модель при
    свободном пересказе в update_project — реальный инцидент: телефон/email/
    сайт/адрес пропали из готовой заявки, хотя были в приложенном профиле.
    Эта функция — надёжная "картотека" сбоку от свободного текста: сначала
    построчный поиск по меткам ("Веб-сайт: ...", "E-mail: ...", это
    надёжнее, точно берёт то же значение, что в файле), затем общий
    regex-проход по ВСЕМУ тексту как подстраховка для файлов без меток."""
    facts: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        label = label.strip().lower()
        value = value.strip().strip("/").strip()
        if not value:
            continue
        for field, markers in _LABEL_MAP.items():
            if field in facts:
                continue
            # Подстрокой, не только точным совпадением/суффиксом — метки
            # склоняются ("Телефоны:" вместо "Телефон:", "E-mail:" сам по
            # себе уже подстрока в разных написаниях).
            if any(m in label for m in markers):
                facts[field] = value

    if "org_email" not in facts:
        m = _EMAIL_RE.search(text)
        if m:
            facts["org_email"] = m.group(0)
    if "org_phone" not in facts:
        m = _PHONE_RE.search(text)
        if m and sum(ch.isdigit() for ch in m.group(0)) >= 7:
            facts["org_phone"] = m.group(0).strip()
    if "org_website" not in facts:
        for m in _WEBSITE_RE.finditer(text):
            candidate = m.group(0)
            # Не путаем e-mail-домен или дату (12.03.2025) с сайтом.
            if "@" in candidate or facts.get("org_email", "") in candidate:
                continue
            if re.match(r"^\d", candidate):
                continue
            if candidate.count(".") >= 1 and any(c.isalpha() for c in candidate):
                facts["org_website"] = candidate
                break
    return facts


def _extract_docx(content: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(content))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text.strip():
                    parts.append(cell.text.strip())
    return "\n".join(parts).strip()[:MAX_CHARS]


def _extract_xlsx(content: bytes) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    parts = []
    for sheet in wb.worksheets:
        parts.append(f"[Лист: {sheet.title}]")
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts).strip()[:MAX_CHARS]


def _extract_pdf(content: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    parts = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(parts).strip()[:MAX_CHARS]


def _extract_pptx(content: bytes) -> str:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(content))
    parts = []
    for i, slide in enumerate(prs.slides, start=1):
        slide_parts = []
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                slide_parts.append(shape.text_frame.text.strip())
        if slide_parts:
            parts.append(f"[Слайд {i}]\n" + "\n".join(slide_parts))
    return "\n".join(parts).strip()[:MAX_CHARS]
