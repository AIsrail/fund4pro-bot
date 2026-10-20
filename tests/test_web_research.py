"""Юнит-тесты цепочки веб-поиска (web_research.py): провайдеры подменяются
заглушками, сеть не используется.

    python -m tests.test_web_research
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import web_research as wr


def _patch(**fns):
    wr.PROVIDERS.clear()
    wr.PROVIDERS.update(fns)
    os.environ["SEARCH_PROVIDERS"] = "gemini,openai,deepseek"


def _ret(text, sources=()):
    async def f(q, ctx):
        return text, list(sources)
    return f


def _boom():
    async def f(q, ctx):
        raise RuntimeError("api down")
    return f


def test_first_provider_wins_and_claude_not_in_default_chain():
    _patch(gemini=_ret("Туристов 100 тыс., stat.gov.kg, 2024", [{"title": "t", "url": "u"}]),
           openai=_ret("другое"), deepseek=_ret("ещё"), claude=_ret("claude"))
    text, src, name = asyncio.run(wr.web_research("q"))
    assert name == "gemini" and "100 тыс." in text and src[0]["url"] == "u"
    assert "claude" not in wr.provider_order()


def test_falls_through_errors_and_no_data_to_next():
    _patch(gemini=_boom(), openai=_ret("Живых данных по этому запросу не нашлось"),
           deepseek=_ret("факт из результатов", [{"title": "a", "url": "b"}]))
    text, src, name = asyncio.run(wr.web_research("q"))
    assert name == "deepseek" and text == "факт из результатов"


def test_all_fail_returns_empty():
    _patch(gemini=_boom(), openai=_ret(""), deepseek=_ret("   "))
    assert asyncio.run(wr.web_research("q")) == ("", [], "")


def test_provider_order_env_override_and_unknown_ignored():
    _patch(gemini=_ret("x"), openai=_ret("x"), deepseek=_ret("x"), claude=_ret("x"))
    os.environ["SEARCH_PROVIDERS"] = "claude, gemini ,bogus"
    assert wr.provider_order() == ["claude", "gemini"]
    os.environ["SEARCH_PROVIDERS"] = "gemini,openai,deepseek"


def test_missing_keys_skip_real_providers_without_network():
    import config
    saved = (config.GEMINI_API_KEY, config.OPENAI_API_KEY)
    config.GEMINI_API_KEY = config.OPENAI_API_KEY = ""
    try:
        assert asyncio.run(wr._search_gemini("q", "")) == ("", [])
        assert asyncio.run(wr._search_openai("q", "")) == ("", [])
    finally:
        config.GEMINI_API_KEY, config.OPENAI_API_KEY = saved


if __name__ == "__main__":
    test_first_provider_wins_and_claude_not_in_default_chain()
    test_falls_through_errors_and_no_data_to_next()
    test_all_fail_returns_empty()
    test_provider_order_env_override_and_unknown_ignored()
    test_missing_keys_skip_real_providers_without_network()
    print("All web-research tests passed.")
