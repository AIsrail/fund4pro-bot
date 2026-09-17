"""Заполнение формы донора через явную СХЕМУ полей + структурированный
батч-вызов модели (JSON по стабильному field_id) — вместо нечёткого
сопоставления текста между отдельно сгенерированным markdown-документом и
ячейками шаблона (см. docx_template_fill.py, оставлен как fallback).

ПОЧЕМУ ПЕРЕДЕЛАНО (архитектурный аудит после серии багов за одну неделю:
заявка не по форме, дублирующиеся ответы в разных полях, потерянные разделы,
XYZ вместо реальных данных): старый пайплайн работал в два физически не
связанных шага — (1) generate_final_document пишет ПОЛНЫЙ текст заявки
свободным markdown, ничего не зная про реальные ячейки шаблона; (2)
docx_template_fill.py пытается угадать, какой кусок этого текста куда
вписать, через нечёткое сравнение текста вопроса с текстом заголовка/метки.
Это и есть источник большинства багов — угадывание неизбежно иногда
промахивается, задваивает совпадения или не находит вообще ничего.

Новый подход убирает угадывание целиком: сначала СЧИТЫВАЕМ реальную
структуру шаблона напрямую (какие ячейки где, с каким вопросом) — это
не угадывание, а точное чтение. Затем ОДНИМ (или несколькими батчами для
длинных форм) структурированным вызовом просим модель ответить по стабильному
field_id на каждый вопрос сразу с учётом всего проекта. Ответ приходит уже
привязанным к конкретной ячейке — записываем напрямую, сопоставлять по
тексту не нужно вообще.

Область применения: только табличная часть шаблона (там же, где раньше
работал fuzzy-matching — все содержательные поля реальных форм доноров на
практике лежат в таблицах). Чекбоксы/флажки вне таблиц (обычные параграфы) —
как и раньше, эта функция их не трогает; известное, некритичное ограничение,
не регрессия относительно старого поведения (там их тоже не было)."""

import logging
from dataclasses import dataclass
from typing import Any

import docx
from docx.shared import Pt

from xyz_highlight import add_text_xyz_highlighted

logger = logging.getLogger("fund4pro.docx_schema_fill")

CHUNK_SIZE = 12  # полей на один структурированный вызов — держит ответ в разумных max_tokens


@dataclass
class FieldSpec:
    field_id: str
    question: str
    kind: str  # "kv" (короткий факт рядом с меткой) | "section" (открытый вопрос в своей ячейке)
    target: Any  # ссылка на docx.table._Cell, куда писать ответ


def extract_template_schema(doc: "docx.Document") -> list[FieldSpec]:
    """Читает РЕАЛЬНУЮ структуру шаблона напрямую из таблиц — тот же обход,
    что раньше использовался только для чтения меток под fuzzy-matching,
    теперь ещё и запоминает прямую ссылку на целевую ячейку для каждого
    поля, так что заполнение потом не требует повторного поиска."""
    fields: list[FieldSpec] = []
    counter = 0

    # Снимаем свойства плавающей таблицы — как в старом пайплайне, иначе
    # Word может неправильно спозиционировать таблицу после правки текста.
    for table in doc.tables:
        tblpPr = table._tbl.tblPr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tblpPr")
        if tblpPr is not None:
            table._tbl.tblPr.remove(tblpPr)

    for table in doc.tables:
        for row in table.rows:
            unique_cells = []
            for c in row.cells:
                if not any(c._tc == u._tc for u in unique_cells):
                    unique_cells.append(c)

            if len(unique_cells) >= 2:
                # Метка -> соседняя ПУСТАЯ ячейка = поле ввода для этой метки
                for j in range(len(unique_cells)):
                    lbl = unique_cells[j].text.strip()
                    if not lbl:
                        continue
                    if j + 1 < len(unique_cells) and not unique_cells[j + 1].text.strip():
                        counter += 1
                        fields.append(FieldSpec(f"f{counter}", lbl, "kv", unique_cells[j + 1]))
            elif len(unique_cells) == 1:
                # Одна ячейка с самим вопросом целиком (открытый, развёрнутый ответ)
                text = unique_cells[0].text.strip()
                if text:
                    counter += 1
                    fields.append(FieldSpec(f"f{counter}", text, "section", unique_cells[0]))

    return fields


