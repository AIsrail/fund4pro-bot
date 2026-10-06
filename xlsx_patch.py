"""Заполнение Excel-шаблона донора БЕЗ пересборки файла.

openpyxl при save() переписывает весь пакет (теряет часть картинок, фигур,
расширений условного форматирования, кэшированные значения формул). Донор
же требует, чтобы его шаблон остался как есть — разрешено только вписать
значения. Поэтому здесь правится ТОЛЬКО XML нужных листов внутри zip: в
пустые ячейки дописывается значение, стиль ячейки (атрибут s) сохраняется,
все остальные части пакета копируются байт-в-байт. Единственное изменение
вне ячеек — флаг fullCalcOnLoad в workbook.xml, чтобы Excel пересчитал
итоговые формулы при открытии.

Ячейки с формулой, с уже заданным текстом/числом (кроме числового 0-
заглушки) и внутренние части объединённых диапазонов не перезаписываются —
возвращаются в списке skipped с причиной.
"""

import io
import logging
import re
import zipfile

from lxml import etree
from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter

logger = logging.getLogger("fund4pro.xlsx_patch")

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
_COORD = re.compile(r"^([A-Za-z]{1,3})(\d{1,7})$")
_NUM = re.compile(r"^-?\d+(?:\.\d+)?$")
# Подсказки-заглушки в шаблонах доноров: их заполняющий и должен заменить.
_PLACEHOLDER = re.compile(
    r"^\s*(укажите|введите|впишите|выберите|заполните|specify|enter|insert|select|please|"
    r"пример|example|xyz|\[.*\]|<.*>|\(.*\))", re.IGNORECASE)


def _q(tag: str) -> str:
    return f"{{{_NS}}}{tag}"


def _sheet_parts(zf: zipfile.ZipFile) -> dict[str, str]:
    """{имя листа: путь к xml внутри zip}."""
    wb = etree.fromstring(zf.read("xl/workbook.xml"))
    rels = etree.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    targets = {r.get("Id"): r.get("Target") for r in rels.iter(f"{{{_NS_PKG}}}Relationship")}
    parts = {}
    for sh in wb.iter(_q("sheet")):
        target = targets.get(sh.get(f"{{{_NS_R}}}id"))
        if not target:
            continue
        path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
        parts[sh.get("name")] = path
    return parts


def _merged_inner(root) -> set[tuple[int, int]]:
    """(row, col) всех ячеек объединённых диапазонов, КРОМЕ левой верхней."""
    inner = set()
    for mc in root.iter(_q("mergeCell")):
        ref = mc.get("ref", "")
        if ":" not in ref:
            continue
        a, b = ref.split(":")
        ma, mb = _COORD.match(a), _COORD.match(b)
        if not ma or not mb:
            continue
        c1, r1 = column_index_from_string(ma.group(1).upper()), int(ma.group(2))
        c2, r2 = column_index_from_string(mb.group(1).upper()), int(mb.group(2))
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                if (r, c) != (r1, c1):
                    inner.add((r, c))
    return inner


