"""Юнит-тесты для billing.py — учёт лимита бесплатных проектов + платных
кредитов (Telegram Stars). Redis не поднимаем: billing._read/_write
подменяются на in-memory словарь, так что проверяется именно бизнес-логика
(can_start_project/consume_project_start/add_paid_credit), а не транспорт.

    python -m tests.test_billing
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import billing
import config


def _patch_fake_store():
    """Подменяет billing._read/_write на in-memory dict и возвращает его
    (для инспекции, если понадобится) вместе с оригиналами для восстановления."""
    store = {}
    orig_read, orig_write = billing._read, billing._write

    async def fake_read(chat_id):
        return dict(store.get(chat_id, {"free_used": 0, "paid_credits": 0}))

    async def fake_write(chat_id, record):
        store[chat_id] = dict(record)

    billing._read, billing._write = fake_read, fake_write
    return store, (orig_read, orig_write)


def test_free_limit_blocks_after_n_projects():
    _patch_fake_store()
    config.ENFORCE_FULL_VERSION_LIMIT = True
    config.FULL_VERSION_LIMIT = 3
    config.UNLIMITED_USER_IDS = set()
    chat_id = 111

    async def run():
        for _ in range(3):
            assert await billing.can_start_project(chat_id) is True
            await billing.consume_project_start(chat_id)
        assert await billing.can_start_project(chat_id) is False, (
            "4-я попытка должна блокироваться при FULL_VERSION_LIMIT=3"
        )

    asyncio.run(run())
    print("OK: бесплатный лимит блокирует ровно после FULL_VERSION_LIMIT проектов")


def test_paid_credit_unblocks_and_is_consumed_once():
    _patch_fake_store()
    config.ENFORCE_FULL_VERSION_LIMIT = True
    config.FULL_VERSION_LIMIT = 1
    config.UNLIMITED_USER_IDS = set()
    chat_id = 222

    async def run():
        await billing.consume_project_start(chat_id)  # съедает единственную бесплатную попытку
        assert await billing.can_start_project(chat_id) is False

        await billing.add_paid_credit(chat_id, 1)
        assert await billing.can_start_project(chat_id) is True
        await billing.consume_project_start(chat_id)
        assert await billing.can_start_project(chat_id) is False, (
            "платный кредит должен закрывать ровно один дополнительный проект"
        )

    asyncio.run(run())
    print("OK: платный кредит снимает блокировку ровно на один проект")


def test_unlimited_user_never_blocked_and_never_consumes_counters():
    _patch_fake_store()
    config.ENFORCE_FULL_VERSION_LIMIT = True
    config.FULL_VERSION_LIMIT = 0
    config.UNLIMITED_USER_IDS = {333}
    chat_id = 333

    async def run():
        for _ in range(5):
            assert await billing.can_start_project(chat_id) is True
            await billing.consume_project_start(chat_id)
        record = await billing._read(chat_id)
        assert record == {"free_used": 0, "paid_credits": 0}, (
            "UNLIMITED_USER_IDS не должен тратить счётчики вообще"
        )

    asyncio.run(run())
    print("OK: UNLIMITED_USER_IDS никогда не блокируется и не тратит счётчики")


def test_enforcement_disabled_never_blocks():
    _patch_fake_store()
    config.ENFORCE_FULL_VERSION_LIMIT = False
    config.FULL_VERSION_LIMIT = 0
    config.UNLIMITED_USER_IDS = set()
    chat_id = 444

    async def run():
        assert await billing.can_start_project(chat_id) is True
        await billing.consume_project_start(chat_id)
        assert await billing.can_start_project(chat_id) is True, (
            "при ENFORCE_FULL_VERSION_LIMIT=false лимит никогда не должен блокировать"
        )

    asyncio.run(run())
    print("OK: ENFORCE_FULL_VERSION_LIMIT=false не блокирует независимо от счётчиков")


if __name__ == "__main__":
    test_free_limit_blocks_after_n_projects()
    test_paid_credit_unblocks_and_is_consumed_once()
    test_unlimited_user_never_blocked_and_never_consumes_counters()
    test_enforcement_disabled_never_blocks()
    print("\nAll billing tests passed.")
