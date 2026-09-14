"""Заполнение оригинального .docx шаблона донора без изменения его структуры,
таблиц, логотипов и стилей оформления."""

import os
import re
import logging
import docx
from docx.shared import Pt

logger = logging.getLogger("fund4pro.docx_template_fill")


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

    # 1. Прямое совпадение заголовка или подстроки — собираем ВСЕ кандидаты и
    # берём самый содержательный (по длине), а не первый попавшийся.
    # РЕАЛЬНЫЙ ИНЦИДЕНТ: вопрос анкеты — длинный текст (например "ПРОЕКТ:
    # Кратко опишите цель, задачи, предполагаемую деятельность..."), и
    # короткий заголовок вроде "Рабочий план реализации проекта" почти
    # всегда оказывается его подстрокой, даже если содержание под ним —
    # заглушка на пару строк, а не развёрнутый ответ. Возврат по первому
    # совпадению (порядок = порядок заголовков в документе) подставлял этот
    # короткий текст вместо настоящего ответа под другим заголовком (типа
    # "Цель и задачи" или "Деятельность и результаты"), который тоже
    # подходил бы под этот вопрос, но проверялся позже и никогда не
    # доходил до дела — самый важный раздел заявки («ПРОЕКТ») оставался
    # фактически незаполненным содержательно.
    candidates = []
    for h, content in sections.items():
        h_clean = clean_str(h)
        if not h_clean or not content:
            continue
        if h_clean in c_clean or c_clean in h_clean:
            candidates.append(content)
    if candidates:
        return max(candidates, key=len)

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
            candidates = []
            for h, content in sections.items():
                h_c = clean_str(h)
                if any(st in h_c for st in section_triggers):
                    candidates.append(content)
            if candidates:
                return max(candidates, key=len)

    # 3. Совпадение первых слов
    w_prompt = c_clean.split()[:3]
    for h, content in sections.items():
        w_h = clean_str(h).split()[:3]
        if len(w_h) >= 2 and len(w_prompt) >= 2 and " ".join(w_h) == " ".join(w_prompt):
            return content

    return None


def _fill_cell(target, val: str) -> None:
    target.text = val
    for p in target.paragraphs:
        for r in p.runs:
            r.font.name = "Times New Roman"
            r.font.size = Pt(10.5)


def _append_section(cell, content: str) -> None:
    p = cell.add_paragraph()
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.line_spacing = 1.15
    run = p.add_run(f"\n{content}")
    run.font.name = "Times New Roman"
    run.font.size = Pt(11)


