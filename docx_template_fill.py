"""Заполнение оригинального .docx шаблона донора без изменения его структуры,
таблиц, логотипов и стилей оформления."""

import os
import re
import logging
import docx
from docx.shared import Pt

logger = logging.getLogger("fund4pro.docx_template_fill")


def parse_markdown_fields_and_sections(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """Разбивает сгенерированный markdown-документ на:
    1. sections: {заголовок: развёрнутый текст} для больших текстовых блоков
    2. key_values: {ключ: значение} для полей форм и контактных данных
    """
    sections: dict[str, str] = {}
    key_values: dict[str, str] = {}
    current_header = None
    current_lines: list[str] = []

    for line in text.split("\n"):
        line_s = line.strip()
        header_match = re.match(r"^#{1,4}\s+(.+)$", line_s)
        if header_match:
            if current_header:
                sections[current_header] = "\n".join(current_lines).strip()
            current_header = header_match.group(1).strip()
            current_lines = []
            continue

        # Парсим ключ-значение вида '- Поле: Значение' или '* Поле: Значение'
        if line_s.startswith(("- ", "* ")):
            raw = line_s[2:].strip()
            if ":" in raw:
                k, _, v = raw.partition(":")
                k = k.strip().strip("*").strip()
                v = v.strip()
                if 2 < len(k) < 100 and v:
                    key_values[k] = v

        if current_header:
            current_lines.append(line)

    if current_header:
        sections[current_header] = "\n".join(current_lines).strip()

    return sections, key_values


def clean_str(s: str) -> str:
    """Удаляет знаки препинания и нормализует регистр для нечёткого сравнения."""
    return re.sub(r"[^\w\s]", "", s).lower().strip()


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
            # Если метка или ключ очень короткие (например "где"), требуем точного совпадения
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


def fill_donor_docx_template(template_path: str, markdown_text: str, output_path: str, session: dict | None = None) -> bool:
    """Открывает оригинальный docx-файл донора, находит пустые ячейки после
    соответствующих меток или поля ввода и заполняет их, сохраняя 100%
    оригинальной верстки (таблицы, шрифты, колонтитулы)."""
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

    # КРИТИЧЕСКИ ВАЖНО: удаляем свойства плавающей таблицы (tblpPr) из всех таблиц!
    # В Word свойство tblpPr заставляет таблицы обтекать друг друга по бокам,
    # что приводит к сжатию соседних таблиц в узкие вертикальные колонки шириной в 1 слово!
    for table in doc.tables:
        tblpPr = table._tbl.tblPr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tblpPr")
        if tblpPr is not None:
            table._tbl.tblPr.remove(tblpPr)

    filled_count = 0

    for table in doc.tables:
        for row in table.rows:
            # Получаем уникальные ячейки строки (исключаем дубликаты объединённых ячеек)
            unique_cells = []
            for c in row.cells:
                if c._tc not in [u._tc for u in unique_cells]:
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

            # Случай Б: 1-ячеечная таблица с развёрнутым вопросом (Таблицы 5, 6, 7, 8, 9, 10, 12)
            elif len(unique_cells) == 1:
                cell = unique_cells[0]
                clean_prompt = clean_str(cell.text)
                matched_content = None

                for h, content in sections.items():
                    ch = clean_str(h)
                    if not ch or not content:
                        continue
                    if ch in clean_prompt or clean_prompt in ch:
                        matched_content = content
                        break
                    hw = ch.split()[:3]
                    pw = clean_prompt.split()[:3]
                    if len(hw) >= 2 and len(pw) >= 2 and " ".join(hw) == " ".join(pw):
                        matched_content = content
                        break

                if matched_content and matched_content not in cell.text:
                    p = cell.add_paragraph()
                    p.paragraph_format.space_before = Pt(6)
                    p.paragraph_format.line_spacing = 1.15
                    run = p.add_run(f"\n{matched_content}")
                    run.font.name = "Times New Roman"
                    run.font.size = Pt(11)
                    filled_count += 1

    logger.info("fill_donor_docx_template filled %d fields/sections in %s", filled_count, template_path)
    if filled_count > 0:
        doc.save(output_path)
        return True

    return False