async def build_field_answers(fields: list[FieldSpec], session_data: dict) -> dict[str, str]:
    """Отвечает на ВСЕ поля схемы батчами по CHUNK_SIZE — один структурированный
    JSON-вызов на батч (llm.fill_form_fields_batch), не нечёткое сопоставление.
    Передаёт уже полученные ответы в контекст следующего батча, чтобы цифры
    (сумма бюджета, даты) оставались согласованными между батчами."""
    from llm import fill_form_fields_batch

    answers: dict[str, str] = {}
    for i in range(0, len(fields), CHUNK_SIZE):
        chunk = fields[i:i + CHUNK_SIZE]
        chunk_dicts = [{"field_id": f.field_id, "question": f.question, "kind": f.kind} for f in chunk]
        try:
            result = await fill_form_fields_batch(chunk_dicts, session_data, already_answered=answers)
        except Exception:
            logger.warning("build_field_answers: batch %d-%d failed, continuing with rest", i, i + len(chunk), exc_info=True)
            result = {}
        answers.update(result)
    return answers


def _fill_kv_cell(target, val: str) -> None:
    target.text = ""
    p = target.paragraphs[0] if target.paragraphs else target.add_paragraph()
    add_text_xyz_highlighted(p, val, font_name="Times New Roman", font_size=Pt(10.5))


def _append_section_answer(cell, content: str) -> None:
    p = cell.add_paragraph()
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.line_spacing = 1.15
    add_text_xyz_highlighted(p, f"\n{content}", font_name="Times New Roman", font_size=Pt(11))


def write_answers_to_template(fields: list[FieldSpec], answers: dict[str, str]) -> tuple[int, int]:
    """Прямая, детерминированная запись — каждый field.target уже указывает
    ровно на ту ячейку, куда должен попасть его ответ. Сопоставлять по
    тексту не нужно: связь установлена один раз при извлечении схемы."""
    filled_kv = 0
    filled_sections = 0
    for f in fields:
        val = (answers.get(f.field_id) or "").strip()
        if not val:
            continue
        if f.kind == "kv":
            _fill_kv_cell(f.target, val)
            filled_kv += 1
        else:
            if val not in f.target.text:
                _append_section_answer(f.target, val)
                filled_sections += 1
    return filled_kv, filled_sections


async def fill_donor_docx_template_v2(template_path: str, output_path: str, session: dict) -> tuple[bool, str]:
    """Новый основной путь заполнения формы донора. Возвращает
    (успех, текст_для_проверки) — текст_для_проверки — это все реально
    записанные значения полей, склеенные вместе; используется вызывающим
    кодом (agent_engine.py) для того же самого подсчёта XYZ-плейсхолдеров,
    что и раньше, без дублирования этой логики здесь."""
    try:
        doc = docx.Document(template_path)
    except Exception as e:
        logger.warning("fill_donor_docx_template_v2: could not open template %s: %s", template_path, e)
        return False, ""

    fields = extract_template_schema(doc)
    if not fields:
        logger.warning("fill_donor_docx_template_v2: no fillable fields found in %s", template_path)
        return False, ""

    answers = await build_field_answers(fields, session)
    filled_kv, filled_sections = write_answers_to_template(fields, answers)

    logger.info(
        "fill_donor_docx_template_v2: filled %d/%d kv fields and %d sections (of %d total fields) in %s",
        filled_kv, sum(1 for f in fields if f.kind == "kv"), filled_sections, len(fields), template_path,
    )

    # Та же защита, что была в старом пайплайне: форма с солидным числом
    # таблиц, но почти ничем не заполненная — считаем неуспехом, а не тихо
    # сохраняем полупустой файл.
    if len(doc.tables) >= 5 and (filled_kv < 5 or filled_sections < 1):
        logger.warning(
            "fill_donor_docx_template_v2: only %d kv + %d sections filled — rejecting partial fill",
            filled_kv, filled_sections,
        )
        return False, ""

    if filled_kv + filled_sections == 0:
        return False, ""

    doc.save(output_path)
    text_for_check = "\n".join(answers.get(f.field_id, "") for f in fields)
    return True, text_for_check
