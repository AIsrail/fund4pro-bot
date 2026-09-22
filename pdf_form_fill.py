"""Заполнение AcroForm PDF-шаблонов доноров — параллельный docx_schema_fill.py
путь для доноров, публикующих официальную форму заявки как заполняемый PDF,
а не Word-документ с таблицами (например NED — Proposal Form и Organization
Profile Form оба публикуются как .pdf).

РЕАЛЬНЫЙ ИНЦИДЕНТ: у бота вообще не было механизма заполнить PDF-форму —
только читать из неё текст (document_reader._extract_pdf, donor_scrape.py).
Когда донор публикует официальную форму именно в PDF, бот либо предлагал
заполнить как есть (что физически невозможно — правился только .docx), либо
молча откатывался на generate_final_document (свободный текст в markdown ->
.docx — НЕ официальная форма донора вообще, а её имитация текстом).

Область применения: только PDF с реальными AcroForm-полями (сделанные в
Adobe Acrobat/LibreOffice как "заполняемая форма") — у таких
pypdf.PdfReader.get_fields() возвращает непустой словарь с человекочитаемыми
именами полей. Имя поля в PDF УЖЕ и есть вопрос формы (в отличие от
GGF-докса, где вопрос — текст соседней ячейки, а не имя поля) — извлекать
метку эвристикой по соседним ячейкам не нужно, это и проще, и надёжнее.
"Плоские" (нередактируемые, скан/print-only) PDF — эта функция ничего не
может заполнить программно, честно возвращает пустую схему; вызывающий код
должен откатиться на прежний путь (текстовый черновик для ручного переноса),
а не тихо делать вид, что форма заполнена."""

import io
import logging
import re
from dataclasses import dataclass
from typing import Any

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, TextStringObject

logger = logging.getLogger("fund4pro.pdf_form_fill")

CHUNK_SIZE = 12

# Поля, которые физически не предназначены для ответа заявителя: кнопки
# "отправить по email" со встроенным JS-действием (видел в обеих формах NED
# как единственное /Btn-поле "Click here") — их вообще не передаём модели и
# не трогаем при записи, чтобы случайно не задеть встроенный скрипт формы.
_SKIP_FIELD_TYPES = ("/Btn", "/Sig")


@dataclass
class PdfFieldSpec:
    field_id: str          # синтетический id ("p1", "p2", ...) — имена полей
                            # PDF бывают с юникод-пробелами/кавычками внутри,
                            # синтетический id безопаснее использовать как
                            # JSON-ключ в батч-ответе модели, чем сырое имя.
    name: str               # РЕАЛЬНОЕ имя поля в PDF — вопрос формы, и то,
                            # что нужно передать назад в update_page_form_field_values.
    kind: str                # "text" | "choice"
    options: list[str] | None = None  # только для "choice" — точный список допустимых значений


def extract_pdf_form_schema(content: bytes) -> list[PdfFieldSpec]:
    """Читает АКТУАЛЬНУЮ структуру AcroForm напрямую — аналог
    docx_schema_fill.extract_template_schema, но без угадывания: имя поля
    PDF и есть вопрос, questions извлекать эвристикой не нужно."""
    try:
        reader = PdfReader(io.BytesIO(content))
        fields = reader.get_fields() or {}
    except Exception:
        logger.warning("extract_pdf_form_schema: could not parse PDF", exc_info=True)
        return []

    specs: list[PdfFieldSpec] = []
    counter = 0
    for name, f in fields.items():
        ft = str(f.get("/FT") or "")
        if ft in _SKIP_FIELD_TYPES or not ft:
            continue
        counter += 1
        field_id = f"p{counter}"
        if ft == "/Tx":
            specs.append(PdfFieldSpec(field_id, str(name), "text"))
        elif ft == "/Ch":
            opts: list[str] = []
            for o in (f.get("/Opt") or []):
                label = str(o[-1] if isinstance(o, (list, tuple)) else o).strip()
                if label:
                    opts.append(label)
            specs.append(PdfFieldSpec(field_id, str(name), "choice", options=opts or None))
        # Другие типы (/Btn кроме отфильтрованных выше и т.п.) — пропускаем,
        # а не падаем: лучше заполнить меньше полей, чем весь батч целиком.
    return specs


