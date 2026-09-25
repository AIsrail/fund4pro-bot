"""Регрессионный тест для docx_schema_fill._lock_table_layout.

ИЗВЕСТНАЯ ЛОВУШКА python-docx (задокументирована и подтверждена на реальных
заявках донора, не гипотеза): вложенные таблицы плана/бюджета в форме
донора обычно имеют автоматическую ширину колонок (tblW type="auto"), пока
в них 1-2 строки-шаблона. agent'ы, заполняющие такие таблицы, клонируют
строку-шаблон под каждую реальную строку ответа (см. _fill_table_field) —
и как только строк становится много (план на 10+ пунктов, бюджет на 20+
позиций), Word при ОТКРЫТИИ файла пересчитывает ширину столбцов по
содержимому текста и может увести таблицу за правое поле страницы/ячейки.
Число таблиц/строк/ячеек при этом не меняется — НИКАКАЯ структурная
проверка (включая уже существующие тесты этого репозитория) это не ловит,
только открыть готовый файл глазами. _lock_table_layout переключает
таблицу на fixed layout с явной шириной колонок сразу после роста строк,
чтобы Word не пересчитывал геометрию заново.

    python -m tests.test_table_layout_lock
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import docx
from docx.oxml.ns import qn


def test_lock_table_layout_switches_to_fixed_with_matching_column_widths():
    from docx_schema_fill import _lock_table_layout

    doc = docx.Document()
    table = doc.add_table(rows=5, cols=3)  # уже "выросшая" таблица (5 строк вместо 1-2 у шаблона)

    tblPr = table._tbl.tblPr
    assert tblPr.find(qn("w:tblLayout")) is None, "sanity: python-docx defaults to no explicit layout (auto)"

    grid = table._tbl.find(qn("w:tblGrid"))
    original_widths = [int(c.get(qn("w:w"))) for c in grid.findall(qn("w:gridCol"))]
    assert len(original_widths) == 3 and all(w > 0 for w in original_widths)

    _lock_table_layout(table)

    layout_el = tblPr.find(qn("w:tblLayout"))
    assert layout_el is not None, "tblLayout must be set after locking"
    assert layout_el.get(qn("w:type")) == "fixed", "layout must be switched to fixed, not left auto"

    for row in table.rows:
        tcs = row._tr.findall(qn("w:tc"))
        assert len(tcs) == 3
        for i, tc in enumerate(tcs):
            tcPr = tc.find(qn("w:tcPr"))
            assert tcPr is not None, "every cell must get an explicit tcPr/tcW after locking"
            w_el = tcPr.find(qn("w:tcW"))
            assert w_el is not None
            assert w_el.get(qn("w:type")) == "dxa"
            assert int(w_el.get(qn("w:w"))) == original_widths[i], (
                "cell width must match the table's own original column layout, "
                "not an arbitrary/guessed value"
            )

    print("OK: _lock_table_layout switches auto-width tables to fixed with matching explicit cell widths")


def test_lock_table_layout_is_a_noop_on_missing_grid():
    """Не должно падать, если у таблицы почему-то нет tblGrid/tblPr —
    защита от зацикливания важнее, чем гарантированный фикс геометрии."""
    from docx_schema_fill import _lock_table_layout

    doc = docx.Document()
    table = doc.add_table(rows=1, cols=1)
    grid = table._tbl.find(qn("w:tblGrid"))
    if grid is not None:
        grid.getparent().remove(grid)

    _lock_table_layout(table)  # must not raise
    print("OK: _lock_table_layout does not crash when tblGrid is missing")


if __name__ == "__main__":
    test_lock_table_layout_switches_to_fixed_with_matching_column_widths()
    test_lock_table_layout_is_a_noop_on_missing_grid()
    print("\nAll table-layout-lock tests passed.")
