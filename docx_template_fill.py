"""Заполнение оригинального .docx шаблона донора без изменения его структуры,
таблиц, логотипов и стилей оформления."""

import os
import re
import logging
import docx
from docx.shared import Pt

logger = logging.getLogger("fund4pro.docx_template_fill")

_BUDGET_HEADER_KEYWORDS = (
    "статья", "смета", "стоимост", "цена", "сумма", "бюджет", "budget",
    "кол-во", "количество", "ед изм", "единиц",
)
_ACTIVITY_HEADER_KEYWORDS = (
    "мероприят", "деятельност", "срок", "ответствен", "activity",
    "план действ", "рабочий план", "workplan", "timeline",
)


def clean_str(s: str) -> str:
    """Удаляет знаки препинания и нормализует регистр для нечёткого сравнения."""
    return re.sub(r"[^\w\s]", "", s).lower().strip()


def parse_markdown_fields_and_sections(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """Разбивает сгенерированный markdown-документ на:
    1. sections: {заголовок: развёрнутый текст} для больших текстовых блоков и вопросов анкеты
    2. key_values: {ключ: значение} для полей форм и контактных данных
    """
    sections: dict[str, str] = {}
    key_values: dict[str, str] = {}

    def is_kv_line(line_s: str):
        # **Ключ:** Значение или **Ключ**: Значение
        m_bold = re.match(r"^(?:[-*•]\s*)?\*\*([^*]+?):?\*\*\s*:?\s*(.*)$", line_s)
        if m_bold:
            k = m_bold.group(1).strip().rstrip(":")
            v = m_bold.group(2).strip()
            if 2 < len(k) < 80 and v and not k.endswith("?"):
                return k, v
        # - Ключ: Значение или * Ключ: Значение
        m_bullet = re.match(r"^[-*•]\s*([^*:\n]+?)\s*:\s*(.+)$", line_s)
        if m_bullet:
            k = m_bullet.group(1).strip().rstrip(":")
            v = m_bullet.group(2).strip()
            if 2 < len(k) < 80 and v:
                return k, v
        return None

    lines = text.splitlines()
    curr_header = None
    curr_body: list[str] = []

    def save_section(h, body_lines):
        t = "\n".join(body_lines).strip()
        if h and t:
            sections[h] = t

    for line in lines:
        line_s = line.strip()
        if not line_s:
            if curr_body:
                curr_body.append("")
            continue

        h_match = re.match(r"^#{1,4}\s+(.+)$", line_s)
        bold_h_match = re.match(r"^\*\*([^*]+)\*\*:?$", line_s)
        kv = is_kv_line(line_s)

        if h_match:
            save_section(curr_header, curr_body)
            curr_header = h_match.group(1).strip()
            curr_body = []
        elif bold_h_match and (len(bold_h_match.group(1).strip()) > 15 or bold_h_match.group(1).strip().endswith("?")):
            save_section(curr_header, curr_body)
            curr_header = bold_h_match.group(1).strip().rstrip(":- ")
            curr_body = []
        elif kv:
            key_values[kv[0]] = kv[1]
            if curr_header:
                curr_body.append(line)
        else:
            if curr_header:
                curr_body.append(line)

    save_section(curr_header, curr_body)
    return sections, key_values


def find_best_kv_match(label: str, key_values: dict[str, str]) -> str | None:
    """Находит наиболее подходящее значение для метки поля таблицы."""
    clbl = clean_str(label)
    if not clbl:
        return None

    # 1. Точное совпадение
    for k, v in key_values.items():
        if clean_str(k) == clbl:
            return v

    # 2. Подстрока (предпочитаем наиболее длинный совпадающий ключ)
    best_k = None
    best_len = 0
    for k, v in key_values.items():
        ck = clean_str(k)
        if len(ck) >= 3 and (ck in clbl or clbl in ck):
            if len(ck) < 5 or len(clbl) < 5:
                if ck != clbl:
                    continue
            if len(ck) > best_len:
                best_len = len(ck)
                best_k = k

    if best_k:
        return key_values[best_k]

    # 3. Совпадение первых 3 слов
    w_lbl = clbl.split()[:3]
    if len(w_lbl) >= 2:
        for k, v in key_values.items():
            w_k = clean_str(k).split()[:3]
            if len(w_k) >= 2 and " ".join(w_k) == " ".join(w_lbl):
                return v

    return None


def find_best_section_match(cell_text: str, sections: dict[str, str]) -> str | None:
    """Находит развёрнутый ответ для ячейки с вопросом анкеты."""
    c_clean = clean_str(cell_text)
    if not c_clean:
        return None

    # 1. Прямое совпадение заголовка или подстроки
    for h, content in sections.items():
        h_clean = clean_str(h)
        if not h_clean or not content:
            continue
        if h_clean in c_clean or c_clean in h_clean:
            return content

    # 2. Семантическое сопоставление ключевых разделов грантовых форм
    semantic_mappings = [
        (["цель и направления", "цели вашей организации", "какова цель"], ["цель", "направления", "история и цели"]),
        (["достигает своей цели", "результатов удалось добиться", "каким образом ваша", "история и опыт деятельности"], ["достигает", "результат", "история и опыт"]),
        (["сети или коалиции", "любые сети"], ["сети", "коалиции", "партнеры", "партнёры"]),
        (["контекст", "социальноэкологической проблемы", "описание проблемы", "проблемы"], ["контекст", "проблем", "обоснование проблемы"]),
        (["проект кратко", "цель задачи", "предполагаемую деятельность", "ожидаемый результат"], ["проект", "цель и задачи", "деятельность", "план"]),
        (["подробный бюджет", "бюджет проекта", "в долларах сша"], ["бюджет", "budget"]),
        (["косвенными благополучателями", "сообщества или области", "благополучатели"], ["благополучател", "сообществ"]),
        (["руководства и сотрудников", "примут участие"], ["руководств", "сотрудник", "команда"]),
    ]

    for prompt_triggers, section_triggers in semantic_mappings:
        if any(pt in c_clean for pt in prompt_triggers):
            for h, content in sections.items():
                h_c = clean_str(h)
                if any(st in h_c for st in section_triggers):
                    return content

    # 3. Совпадение первых слов
    w_prompt = c_clean.split()[:3]
    for h, content in sections.items():
        w_h = clean_str(h).split()[:3]
        if len(w_h) >= 2 and len(w_prompt) >= 2 and " ".join(w_h) == " ".join(w_prompt):
            return content

    return None


def _parse_markdown_table_rows(text: str) -> list[list[str]]:
    """Извлекает СТРОКИ ДАННЫХ markdown-таблицы (| ячейка | ячейка |), БЕЗ
    строки заголовка таблицы — модель обычно форматирует бюджет/план
    действий именно так. Строка заголовка — это та, что стоит прямо перед
    строкой-разделителем (|---|---|), и она намеренно исключается из
    результата, иначе названия колонок ("Статья расходов", "Мероприятие")
    попадали бы в таблицу донора как будто это первая строка данных."""
    raw_rows: list[list[str]] = []
    header_indices: set[int] = set()
    for line in text.splitlines():
        line_s = line.strip()
        if not (line_s.startswith("|") and line_s.endswith("|") and line_s.count("|") >= 3):
            continue
        cells = [c.strip() for c in line_s.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            if raw_rows:
                header_indices.add(len(raw_rows) - 1)  # строка перед разделителем — заголовок
            continue
        if any(cells):
            raw_rows.append(cells)
    return [r for i, r in enumerate(raw_rows) if i not in header_indices]


def _try_fill_grid_tables(doc, sections: dict[str, str], unfilled_report: list[str]) -> tuple[int, int]:
    """Заполняет многоколоночные грид-таблицы (реальная форма таблицы
    бюджета/плана мероприятий у большинства доноров: № | Статья | Кол-во |
    Сумма, или № | Мероприятие | Срок | Ответственный), которые Случаи А и
    Б ниже структурно не умеют обрабатывать.

    РЕАЛЬНЫЙ БАГ, который эта функция чинит: Случай А заполняет только
    строки вида "метка -> одна пустая ячейка справа в той же строке" —
    в заголовке грид-таблицы ни одна ячейка не пустая (это подписи
    столбцов), а в строках данных ВСЕ ячейки пустые сразу (значит "метки"
    там просто нет и Случай А молча ничего не делает). Случай Б трогает
    только однокелечные таблицы. В результате настоящая таблица бюджета
    или плана действий раньше полностью игнорировалась без единого
    предупреждения — остальные поля формы (контакты, оргданные) при этом
    заполнялись нормально, что и создавало впечатление "пропускает
    разделы Бюджет/План действий" при в целом рабочем боте.

    Возвращает (filled_cells, filled_tables) для учёта в общей статистике
    заполнения; дописывает в unfilled_report человекочитаемые предупреждения
    для случаев, где заполнить с уверенностью не удалось."""
    filled_cells = 0
    filled_tables = 0

    for table in doc.tables:
        rows = table.rows
        if len(rows) < 2:
            continue

        def unique_cells_of(row):
            uniq = []
            for c in row.cells:
                if not any(c._tc == u._tc for u in uniq):
                    uniq.append(c)
            return uniq

        header_unique = unique_cells_of(rows[0])
        if len(header_unique) < 3:
            continue  # не грид — это Случай А или Б, не наша забота
        if any(not c.text.strip() for c in header_unique):
            continue  # в заголовке есть пустая ячейка — больше похоже на Случай А, не трогаем

        header_text = clean_str(" ".join(c.text for c in header_unique))
        is_budget = any(k in header_text for k in _BUDGET_HEADER_KEYWORDS)
        is_activity = any(k in header_text for k in _ACTIVITY_HEADER_KEYWORDS)
        if not (is_budget or is_activity):
            continue
        label = "Бюджет проекта (таблица)" if is_budget else "План действий (таблица)"

        data_rows = rows[1:]
        empty_data_rows = [r for r in data_rows if all(not c.text.strip() for c in r.cells)]
        if not empty_data_rows:
            # Таблица не выглядит как пустой шаблон, ожидающий построчного
            # заполнения (уже что-то есть, либо это не таблица ввода) —
            # не трогаем структуру.
            continue

        triggers = _BUDGET_HEADER_KEYWORDS if is_budget else _ACTIVITY_HEADER_KEYWORDS
        content = None
        for h, body in sections.items():
            if any(t in clean_str(h) for t in triggers):
                content = body
                break
        if content is None:
            content = find_best_section_match(header_text, sections)

        if not content:
            unfilled_report.append(
                f"{label} — в форме есть пустая таблица для этого раздела, но в "
                f"сгенерированном тексте не нашлось подходящего содержания."
            )
            continue

        n_cols = len(header_unique)
        parsed_rows = _parse_markdown_table_rows(content)
        usable_rows = [r for r in parsed_rows if len(r) >= max(2, n_cols - 2)]

        if usable_rows:
            filled_rows = 0
            for target_row, data_row in zip(empty_data_rows, usable_rows):
                target_cells = unique_cells_of(target_row)
                for cell, value in zip(target_cells, data_row):
                    cell.text = str(value)
                    for p in cell.paragraphs:
                        for r in p.runs:
                            r.font.name = "Times New Roman"
                            r.font.size = Pt(10.5)
                filled_cells += len(target_cells)
                filled_rows += 1
            filled_tables += 1
            if len(usable_rows) > len(empty_data_rows):
                unfilled_report.append(
                    f"{label} — в таблице донора {len(empty_data_rows)} пустых строк, а "
                    f"данных получилось {len(usable_rows)}: заполнены только первые "
                    f"{filled_rows}, остальное нужно дописать вручную или попросить "
                    f"бота 'добавь ещё строки в таблицу бюджета/плана действий'."
                )
        else:
            # Не удалось разобрать построчно по столбцам (модель не оформила
            # раздел как markdown-таблицу, либо число колонок совсем не
            # совпадает) — не рискуем угадывать разбивку по столбцам,
            # кладём весь текст цельным блоком в первую пустую строку, чтобы
            # данные хотя бы не потерялись, и честно предупреждаем.
            target_cells = unique_cells_of(empty_data_rows[0])
            if target_cells:
                target_cells[-1].text = content[:2000]
                filled_cells += 1
                filled_tables += 1
            unfilled_report.append(
                f"{label} — не удалось автоматически разложить по столбцам таблицы "
                f"донора: весь текст вставлен одним блоком в первую строку. "
                f"Проверьте и перенесите вручную по нужным колонкам."
            )

    return filled_cells, filled_tables


def fill_donor_docx_template(template_path: str, markdown_text: str, output_path: str, session: dict | None = None) -> bool:
    """Открывает оригинальный docx-файл донора, находит пустые ячейки после
    соответствующих меток или поля ввода и заполняет их, сохраняя 100%
    оригинальной верстки (таблицы, шрифты, колонтитулы).
    Возвращает True ТОЛЬКО если форма реально заполнена содержанием."""
    try:
        doc = docx.Document(template_path)
    except Exception as e:
        logger.warning("Could not open template docx %s: %s", template_path, e)
        return False

    sections, key_values = parse_markdown_fields_and_sections(markdown_text)

    # Дополнительно извлекаем контакты из session['project_data']['org_info'] как страховку
    if session and isinstance(session, dict):
        pdata = session.get("project_data", {})
        org_info = str(pdata.get("org_info", ""))
        if org_info:
            m_org = re.search(r'(?:ОО|ОФ|ОЮЛ|НКО|Ассоциация)?\s*[«\"].+?[»\"]', org_info)
            if m_org and "Полное название организации" not in key_values:
                key_values["Полное название организации"] = m_org.group(0).strip()
            m_dir = re.search(r'Директор\s*:\s*([А-Яа-яA-Za-z\s]+?)(?=[,.;\n]|$)', org_info)
            if m_dir and "Контактное лицо" not in key_values:
                key_values["Контактное лицо"] = m_dir.group(1).strip()
                key_values["Должность"] = "Директор"
            m_mail = re.search(r'([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', org_info)
            if m_mail and "Адрес электронной почты" not in key_values:
                key_values["Адрес электронной почты"] = m_mail.group(1).strip()
            m_phone = re.search(r'(\+?\d[\d\s\-()]{7,}\d)', org_info)
            if m_phone and "Телефон" not in key_values:
                key_values["Телефон"] = m_phone.group(1).strip()
            m_web = re.search(r'(?:https?://|www\.)[a-zA-Z0-9_.-]+', org_info)
            if m_web and "Вебсайт/Социальные сети" not in key_values:
                key_values["Вебсайт/Социальные сети"] = m_web.group(0).strip()
            if "Ош" in org_info and "Город" not in key_values:
                key_values["Город"] = "Ош"
                key_values["Область"] = "Ошская область"
            if ("Кыргыз" in org_info or "КР" in org_info) and "Страна" not in key_values:
                key_values["Страна"] = "Кыргызская Республика"

    # Удаляем свойства плавающей таблицы (tblpPr) из всех таблиц Word
    for table in doc.tables:
        tblpPr = table._tbl.tblPr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tblpPr")
        if tblpPr is not None:
            table._tbl.tblPr.remove(tblpPr)

    unfilled_report: list[str] = []
    filled_count = 0
    filled_sections_count = 0

    # Грид-таблицы (реальная таблица бюджета/плана мероприятий) — ДО
    # основного цикла ниже, потому что Случаи А/Б по конструкции не умеют
    # их трогать (см. docstring _try_fill_grid_tables), так что порядок
    # выполнения между ними не важен, конфликтов не будет.
    grid_filled_cells, grid_filled_tables = _try_fill_grid_tables(doc, sections, unfilled_report)
    filled_count += grid_filled_cells
    filled_sections_count += grid_filled_tables

    for table in doc.tables:
        for row in table.rows:
            # Получаем уникальные ячейки строки (исключаем дубликаты объединённых ячеек)
            unique_cells = []
            for c in row.cells:
                if not any(c._tc == u._tc for u in unique_cells):
                    unique_cells.append(c)

            # Случай А: Сетка метка -> поле ввода (Таблицы 1, 2, 3, 4 и т.д.)
            if len(unique_cells) >= 2:
                for j in range(len(unique_cells)):
                    lbl = unique_cells[j].text.strip()
                    if not lbl:
                        continue
                    # Если следующая ячейка пустая — это поле ввода для данной метки
                    if j + 1 < len(unique_cells) and not unique_cells[j + 1].text.strip():
                        val = find_best_kv_match(lbl, key_values)
                        if val:
                            target = unique_cells[j + 1]
                            target.text = val
                            for p in target.paragraphs:
                                for r in p.runs:
                                    r.font.name = "Times New Roman"
                                    r.font.size = Pt(10.5)
                            filled_count += 1

            # Случай Б: 1-ячеечная таблица с развёрнутым вопросом (Таблицы 5, 6, 7, 8, 9, 10, 12, 13)
            elif len(unique_cells) == 1:
                cell = unique_cells[0]
                matched_content = find_best_section_match(cell.text, sections)

                if matched_content and matched_content not in cell.text:
                    p = cell.add_paragraph()
                    p.paragraph_format.space_before = Pt(6)
                    p.paragraph_format.line_spacing = 1.15
                    run = p.add_run(f"\n{matched_content}")
                    run.font.name = "Times New Roman"
                    run.font.size = Pt(11)
                    filled_count += 1
                    filled_sections_count += 1

    logger.info("fill_donor_docx_template filled %d fields and %d sections in %s",
                filled_count, filled_sections_count, template_path)

    # ЖЁСТКАЯ ПРОВЕРКА КАЧЕСТВА: если шаблон имеет много таблиц (> 5),
    # но заполнено менее 5 полей или 0 развёрнутых секций — считаем, что
    # шаблон НЕ заполнился! Возвращаем False, чтобы сработал fallback
    # на markdown_to_docx, который гарантированно выдаст ПОЛНЫЙ документ.
    if len(doc.tables) >= 5:
        if filled_count < 5 or filled_sections_count < 1:
            logger.warning("fill_donor_docx_template: only %d fields and %d sections filled in %s — rejecting partial fill, falling back to markdown_to_docx",
                           filled_count, filled_sections_count, template_path)
            return False

    if filled_count > 0:
        # РЕАЛЬНЫЙ ИНЦИДЕНТ: разделы "План действий"/"Бюджет проекта"
        # пропадали из готовой заявки без единого слова об этом — форма
        # молча уходила пользователю неполной. Теперь любые случаи, где
        # заполнение прошло неуверенно (грид-таблица не разобралась по
        # столбцам, раздел не нашёл содержания) — фиксируются в сессии,
        # чтобы вызывающий код (agent_router._send_docx) честно предупредил
        # пользователя и спросил недостающее, а не сделал вид, что всё ОК.
        if unfilled_report and session is not None and isinstance(session, dict):
            existing = session.get("_unfilled_donor_sections", [])
            session["_unfilled_donor_sections"] = existing + unfilled_report
        doc.save(output_path)
        return True

    return False