def _snap_to_option(value: str, options: list[str]) -> str:
    """"choice"-поле в PDF принимает СТРОГО одно из перечисленных значений
    (страна из выпадающего списка, "Да"/"Нет" и т.п.) — свободный ответ
    модели ("Кыргызстан") нужно детерминированно сопоставить с точным
    вариантом донора ("Kyrgyz Republic"), иначе pypdf либо молча не впишет
    значение, либо впишет невалидное для этого поля. Возвращает пустую
    строку, если совпадения не нашлось (лучше оставить поле пустым, чем
    вписать в выпадающий список значение, которого там нет)."""
    v = value.strip()
    if not v:
        return ""
    for opt in options:
        if opt.strip().lower() == v.lower():
            return opt
    # Частые расхождения формулировок донора и модели: «Кыргызстан» против
    # «Kyrgyz Republic», «США» против «United States» и т.п. — пробуем
    # подстроку в обе стороны как второй, менее строгий проход.
    for opt in options:
        ol = opt.strip().lower()
        if ol and (ol in v.lower() or v.lower() in ol):
            return opt
    return ""


async def build_pdf_field_answers(fields: list[PdfFieldSpec], session_data: dict) -> dict[str, str]:
    """Батчами по CHUNK_SIZE отвечает на текстовые и choice-поля PDF-формы —
    структурно то же самое, что docx_schema_fill.build_field_answers, только
    без разделения kv/section (в реальных AcroForm-формах доноров узкие
    факты, не абзацы — длинное эссе-описание проекта донор просит отдельным
    прикладываемым документом, не полем PDF-формы, см. модуль-докстринг)."""
    from llm import fill_pdf_fields_batch

    answers: dict[str, str] = {}
    for i in range(0, len(fields), CHUNK_SIZE):
        chunk = fields[i:i + CHUNK_SIZE]
        chunk_dicts = [
            {"field_id": f.field_id, "question": f.name, "kind": f.kind, "options": f.options}
            for f in chunk
        ]
        try:
            result = await fill_pdf_fields_batch(chunk_dicts, session_data, already_answered=answers)
        except Exception:
            logger.warning("build_pdf_field_answers: batch %d-%d failed, continuing with rest", i, i + len(chunk), exc_info=True)
            result = {}
        answers.update(result)

    # Детерминированная подстраховка (тот же приём, что и в docx-пайплайне):
    # для choice-полей ответ модели ОБЯЗАН стать ровно одним из допустимых
    # вариантов, иначе значение лучше не писать вообще, чем писать невалидное.
    by_id = {f.field_id: f for f in fields}
    for fid, val in list(answers.items()):
        spec = by_id.get(fid)
        if spec and spec.kind == "choice" and spec.options:
            snapped = _snap_to_option(val, spec.options)
            if not snapped:
                logger.info("build_pdf_field_answers: %r (%r) has no matching option, leaving blank", val[:60], spec.name)
            answers[fid] = snapped
    return answers


_NON_LATIN_RE = re.compile(r"[^\x00-\x7F]")


def _has_non_latin(answers: dict[str, str]) -> list[str]:
    """РЕАЛЬНАЯ НАХОДКА (проверено на настоящей форме NED): шрифт формы
    (Helvetica, WinAnsi-кодировка без кириллицы) физически не может
    отобразить кириллицу в поле — pypdf при записи такого значения выдаёт
    предупреждение "characters not supported by font encoding", и в
    большинстве PDF-читалок поле показывает нечитаемые символы, хотя
    значение технически записано верно. Промпт просит модель отвечать по-
    английски (см. fill_pdf_fields_batch в llm.py) — это страховка ПОСЛЕ,
    на случай если модель всё равно ответила кириллицей: не блокирует
    запись (лучше нечитаемое значение, чем пустое поле), но явно
    предупреждает вызывающий код, чтобы предупредить пользователя."""
    return [v for v in answers.values() if v and _NON_LATIN_RE.search(v)]


