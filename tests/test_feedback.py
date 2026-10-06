"""Юнит-тесты для feedback.py.

    python -m tests.test_feedback
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feedback


def test_format_for_owner_full():
    text = feedback.format_for_owner(
        1, "ivan", {"ideas": "yes", "data": "part", "template": "no", "quality": 4, "suggestions": "больше доноров"}
    )
    assert "@ivan" in text and "Идеи: да" in text and "частично" in text
    assert "Заявка по шаблону: нет" in text and "4/5" in text and "больше доноров" in text


def test_format_for_owner_partial_answers():
    text = feedback.format_for_owner(7, None, {"quality": 2})
    assert "chat 7" in text and "Идеи: —" in text and "2/5" in text and "Предложения" not in text


def test_callback_data_within_telegram_limit():
    for key, _q, opts in feedback.CHOICE_QUESTIONS:
        for code, _l in opts:
            assert len(f"fb:c:{key}:{code}".encode()) <= 64


def test_summarize():
    assert feedback.summarize([]) == "Отзывов пока нет."
    out = feedback.summarize([
        {"ideas": "yes", "quality": 5, "suggestions": "x"}, {"ideas": "no", "quality": 3},
    ])
    assert "Отзывов: 2" in out and "4.00/5" in out and "С предложениями: 1" in out


if __name__ == "__main__":
    test_format_for_owner_full()
    test_format_for_owner_partial_answers()
    test_callback_data_within_telegram_limit()
    test_summarize()
    print("All feedback tests passed.")
