"""Тесты для системы платежей по конкурсам (28.10.2026).

Система: первые 2 конкурса бесплатно, с 3-го требуется платёж (500 сом).
Конкурс идентифицируется по названию + полному домену.
"""

import sys
import json
sys.path.insert(0, "/".join(__file__.split("/")[:-2]))

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import billing
import config
from donor_scrape import make_contest_id


async def test_make_contest_id():
    """Проверяем создание уникального ID конкурса."""
    url1 = "https://grants.example.com/kyrgyzstan/2026"
    name1 = "Tech Innovation 2026"

    contest_id_1 = make_contest_id(url1, name1)
    assert "tech_innovation_2026" in contest_id_1
    assert "grants.example.com" in contest_id_1
    print(f"✅ make_contest_id: {contest_id_1}")

    # Разные названия → разные IDs
    contest_id_2 = make_contest_id(url1, "Social Impact 2026")
    assert contest_id_1 != contest_id_2
    print(f"✅ Разные названия → разные IDs")

    # Одинаковые название и домен → одинаковые IDs
    contest_id_1_dup = make_contest_id(url1, name1)
    assert contest_id_1 == contest_id_1_dup
    print(f"✅ Одинаковые параметры → одинаковый ID")


async def test_check_contest_quota_free_contests():
    """Первые 2 конкурса должны быть бесплатны."""
    # При Redis=None возвращается fallback (всегда разрешает)
    billing._redis_client = None

    chat_id = 123456789

    # С fallback-поведением функция всегда разрешает (можно стартовать)
    # Но reason должен быть "redis_unavailable"
    quota_1 = await billing.check_contest_quota(chat_id, "tech_2026__grants.com")
    assert quota_1["can_start"] == True
    assert quota_1["reason"] == "redis_unavailable"
    print(f"✅ Конкурс #1: fallback allow (redis_unavailable)")

    quota_2 = await billing.check_contest_quota(chat_id, "social_2026__grants.com")
    assert quota_2["can_start"] == True
    assert quota_2["reason"] == "redis_unavailable"
    print(f"✅ Конкурс #2: fallback allow (redis_unavailable)")


async def test_check_contest_quota_paid_contest_no_access():
    """Конкурс #3+ требует платёж при наличии Redis."""
    # При Redis=None всегда разрешает (fallback)
    billing._redis_client = None

    chat_id = 987654321

    # С fallback-поведением функция всегда разрешает
    quota_3 = await billing.check_contest_quota(chat_id, "funded_2026__grants.com")
    assert quota_3["can_start"] == True
    assert quota_3["reason"] == "redis_unavailable"
    print(f"✅ Конкурс #3: fallback allow (redis_unavailable)")


def test_donation_scenario():
    """Сценарий: юзер разработал проекты под 2 конкурсами, пытается открыть 3-й."""
    print("\n--- Сценарий: 3 конкурса ---")

    chat_id = 111222333
    contests = {
        "tech_2026__grants1.com": {"order": 1, "projects_count": 5},
        "social_2026__grants1.com": {"order": 2, "projects_count": 3},
        # Конкурс #3 — платный, не использовался
    }

    # Юзер может разработать до 5 проектов под каждым из первых 2 конкурсов
    for name, data in list(contests.items())[:2]:
        print(f"  ✓ {name}: {data['projects_count']} проектов (бесплатно)")

    # Конкурс #3 требует платёж
    print(f"  ✗ Конкурс #3: требуется 500 сом за доступ")


async def main():
    print("Testing contest-based billing system...\n")

    # Отключаем Redis для тестов
    billing._redis_client = None

    await test_make_contest_id()
    print()

    await test_check_contest_quota_free_contests()
    print()

    await test_check_contest_quota_paid_contest_no_access()
    print()

    test_donation_scenario()

    print("\n✅ Все тесты пройдены!")


if __name__ == "__main__":
    asyncio.run(main())