def fill_pdf_form(content: bytes, fields: list[PdfFieldSpec], answers: dict[str, str]) -> tuple[bytes, int]:
    """Пишет ответы в реальные AcroForm-поля и возвращает (новые_байты_pdf,
    число_заполненных_полей). NeedAppearances выставляется явно — иначе
    заполненные pypdf значения в части PDF-читалок отображаются пустыми,
    пока пользователь не кликнет в поле руками (известная особенность
    pypdf/большинства вьюеров, не баг конкретно этого кода)."""
    reader = PdfReader(io.BytesIO(content))
    writer = PdfWriter()
    writer.append(reader)

    by_id = {f.field_id: f for f in fields}
    values_by_name: dict[str, str] = {}
    for fid, val in answers.items():
        spec = by_id.get(fid)
        val = (val or "").strip()
        if not spec or not val:
            continue
        values_by_name[spec.name] = val

    filled = 0
    for page in writer.pages:
        try:
            annots = page.get("/Annots")
        except Exception:
            annots = None
        if not annots:
            continue
        page_field_names = set()
        for annot in annots:
            obj = annot.get_object()
            nm = obj.get("/T")
            if nm and str(nm) in values_by_name:
                page_field_names.add(str(nm))
        if not page_field_names:
            continue
        page_values = {nm: values_by_name[nm] for nm in page_field_names}
        try:
            writer.update_page_form_field_values(page, page_values, auto_regenerate=False)
            filled += len(page_values)
        except Exception:
            logger.warning("fill_pdf_form: update_page_form_field_values failed on a page", exc_info=True)

    try:
        if writer._root_object.get("/AcroForm") is not None:
            writer._root_object["/AcroForm"][NameObject("/NeedAppearances")] = __import__("pypdf").generic.BooleanObject(True)
    except Exception:
        logger.warning("fill_pdf_form: could not set NeedAppearances", exc_info=True)

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), filled


async def fill_donor_pdf_template_v1(template_path: str, output_path: str, session: dict) -> tuple[bool, str]:
    """PDF-аналог docx_schema_fill.fill_donor_docx_template_v2 — тот же
    контракт (успех, текст_для_проверки), чтобы вызывающий код
    (agent_docgen.py) мог выбирать путь по расширению файла, не меняя
    остальную логику (XYZ-подсчёт, red-flags и т.п.)."""
    try:
        with open(template_path, "rb") as fh:
            content = fh.read()
    except Exception as e:
        logger.warning("fill_donor_pdf_template_v1: could not read %s: %s", template_path, e)
        return False, ""

    fields = extract_pdf_form_schema(content)
    if not fields:
        logger.info("fill_donor_pdf_template_v1: no AcroForm fields in %s (flat/scanned PDF?)", template_path)
        return False, ""

    from docx_schema_fill import xyz_ratio  # переиспользуем ту же метрику, не дублируем

    answers = await build_pdf_field_answers(fields, session)
    # Тот же XYZ-порог, что и для docx-пайплайна (llm.PLACEHOLDER_RULE) —
    # отдельного "второго прохода" для PDF пока нет (полей меньше и они
    # короче, чем в докс-формах, риск ниже), но долю всё равно логируем.
    ratio = xyz_ratio(list(answers.values()))
    if ratio > 0:
        logger.info("fill_donor_pdf_template_v1: XYZ share %.0f%% among filled fields", ratio * 100)

    non_latin = _has_non_latin(answers)
    if non_latin:
        logger.warning(
            "fill_donor_pdf_template_v1: %d field(s) still contain non-Latin text despite the "
            "English-answer instruction — shrift формы может не отрисовать это значение: %r",
            len(non_latin), [v[:40] for v in non_latin[:5]],
        )
        session["_pdf_non_latin_warning"] = True

    try:
        new_bytes, filled = fill_pdf_form(content, fields, answers)
    except Exception:
        logger.warning("fill_donor_pdf_template_v1: fill_pdf_form failed", exc_info=True)
        return False, ""

    if filled == 0:
        return False, ""

    with open(output_path, "wb") as fh:
        fh.write(new_bytes)

    logger.info(
        "fill_donor_pdf_template_v1: filled %d/%d fields (of %d total) in %s",
        filled, sum(1 for f in fields if f.kind in ("text", "choice")), len(fields), template_path,
    )
    text_for_check = "\n".join(answers.values())
    return True, text_for_check
