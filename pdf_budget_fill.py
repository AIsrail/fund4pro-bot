"""Заполнение бюджетных полей в PDF-форме донора (заполняемый AcroForm).

Переиспользует чтение/запись полей из pdf_form_fill. Плоский PDF (скан, без
полей) заполнить программно нельзя — extract_pdf_form_schema вернёт пусто.
"""

import json
import logging

from pdf_form_fill import (
    PdfFieldSpec,
    _has_non_latin,
    _snap_to_option,
    extract_pdf_form_schema,
    fill_pdf_form,
)

logger = logging.getLogger("fund4pro.pdf_budget_fill")

MAX_FIELDS_IN_PROMPT = 300


def describe_fields(fields: list[PdfFieldSpec]) -> str:
    lines = []
    for f in fields[:MAX_FIELDS_IN_PROMPT]:
        opts = f" варианты={f.options}" if f.options else ""
        lines.append(f"{f.field_id} [{f.kind}] {f.name[:100]}{opts}")
    return "\n".join(lines)


async def map_budget_to_pdf_fields(fields: list[PdfFieldSpec], budget_text: str) -> dict[str, str]:
    from llm import call_claude

    system_prompt = (
        "Перед тобой список полей PDF-формы донора (id, тип, имя поля — это и есть "
        "подпись) и согласованный бюджет проекта. Заполни ТОЛЬКО бюджетные поля "
        "(статьи расходов, количества, ставки, суммы, итоги, админ-расходы); "
        "поля об организации и проекте не трогай. Не придумывай суммы, которых "
        "нет в бюджете; нет соответствия — пропусти поле. Итоги рассчитай "
        "аккуратно. Шрифт PDF-формы обычно не поддерживает кириллицу, поэтому "
        "названия статей пиши по-английски, числа — цифрами без символов валют и "
        "разделителей тысяч. Для полей с вариантами выбери ровно один вариант.\n\n"
        'Верни СТРОГО JSON без пояснений: {"p12": "Project Manager", "p13": "12000"}'
    )
    try:
        raw = await call_claude(system_prompt, f"Поля формы:\n{describe_fields(fields)}\n\nБюджет:\n{budget_text}", max_tokens=4000)
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start:end + 1])
    except Exception as exc:
        logger.warning("map_budget_to_pdf_fields failed: %s: %s", type(exc).__name__, exc)
        return {}
    answers = {str(k): str(v) for k, v in data.items() if v is not None and str(v).strip()} if isinstance(data, dict) else {}
    by_id = {f.field_id: f for f in fields}
    for fid, val in list(answers.items()):
        spec = by_id.get(fid)
        if spec is None:
            del answers[fid]
        elif spec.kind == "choice" and spec.options:
            snapped = _snap_to_option(val, spec.options)
            if snapped:
                answers[fid] = snapped
            else:
                del answers[fid]
    return answers


def fill_pdf_budget(content: bytes, fields: list[PdfFieldSpec], answers: dict[str, str]):
    """(файл, число_полей, есть_ли_кириллица_в_ответах)."""
    new_bytes, filled = fill_pdf_form(content, fields, answers)
    return new_bytes, filled, bool(_has_non_latin(answers))


__all__ = ["extract_pdf_form_schema", "map_budget_to_pdf_fields", "fill_pdf_budget", "describe_fields"]