async def fill_donor_docx_template(template_path: str, markdown_text: str, output_path: str, session: dict | None = None) -> bool:
    """Открывает оригинальный docx-файл донора, находит пустые ячейки после
    соответствующих меток или поля ввода и заполняет их, сохраняя 100%
    оригинальной верстки (таблицы, шрифты, колонтитулы).
    Возвращает True ТОЛЬКО если форма реально заполнена содержанием.

    ДВА ПРОХОДА: первый — дешёвый детерминированный fuzzy-matching по тексту,
    который уже сгенерировала модель (find_best_kv_match/find_best_section_match).
    РЕАЛЬНЫЙ ИНЦИДЕНТ: поля/вопросы формы, для которых matching не нашёл
    совпадения, раньше молча оставались пустыми — 'Финансовое положение',
    'Когда организация была создана', детальный бюджет и т.п. пропадали из
    итогового файла, хотя правило XYZ-плейсхолдеров прямо запрещает
    оставлять недостающее пустым. Второй проход — один батч-запрос к модели
    (llm.fill_missing_donor_fields) ТОЛЬКО по тому, что осталось пустым
    после первого прохода, с явным исключением полей донора/чекбоксов."""
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

    filled_count = 0
    filled_sections_count = 0

    # Проход 1: детерминированный fuzzy-matching. Для всего, что НЕ нашло
    # соответствия, запоминаем метку/вопрос И саму ячейку-мишень — чтобы во
    # втором проходе не пересканировать документ заново, а точечно
    # дозаполнить именно эти ячейки.
    unmatched_kv: list[tuple[str, "docx.table._Cell"]] = []
    unmatched_sections: list[tuple[str, "docx.table._Cell"]] = []

    # РЕАЛЬНЫЙ ИНЦИДЕНТ: после того как "ПРОЕКТ" (цель/задачи) наконец стал
    # заполняться настоящим содержанием, этот же текст стал "лучшим
    # совпадением" (или ответом второго прохода) ещё для нескольких СОВСЕМ
    # других вопросов подряд — чекбоксов категорий благополучателей,
    # косвенных благополучателей, состава команды. Разные ячейки с РАЗНЫМ
    # текстом вопроса не должны получать ОДИН И ТОТ ЖЕ развёрнутый ответ —
    # это почти всегда либо промах fuzzy-matching, либо модель второго
    # прохода "полениласm" и скопировала один ответ на несколько пунктов.
    # Легитимный случай — ТА ЖЕ ячейка/вопрос физически продублирован в
    # документе (например колонки таблицы 13) — это ловится по одинаковому
    # clean_str(question), а не блокируется.
    filled_content_by_question: dict[str, str] = {}

    def _content_conflicts(question_text: str, content: str) -> bool:
        qkey = clean_str(question_text)
        for other_q, other_content in filled_content_by_question.items():
            if other_content == content and other_q != qkey:
                return True
        return False

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
                            _fill_cell(unique_cells[j + 1], val)
                            filled_count += 1
                        else:
                            unmatched_kv.append((lbl, unique_cells[j + 1]))

            # Случай Б: 1-ячеечная таблица с развёрнутым вопросом (Таблицы 5, 6, 7, 8, 9, 10, 12, 13)
            elif len(unique_cells) == 1:
                cell = unique_cells[0]
                matched_content = find_best_section_match(cell.text, sections)

                # РЕАЛЬНЫЙ ИНЦИДЕНТ: для вопроса "ПРОЕКТ: цель, задачи,
                # деятельность, результат" (самый важный раздел заявки)
                # find_best_section_match нашёл короткий заголовок-подстроку
                # ("Рабочий план реализации проекта"), под которым оказалась
                # не сама деятельность, а служебная памятка донора — и это
                # засчиталось как 'заполнено'. Все вопросы этого типа —
                # открытые, развёрнутые (форма явно просит абзац-два), так
                # что совпадение короче разумного порога почти наверняка
                # промах нечёткого сопоставления, а не настоящий ответ —
                # безопаснее отправить такую ячейку на второй проход (LLM
                # с полным контекстом), чем молча принять мусорное совпадение.
                if matched_content and len(matched_content) < 150:
                    logger.warning(
                        "fill_donor_docx_template: отбрасываю подозрительно короткое совпадение (%d симв.) для ячейки %r",
                        len(matched_content), cell.text.strip()[:80],
                    )
                    matched_content = None

                if matched_content and _content_conflicts(cell.text, matched_content):
                    logger.warning(
                        "fill_donor_docx_template: отбрасываю совпадение — этот же текст уже использован для другого вопроса (ячейка %r)",
                        cell.text.strip()[:80],
                    )
                    matched_content = None

                if matched_content and matched_content not in cell.text:
                    _append_section(cell, matched_content)
                    filled_content_by_question[clean_str(cell.text)] = matched_content
                    filled_count += 1
                    filled_sections_count += 1
                elif not matched_content:
                    unmatched_sections.append((cell.text.strip(), cell))

    # Проход 2: один батч-запрос к модели только по тому, что осталось
    # пустым — см. РЕАЛЬНЫЙ ИНЦИДЕНТ в llm.fill_missing_donor_fields.
    if (unmatched_kv or unmatched_sections) and session is not None:
        from llm import fill_missing_donor_fields

        kv_labels = [lbl for lbl, _ in unmatched_kv]
        section_questions = [q for q, _ in unmatched_sections]
        extra_kv, extra_sections = await fill_missing_donor_fields(kv_labels, section_questions, session)

        for lbl, target in unmatched_kv:
            val = extra_kv.get(lbl)
            if val:
                _fill_cell(target, val)
                filled_count += 1

        for q, cell in unmatched_sections:
            content = extra_sections.get(q)
            if content and _content_conflicts(q, content):
                logger.warning(
                    "fill_donor_docx_template: второй проход вернул уже использованный для другого вопроса ответ — отбрасываю (вопрос %r)",
                    q[:80],
                )
                content = None
            if content:
                _append_section(cell, content)
                filled_content_by_question[clean_str(q)] = content
                filled_count += 1
                filled_sections_count += 1

        logger.info(
            "fill_donor_docx_template: second pass answered %d/%d missing kv fields and %d/%d missing sections",
            len(extra_kv), len(unmatched_kv), len(extra_sections), len(unmatched_sections),
        )

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
        doc.save(output_path)
        return True

    return False