def _to_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    s = str(value).strip().replace(" ", "").replace(" ", "")
    s = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", s).replace(",", ".")
    if _NUM.match(s):
        f = float(s)
        return int(f) if f.is_integer() and "." not in s else f
    return None


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        root = etree.fromstring(zf.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return ["".join(si.itertext()) for si in root.iter(_q("si"))]


def _find_row(sheet_data, row_num: int):
    """Строка <row r=N> или None; плюс элемент, ПЕРЕД которым вставлять новую."""
    before = None
    for row in sheet_data.iterfind(_q("row")):
        r = int(row.get("r", "0"))
        if r == row_num:
            return row, None
        if r > row_num and before is None:
            before = row
    return None, before


def _set_cell(sheet_data, coord: str, value, merged_inner, shared) -> tuple[str | None, bool]:
    """(причина_пропуска | None, заменён_ли_пример/подсказка)."""
    replaced = False
    m = _COORD.match(coord.strip())
    if not m:
        return "некорректный адрес ячейки", False
    col_letters, row_num = m.group(1).upper(), int(m.group(2))
    col_num = column_index_from_string(col_letters)
    if (row_num, col_num) in merged_inner:
        return "часть объединённой ячейки", False
    ref = f"{col_letters}{row_num}"

    row, before = _find_row(sheet_data, row_num)
    if row is None:
        row = etree.Element(_q("row"))
        row.set("r", str(row_num))
        if before is not None:
            before.addprevious(row)
        else:
            sheet_data.append(row)

    cell, after = None, None
    for c in row.iterfind(_q("c")):
        cm = _COORD.match(c.get("r", ""))
        if not cm:
            continue
        cn = column_index_from_string(cm.group(1).upper())
        if cn == col_num:
            cell = c
            break
        if cn > col_num and after is None:
            after = c
    if cell is None:
        cell = etree.Element(_q("c"))
        cell.set("r", ref)
        if after is not None:
            after.addprevious(cell)
        else:
            row.append(cell)
    else:
        if cell.find(_q("f")) is not None:
            return "в ячейке формула", False
        v_el = cell.find(_q("v"))
        is_el = cell.find(_q("is"))
        t = cell.get("t")
        existing = ""
        if t == "s" and v_el is not None and (v_el.text or "").strip().isdigit():
            idx = int(v_el.text)
            existing = shared[idx] if idx < len(shared) else "?"
        elif is_el is not None:
            existing = "".join(is_el.itertext())
        elif t in ("str", "b", "e"):
            existing = (v_el.text or "") if v_el is not None else ""
        if existing.strip():
            if not _PLACEHOLDER.match(existing):
                return "ячейка уже заполнена текстом донора", False
            replaced = True
        elif v_el is not None and (v_el.text or "").strip() not in ("", "0"):
            replaced = True  # числовой пример в шаблоне

    for child in list(cell):
        cell.remove(child)
    num = _to_number(value)
    if num is not None:
        if "t" in cell.attrib:
            del cell.attrib["t"]
        v = etree.SubElement(cell, _q("v"))
        v.text = repr(num) if isinstance(num, float) else str(num)
    else:
        cell.set("t", "inlineStr")
        is_el = etree.SubElement(cell, _q("is"))
        tt = etree.SubElement(is_el, _q("t"))
        tt.text = str(value)
        tt.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    if "spans" in row.attrib:
        del row.attrib["spans"]
    return None, replaced


def _force_recalc(data: bytes) -> bytes:
    root = etree.fromstring(data)
    calc = root.find(_q("calcPr"))
    if calc is None:
        calc = etree.Element(_q("calcPr"))
        anchor = None
        for tag in ("definedNames", "externalReferences", "functionGroups", "sheets"):
            anchor = root.find(_q(tag))
            if anchor is not None:
                break
        if anchor is None:
            return data
        anchor.addnext(calc)
    calc.set("fullCalcOnLoad", "1")
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def fill_cells_preserving(content: bytes, values: dict):
    """values: {"Лист": {"C7": 12000, "B7": "Зарплата"}}. Возвращает
    (новый_файл, записанные ["Лист!C7"], пропущенные [("Лист!C8", причина)],
    заменённые_примеры ["Лист!E8"])."""
    zin = zipfile.ZipFile(io.BytesIO(content))
    parts = _sheet_parts(zin)
    shared = _shared_strings(zin)
    applied: list[str] = []
    skipped: list[tuple[str, str]] = []
    replaced_cells: list[str] = []
    new_parts: dict[str, bytes] = {}

    for sheet_name, cells in values.items():
        if not isinstance(cells, dict):
            continue
        actual = sheet_name
        if actual not in parts:
            if len(parts) == 1:
                actual = next(iter(parts))
            else:
                skipped.extend((f"{sheet_name}!{c}", "лист не найден") for c in cells)
                continue
        path = parts[actual]
        root = etree.fromstring(new_parts.get(path) or zin.read(path))
        sheet_data = root.find(_q("sheetData"))
        if sheet_data is None:
            continue
        merged_inner = _merged_inner(root)
        for coord, value in cells.items():
            if value is None or str(value).strip() == "":
                continue
            reason, replaced = _set_cell(sheet_data, str(coord), value, merged_inner, shared)
            label = f"{actual}!{coord}"
            if reason:
                skipped.append((label, reason))
            else:
                applied.append(label)
                if replaced:
                    replaced_cells.append(label)
        new_parts[path] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

    if not applied:
        return content, applied, skipped, replaced_cells

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename in new_parts:
                data = new_parts[item.filename]
            elif item.filename == "xl/workbook.xml":
                data = _force_recalc(data)
            zout.writestr(item, data)
    return out.getvalue(), applied, skipped, replaced_cells


def extract_xlsx_grid(content: bytes, max_chars: int = 14000) -> str:
    """Сетка шаблона для LLM: по строкам, КАЖДАЯ колонка с адресом. ∅ —
    пустая ячейка (сюда можно писать), ▒ — внутри объединённой, '=...' —
    формула (не трогать)."""
    wb = load_workbook(io.BytesIO(content), data_only=False)
    lines: list[str] = []
    total = 0
    for ws in wb.worksheets:
        header = f"[Лист: {ws.title}]"
        lines.append(header)
        total += len(header)
        merged_inner = set()
        for rng in ws.merged_cells.ranges:
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    if (r, c) != (rng.min_row, rng.min_col):
                        merged_inner.add((r, c))
        max_col = min(ws.max_column or 1, 26)
        for row in ws.iter_rows(min_row=1, max_row=ws.max_row or 1, max_col=max_col):
            if not any(c.value is not None and str(c.value).strip() for c in row):
                continue
            parts = []
            for c in row:
                if (c.row, c.column) in merged_inner:
                    parts.append(f"{get_column_letter(c.column)}{c.row}=▒")
                elif c.value is None or not str(c.value).strip():
                    parts.append(f"{get_column_letter(c.column)}{c.row}=∅")
                else:
                    text = str(c.value).replace("\n", " ")
                    parts.append(f"{get_column_letter(c.column)}{c.row}={text[:60]!r}")
            line = " | ".join(parts)
            total += len(line)
            if total > max_chars:
                lines.append("... (шаблон обрезан)")
                return "\n".join(lines)
            lines.append(line)
    return "\n".join(lines)
