"""Заполнение бюджетной таблицы внутри Word-заявки донора.

У небольших доноров бюджет — таблица в самой заявке. Здесь заполняются ТОЛЬКО
пустые ячейки (и подсказки-заглушки вроде «укажите…») таблицы бюджета;
заголовки, остальные таблицы и весь текст заявки не меняются. В Word нет
формул, поэтому итоговые строки («Итого/Total») пересчитываются кодом по
столбцу, а не доверяются модели.
"""

import io
import json
import logging
import re

import docx

from xlsx_patch import _PLACEHOLDER

logger = logging.getLogger("fund4pro.docx_budget_fill")

_BUDGET_WORDS = re.compile(
    r"бюджет|budget|смет|затрат|расход|cost|expens|стоимост|итого|total|сумм|amount|price|цена", re.I)
_TOTAL_LABEL = re.compile(r"^\s*(итого|всего|итог\b|total|subtotal|grand total|сумма по|общая сумма)", re.I)
_ADDR = re.compile(r"^r(\d+)c(\d+)$")
_MONEY = re.compile(r"^[\$€£]?\s*-?[\d\s ,.]+\s*[\$€£]?$")
MAX_ROWS_IN_PROMPT = 90


def _grid(table):
    """grid[r][c] = (cell, is_anchor). Объединённая ячейка: якорь — первая
    встреча в порядке строк/столбцов, остальные — не-якоря (писать нельзя)."""
    seen = []
    out = []
    for row in table.rows:
        line = []
        for cell in row.cells:
            tc = cell._tc
            anchor = not any(tc is s for s in seen)
            if anchor:
                seen.append(tc)
            line.append((cell, anchor))
        out.append(line)
    return out


def _text(cell) -> str:
    return cell.text.strip().replace("\n", " ")


def _looks_budget(table, lenient: bool) -> bool:
    rows = len(table.rows)
    cols = len(table.columns)
    if rows < (3 if lenient else 5) or cols < 2:
        return False
    texts = [" ".join(_text(c) for c in r.cells) for r in table.rows]
    if lenient:
        return True
    head = " ".join(texts[:2])
    body = " ".join(texts)
    return bool(_BUDGET_WORDS.search(head) or (_BUDGET_WORDS.search(body) and len(re.findall(r"\d{3,}", body)) >= 2))


def find_budget_tables(content: bytes, lenient: bool = False) -> list[int]:
    try:
        document = docx.Document(io.BytesIO(content))
    except Exception:
        return []
    return [i for i, t in enumerate(document.tables) if _looks_budget(t, lenient)]


def render_tables(content: bytes, indices: list[int]) -> str:
    """Сетка таблиц для LLM: адрес ячейки rNcM (строка, столбец с 0), ∅ — пусто
    (можно писать), ▒ — внутри объединённой, текст — подписи донора."""
    document = docx.Document(io.BytesIO(content))
    lines = []
    for ti in indices:
        table = document.tables[ti]
        grid = _grid(table)
        lines.append(f"[Таблица T{ti}: строк {len(grid)}, столбцов {len(table.columns)}]")
        for r, row in enumerate(grid[:MAX_ROWS_IN_PROMPT]):
            parts = []
            for c, (cell, anchor) in enumerate(row):
                if not anchor:
                    parts.append(f"r{r}c{c}=▒")
                else:
                    t = _text(cell)
                    parts.append(f"r{r}c{c}={t[:50]!r}" if t else f"r{r}c{c}=∅")
            lines.append(" | ".join(parts))
    return "\n".join(lines)


