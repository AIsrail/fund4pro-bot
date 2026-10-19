"""Подсветка XYZ-плейсхолдеров красным в генерируемых .docx.

РЕАЛЬНАЯ ЖАЛОБА: правило плейсхолдеров (см. llm.PLACEHOLDER_RULE) и так
уже заставляет модель ставить XYZ на место недостающих цифр/дат вместо
того чтобы выдумывать их — но в готовом .docx они тонут в сплошном чёрном
тексте, и пользователь легко пропускает часть при финальной вычитке перед
подачей. Простое решение — красить сами вхождения 'XYZ' в текст красным и
жирным прямо на уровне run'ов, не трогая остальное форматирование."""

import re

from docx.enum.text import WD_COLOR_INDEX
from docx.shared import RGBColor

_XYZ_RE = re.compile(r"XYZ")
_APPLICANT_COMMENT_RE = re.compile(r"\[КОММЕНТАРИЙ ДЛЯ ЗАЯВИТЕЛЯ[^\]]*\]")
XYZ_COLOR = RGBColor(0xFF, 0x00, 0x00)  # яркий красный — заметнее прежнего тёмного 0xCC0000


def add_text_xyz_highlighted(
    paragraph,
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    font_name: str | None = None,
    font_size=None,
) -> None:
    """Добавляет text в paragraph как один или несколько runs, окрашивая
    каждое вхождение 'XYZ' в красный жирный — остальные атрибуты
    (bold/italic/шрифт) применяются к не-XYZ фрагментам как обычно, XYZ
    всегда выделяется поверх них (жирный + красный), чтобы точно бросался
    в глаза независимо от того, был ли фрагмент уже жирным/курсивным.

    Пометки для заявителя «[КОММЕНТАРИЙ ДЛЯ ЗАЯВИТЕЛЯ — НЕ ДЛЯ ДОНОРА: ...]»
    (см. quality_rules.py) выделяются жёлтым фоном — их нужно удалить перед
    отправкой донору; внутри них XYZ отдельно не красится."""
    last = 0
    found_comment = False
    for cm in _APPLICANT_COMMENT_RE.finditer(text):
        found_comment = True
        if cm.start() > last:
            _add_xyz_segments(paragraph, text[last:cm.start()], bold, italic, font_name, font_size)
        run = paragraph.add_run(cm.group(0))
        run.italic = True
        run.font.highlight_color = WD_COLOR_INDEX.YELLOW
        if font_name:
            run.font.name = font_name
        if font_size:
            run.font.size = font_size
        last = cm.end()
    if found_comment:
        if last < len(text):
            _add_xyz_segments(paragraph, text[last:], bold, italic, font_name, font_size)
        return
    _add_xyz_segments(paragraph, text, bold, italic, font_name, font_size)


def _add_xyz_segments(paragraph, text: str, bold: bool, italic: bool, font_name, font_size) -> None:
    pos = 0
    for m in _XYZ_RE.finditer(text):
        if m.start() > pos:
            _add_plain_run(paragraph, text[pos:m.start()], bold, italic, font_name, font_size)
        run = paragraph.add_run(m.group(0))
        run.bold = True
        run.font.color.rgb = XYZ_COLOR
        if font_name:
            run.font.name = font_name
        if font_size:
            run.font.size = font_size
        pos = m.end()
    if pos < len(text):
        _add_plain_run(paragraph, text[pos:], bold, italic, font_name, font_size)


def _add_plain_run(paragraph, text: str, bold: bool, italic: bool, font_name, font_size) -> None:
    if not text:
        return
    run = paragraph.add_run(text)
    run.bold = bold
    run.italic = italic
    if font_name:
        run.font.name = font_name
    if font_size:
        run.font.size = font_size
