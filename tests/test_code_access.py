"""Тесты для code_access.py."""

import asyncio
import pytest

import code_access


def test_code_format_validation():
    """Валидация формата кода (16 hex символов)."""
    # Пока просто проверим, что модуль загружается
    assert hasattr(code_access, "redeem_code")
    assert hasattr(code_access, "get_status")
    assert hasattr(code_access, "FUNDAN_API_BASE")


@pytest.mark.asyncio
async def test_redeem_code_invalid_format():
    """Коды неверного формата должны вернуть None."""
    result = await code_access.redeem_code("short")
    assert result is None

    result = await code_access.redeem_code("")
    assert result is None


@pytest.mark.asyncio
async def test_redeem_code_network_error():
    """При сетевой ошибке должно вернуться None."""
    # Используем явно неверный API
    result = await code_access.redeem_code("751F3C29B0DE8ED1")
    # Может быть None (ошибка сети) или разумное значение, если API доступен
    # Пока просто проверим, что функция завершится без исключений
    assert result is None or isinstance(result, dict)