async def map_budget_to_tables(grid_text: str, budget_text: str) -> dict:
    from llm import call_claude

    system_prompt = (
        "Перед тобой таблицы Word-заявки донора (по строкам; у каждой ячейки адрес "
        "rNcM: N — строка, M — столбец, оба с нуля) и согласованный с пользователем "
        "бюджет. Знаки: ∅ — пустая ячейка (сюда можно писать), ▒ — внутри "
        "объединённой (не трогать), текст в кавычках — заголовки/подписи донора "
        "(не менять; подсказки вроде «укажите…» можно заменить).\n\n"
        "Найди таблицу бюджета и распиши бюджет по её строкам: определи по шапке "
        "смысл столбцов (статья, количество, ставка, период, сумма, комментарий), "
        "впиши по одной статье бюджета на строку подряд. Итоговые суммы строк и "
        "блоков («Итого») тоже впиши — рассчитай аккуратно. Не придумывай статьи "
        "и суммы: только то, что есть в согласованном бюджете; строки таблицы, "
        "которым нет соответствия, оставь пустыми. Если пустых строк меньше, чем "
        "статей, объедини мелкие статьи, не выходя за таблицу. Числа — без "
        "валютных символов и разделителей тысяч, десятичная точка. Не уверен — "
        "пропусти ячейку.\n\n"
        'Верни СТРОГО JSON без пояснений: {"T0": {"r3c1": "Руководитель проекта", "r3c4": 12000}}'
    )
    try:
        raw = await call_claude(system_prompt, f"Таблицы заявки:\n{grid_text}\n\nСогласованный бюджет:\n{budget_text}", max_tokens=4000)
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start:end + 1])
    except Exception as exc:
        logger.warning("map_budget_to_tables failed: %s: %s", type(exc).__name__, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _num(text: str):
    t = (text or "").strip()
    if not t or not _MONEY.match(t):
        return None
    t = re.sub(r"[\$€£\s ]", "", t)
    t = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", t).replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def _fmt(x: float) -> str:
    return str(int(round(x))) if abs(x - round(x)) < 0.005 else f"{x:.2f}"


def _write(cell, text: str) -> None:
    text = str(text).replace("\n", " ").strip()
    paragraphs = cell.paragraphs
    first = paragraphs[0]
    if first.runs:
        first.runs[0].text = text
        for run in first.runs[1:]:
            run.text = ""
    else:
        first.add_run(text)
    for p in paragraphs[1:]:
        for run in p.runs:
            run.text = ""


def fill_docx_budget(content: bytes, mapping: dict):
    """Возвращает (файл, записанные ['T0!r3c1'], пропущенные [(адрес, причина)],
    заменённые_подсказки, пересчитанные_итоги ['T0!r9c4: 5200 -> 5100'])."""
    document = docx.Document(io.BytesIO(content))
    tables = document.tables
    applied, skipped, replaced, fixed = [], [], [], []
    written: dict[tuple[int, int, int], float | None] = {}
    totals_by_table: dict[int, list[int]] = {}

    for tkey, cells in mapping.items():
        digits = re.sub(r"\D", "", str(tkey))
        if not digits or not isinstance(cells, dict) or int(digits) >= len(tables):
            skipped.append((str(tkey), "таблица не найдена"))
            continue
        ti = int(digits)
        grid = _grid(tables[ti])
        for addr, value in cells.items():
            label = f"T{ti}!{addr}"
            m = _ADDR.match(str(addr).strip().lower())
            if not m or value is None or str(value).strip() == "":
                continue
            r, c = int(m.group(1)), int(m.group(2))
            if r >= len(grid) or c >= len(grid[r]):
                skipped.append((label, "вне таблицы"))
                continue
            cell, anchor = grid[r][c]
            if not anchor:
                skipped.append((label, "часть объединённой ячейки"))
                continue
            existing = _text(cell)
            was_placeholder = False
            if existing:
                if not _PLACEHOLDER.match(existing):
                    skipped.append((label, "ячейка уже заполнена текстом донора"))
                    continue
                was_placeholder = True
            _write(cell, value)
            applied.append(label)
            if was_placeholder:
                replaced.append(label)
            written[(ti, r, c)] = _num(str(value))

    # Итоги: пересчёт по столбцу, только для ячеек итоговых строк, куда писала модель.
    for ti in sorted({k[0] for k in written}):
        grid = _grid(tables[ti])
        total_rows = [r for r, row in enumerate(grid)
                      if any(_TOTAL_LABEL.match(_text(cell)) for cell, anchor in row if anchor)]
        for r in total_rows:
            for c in range(len(grid[r])):
                if (ti, r, c) not in written or written[(ti, r, c)] is None:
                    continue
                total = 0.0
                for rr in range(r):
                    if rr in total_rows:
                        continue
                    cell, anchor = grid[rr][c]
                    if anchor and (v := _num(_text(cell))) is not None and rr > 0:
                        total += v
                llm_value = written[(ti, r, c)]
                if total > 0 and abs(total - llm_value) > max(0.5, 0.005 * total):
                    _write(grid[r][c][0], _fmt(total))
                    fixed.append(f"T{ti}!r{r}c{c}: {_fmt(llm_value)} -> {_fmt(total)}")

    out = io.BytesIO()
    document.save(out)
    return out.getvalue(), applied, skipped, replaced, fixed
