"""Тесты схемы 21.10.2026: бесплатные 2 проекта / 2 бюджета, платный перенос
в шаблон донора, бонус покупателям книги (@c4faq_bot -> ?start=book).

    python -m tests.test_book_and_limits
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import billing
import book_bonus
import config

_store: dict = {}


def _fake_store():
    async def _r(chat_id):
        rec = _store.setdefault(chat_id, {})
        for res in ("project", "template_download", "file_export", "budget_export", "budget_dev"):
            rec.setdefault(res, {"free_used": 0, "paid_credits": 0})
        return rec

    async def _w(chat_id, rec):
        _store[chat_id] = rec

    billing._read, billing._write = _r, _w
    billing._redis_init_failed = True
    billing._redis_client = None
    _store.clear()
    book_bonus._memory_granted.clear()


def test_defaults_match_owner_scheme():
    assert config.FULL_VERSION_LIMIT == 2 and config.FREE_BUDGET_DEVS == 2
    assert config.FREE_FILE_EXPORTS == 0 and config.FREE_BUDGET_EXPORTS == 0
    assert config.PROJECT_DEV_PRICE_KGS == 200 and config.BUDGET_DEV_PRICE_KGS == 200
    assert config.BUDGET_EXPORT_PRICE_KGS == 200 and config.PRICE_USD == 2


def test_budget_dev_two_free_then_paid():
    _fake_store()
    config.PAYMENT_ENABLED = True
    config.ENFORCE_BUDGET_DEV_LIMIT = True
    run = asyncio.run
    for _ in range(2):
        assert run(billing.can_use(1, "budget_dev"))
        run(billing.consume(1, "budget_dev"))
    assert not run(billing.can_use(1, "budget_dev"))
    run(billing.add_paid_credit(1, "budget_dev"))
    assert run(billing.can_use(1, "budget_dev"))
    run(billing.consume(1, "budget_dev"))
    assert not run(billing.can_use(1, "budget_dev"))


def test_file_transfer_paid_from_first_time():
    _fake_store()
    config.PAYMENT_ENABLED = True
    config.ENFORCE_FILE_EXPORT_PAYMENT = True
    assert not asyncio.run(billing.can_use(2, "file_export"))


def test_book_bonus_gives_free_transfers_once():
    _fake_store()
    config.PAYMENT_ENABLED = True
    config.ENFORCE_FILE_EXPORT_PAYMENT = True
    run = asyncio.run
    assert run(book_bonus.grant_book_bonus(3)) is True
    assert run(book_bonus.grant_book_bonus(3)) is False  # повторно не выдаётся
    for _ in range(config.BOOK_FREE_TRANSFERS):
        assert run(billing.can_use(3, "file_export"))
        run(billing.consume(3, "file_export"))
    assert not run(billing.can_use(3, "file_export"))
    # бюджетный перенос бонусом не покрывается
    assert not run(billing.can_use(3, "budget_export"))


if __name__ == "__main__":
    test_defaults_match_owner_scheme()
    test_budget_dev_two_free_then_paid()
    test_file_transfer_paid_from_first_time()
    test_book_bonus_gives_free_transfers_once()
    print("All book-and-limits tests passed.")
