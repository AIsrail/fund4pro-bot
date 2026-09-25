"""Регрессионный тест для docx_schema_fill.enforce_word_limits.

Найдено при ревью скилла grant-writing (Hermes/CA_AIbot, 25.09.2026):
донор часто задаёт лимит слов прямо в тексте вопроса ("не более 200 слов"),
а fill_donor_docx_template_v2 полностью полагался на то, что МОДЕЛЬ сама
уложится — ноль программной проверки. Тот же класс риска, что уже чинили
в других местах пайплайна (MAX_TOOL_ROUNDS, реестр документов): доверие к
модели вместо кода, особенно рискованно на слабом фолбэк-провайдере.

    python -m tests.test_word_limit_enforcement
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docx_schema_fill import FieldSpec


def _section_field(fid: str, question: str) -> FieldSpec:
    return FieldSpec(fid, question, "section", None)


def test_overlong_answer_gets_compressed_to_the_donors_limit():
    import docx_schema_fill as dsf
    import llm

    long_answer = " ".join(f"слово{i}" for i in range(250))  # заведомо длиннее лимита
    short_answer = " ".join(f"слово{i}" for i in range(90))

    async def fake_call_claude(system_prompt, user_message, history=None, max_tokens=2000, prefer_anthropic=False):
        assert "100" in system_prompt  # лимит из вопроса должен попасть в промпт сжатия
        assert user_message == long_answer
        return short_answer

    fields = [_section_field("f1", "Опишите проект (не более 100 слов)")]
    answers = {"f1": long_answer}

    original = llm.call_claude
    llm.call_claude = fake_call_claude
    try:
        n = asyncio.run(dsf.enforce_word_limits(fields, answers))
    finally:
        llm.call_claude = original

    assert n == 1
    assert answers["f1"] == short_answer
    print("OK: an answer over the donor's stated word limit gets compressed to fit")


def test_answer_within_limit_is_left_untouched_no_llm_call():
    import docx_schema_fill as dsf
    import llm

    called = {"yes": False}

    async def fake_call_claude(*args, **kwargs):
        called["yes"] = True
        return "should not be reached"

    fields = [_section_field("f1", "Опишите проект (не более 200 слов)")]
    short_answer = " ".join(f"слово{i}" for i in range(50))
    answers = {"f1": short_answer}

    original = llm.call_claude
    llm.call_claude = fake_call_claude
    try:
        n = asyncio.run(dsf.enforce_word_limits(fields, answers))
    finally:
        llm.call_claude = original

    assert n == 0
    assert answers["f1"] == short_answer
    assert not called["yes"], "an answer already within the limit must not trigger a compression call"
    print("OK: an answer already within limit is left as-is, no LLM call made")


def test_no_limit_in_question_is_skipped():
    import docx_schema_fill as dsf

    fields = [_section_field("f1", "Опишите проект")]  # без лимита слов
    long_answer = " ".join(f"слово{i}" for i in range(500))
    answers = {"f1": long_answer}

    n = asyncio.run(dsf.enforce_word_limits(fields, answers))
    assert n == 0
    assert answers["f1"] == long_answer
    print("OK: a field with no stated word limit is left alone even if very long")


def test_xyz_placeholders_do_not_count_toward_the_limit():
    from docx_schema_fill import _count_words

    text = "Достигнуто XYZ территорий, XYZ учреждений установлено, XYZ обращений подано"
    # 3 XYZ (не считаются) + "Достигнуто", "территорий", "учреждений", "установлено",
    # "обращений", "подано" = 6 значимых слов
    assert _count_words(text) == 6, f"got {_count_words(text)}"
    print("OK: XYZ placeholders are excluded from the word count")


def test_compression_that_still_overshoots_keeps_the_original():
    import docx_schema_fill as dsf
    import llm

    long_answer = " ".join(f"слово{i}" for i in range(250))
    still_long = " ".join(f"слово{i}" for i in range(200))  # "сжатый" вариант всё равно далеко за лимитом

    async def fake_call_claude(*args, **kwargs):
        return still_long

    fields = [_section_field("f1", "Опишите проект (максимум 100 слов)")]
    answers = {"f1": long_answer}

    original = llm.call_claude
    llm.call_claude = fake_call_claude
    try:
        n = asyncio.run(dsf.enforce_word_limits(fields, answers))
    finally:
        llm.call_claude = original

    assert n == 0, "a compression that still overshoots the limit must not be accepted"
    assert answers["f1"] == long_answer, "the original answer must be kept rather than a still-too-long rewrite"
    print("OK: a failed compression attempt keeps the original answer instead of a still-too-long one")


if __name__ == "__main__":
    test_overlong_answer_gets_compressed_to_the_donors_limit()
    test_answer_within_limit_is_left_untouched_no_llm_call()
    test_no_limit_in_question_is_skipped()
    test_xyz_placeholders_do_not_count_toward_the_limit()
    test_compression_that_still_overshoots_keeps_the_original()
    print("\nAll word-limit-enforcement tests passed.")
