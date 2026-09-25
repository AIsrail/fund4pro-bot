"""Регрессионный тест для llm.donor_perspective_review — "собеседник-эксперт,
который смотрит на готовый документ глазами донора" (просьба владельца),
смоделировано на Pass 2 ("fresh-eyes reviewer") из скилла grant-writing:
изолированный вызов БЕЗ истории разговора, в котором документ создавался.

    python -m tests.test_donor_perspective_review
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_returns_review_text_on_success():
    import llm

    async def fake_call_claude(system_prompt, user_message, history=None, max_tokens=2000, prefer_anthropic=False):
        assert "донор" in system_prompt.lower()
        assert "Тестовый донор" in user_message
        assert "Тестовый документ" in user_message
        return "1. Не указан источник цифры X.\n2. Нет ответа на вопрос про устойчивость."

    original = llm.call_claude
    llm.call_claude = fake_call_claude
    try:
        result = asyncio.run(llm.donor_perspective_review("Тестовый документ с заявкой", "Тестовый донор"))
        assert "источник" in result.lower()
    finally:
        llm.call_claude = original
    print("OK: donor_perspective_review returns the reviewer's findings on success")


def test_empty_document_short_circuits_without_calling_llm():
    import llm

    called = {"yes": False}

    async def fake_call_claude(*args, **kwargs):
        called["yes"] = True
        return "should not be reached"

    original = llm.call_claude
    llm.call_claude = fake_call_claude
    try:
        result = asyncio.run(llm.donor_perspective_review("   ", "Тестовый донор"))
        assert result == ""
        assert not called["yes"], "an empty document must not trigger an LLM call at all"
    finally:
        llm.call_claude = original
    print("OK: an empty document short-circuits without calling the LLM")


def test_llm_failure_is_swallowed_not_raised():
    import llm

    async def failing_call_claude(*args, **kwargs):
        raise RuntimeError("provider down")

    original = llm.call_claude
    llm.call_claude = failing_call_claude
    try:
        result = asyncio.run(llm.donor_perspective_review("Тестовый документ", "Тестовый донор"))
        assert result == "", "a failed review must degrade to '' (best-effort), never raise or block delivery"
    finally:
        llm.call_claude = original
    print("OK: a failed reviewer call degrades to '' instead of raising — never blocks document delivery")


if __name__ == "__main__":
    test_returns_review_text_on_success()
    test_empty_document_short_circuits_without_calling_llm()
    test_llm_failure_is_swallowed_not_raised()
    print("\nAll donor-perspective-review tests passed.")
