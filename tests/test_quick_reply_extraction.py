"""Регрессионный тест для agent_engine._extract_options_from_reply —
эвристика, которая сама придумывает кнопки-подсказки, когда модель не
вызвала suggest_quick_replies. Чистая функция, LLM не задействована.

РЕАЛЬНАЯ ЖАЛОБА (скриншот в чате с владельцем): бот прислал уже
заполненный блок профиля организации "для копирования" (с нумерацией ПОЛЕЙ
ДОНОРСКОЙ ФОРМЫ вроде "16) Является ли кто-либо из сотрудников выборным
должностным лицом...") и в конце того же сообщения — короткую просьбу
написать "дальше". Эвристика сканировала ВЕСЬ текст и превратила нумерацию
скопированного контента в кнопки, никак не связанные с реальным вопросом.

    python -m tests.test_quick_reply_extraction
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_engine import _extract_options_from_reply


def test_numbered_content_block_before_continue_prompt_is_not_a_menu():
    reply = (
        "Вот заполненный блок профиля организации (для копирования в форму донора):\n\n"
        "14) Полное юридическое название организации: ОО «Ромашка»\n"
        "15) Дата регистрации: 12.03.2018\n"
        "16) Является ли кто-либо из сотрудников выборным должностным лицом "
        "или связан с государственными органами: Нет\n\n"
        "Если всё это ок, в следующем сообщении дам следующий блок профиля "
        "(миссия, основные виды деятельности, источники финансирования, "
        "сильные стороны) в таком же формате для копирования. Напишите "
        "коротко: «дальше» — и продолжу.\n\n"
        "👇 Нажмите кнопку ниже или отправьте цифру в ответ:"
    )
    opts = _extract_options_from_reply(reply)
    assert opts == [], (
        f"нумерация из скопированного блока полей формы не должна превращаться "
        f"в кнопки — получили {opts!r}"
    )
    print("OK: numbered form-field content earlier in the message is not mistaken for a menu")


def test_genuine_short_menu_near_the_end_still_extracted():
    """Не перечиним: настоящее меню сразу перед '👇 Нажмите кнопку' по-прежнему
    должно извлекаться (см. agent_roadmap.py UX-паттерн)."""
    reply = (
        "Какой донор вам интереснее?\n\n"
        "1. ГГФ\n"
        "2. NED\n"
        "3. Другой донор\n\n"
        "👇 Нажмите кнопку ниже или отправьте цифру в ответ:"
    )
    opts = _extract_options_from_reply(reply)
    assert len(opts) == 3, f"настоящее меню из 3 пунктов должно распознаваться, получили {opts!r}"
    assert opts[0].startswith("1."), opts
    print("OK: a genuine short menu right before the boilerplate is still extracted")


def test_genuine_menu_survives_even_with_earlier_unrelated_numbered_recap():
    """Меню в последнем абзаце распознаётся, даже если РАНЬШЕ в сообщении
    была своя (не связанная) нумерация."""
    reply = (
        "Уже заполнено:\n\n"
        "16) Является ли кто-либо из сотрудников выборным должностным лицом: Нет\n\n"
        "Какой бюджет закладываем?\n\n"
        "1. До 5000 USD\n"
        "2. 5000-15000 USD\n\n"
        "👇 Нажмите кнопку ниже или отправьте цифру в ответ:"
    )
    opts = _extract_options_from_reply(reply)
    assert len(opts) == 2, f"меню в последнем абзаце должно распознаваться, получили {opts!r}"
    print("OK: a genuine menu in the tail paragraph survives an unrelated numbered block earlier")


def test_choice_phrased_as_convenience_question_without_question_mark():
    """РЕАЛЬНАЯ ЖАЛОБА: "Напишите, пожалуйста, что вам удобнее: - ... - ..."
    — явный выбор из 2 буллетов, но без "?" и без старых choice-маркеров —
    раньше гейт отсеивал сообщение до парсинга буллетов."""
    reply = (
        "Напишите, пожалуйста, что вам удобнее:\n"
        "- «Предложи варианты сам» — и я распишу 3-4 конкретных направления "
        "с обоснованиями под NED;\n"
        "- или кратко своим текстом: в чём вы видите проблему/идею "
        "(2-3 предложения), а я уже переведу это в язык NED и «3 деревьев»."
    )
    opts = _extract_options_from_reply(reply)
    assert len(opts) == 2, f"выбор без '?' должен распознаваться, получили {opts!r}"
    print("OK: a convenience-phrased choice without a question mark is still extracted")


if __name__ == "__main__":
    test_numbered_content_block_before_continue_prompt_is_not_a_menu()
    test_genuine_short_menu_near_the_end_still_extracted()
    test_genuine_menu_survives_even_with_earlier_unrelated_numbered_recap()
    test_choice_phrased_as_convenience_question_without_question_mark()
    print("\nAll quick-reply-extraction tests passed.")
