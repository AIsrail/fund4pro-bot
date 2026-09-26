"""Юнит-тесты для billing.py — учёт лимитов бесплатного использования по
НЕСКОЛЬКИМ независимым ресурсам (project/template_download/file_export) +
платных кредитов (Telegram Stars) на каждый из них отдельно. Redis не
поднимаем: billing._read/_write подменяются на in-memory словарь, так что
проверяется именно бизнес-логика (can_use/consume/add_paid_credit), а не
транспорт.

    python -m tests.test_billing
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import billing
import config

# Захватываем ПОДЛИННУЮ billing._read до того, как её подменит первый же
# _patch_fake_store() (тесты в этом файле не восстанавливают оригинал между
# собой — каждый следующий просто патчит заново, этого достаточно для всех
# тестов, КРОМЕ того, что ниже намеренно проверяет саму настоящую _read).
_REAL_READ = billing._read

_EMPTY_RECORD = {
    "project": {"free_used": 0, "paid_credits": 0},
    "template_download": {"free_used": 0, "paid_credits": 0},
    "file_export": {"free_used": 0, "paid_credits": 0},
}


def _patch_fake_store():
    """Подменяет billing._read/_write на in-memory dict и возвращает его
    (для инспекции, если понадобится) вместе с оригиналами для восстановления."""
    store = {}
    orig_read, orig_write = billing._read, billing._write

    async def fake_read(chat_id):
        if chat_id not in store:
            return {k: dict(v) for k, v in _EMPTY_RECORD.items()}
        return {k: dict(v) for k, v in store[chat_id].items()}

    async def fake_write(chat_id, record):
        store[chat_id] = {k: dict(v) for k, v in record.items()}

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

        await billing.add_paid_credit(chat_id, "project", 1)
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
        assert record["project"] == {"free_used": 0, "paid_credits": 0}, (
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


def test_resources_are_independent_of_each_other():
    """РЕАЛЬНАЯ ПРОСЬБА ВЛАДЕЛЬЦА (25.09.2026): исчерпание лимита на
    скачивание шаблона не должно трогать лимит проектов или экспорта в
    файл, и наоборот — три отдельных счётчика, не один общий."""
    _patch_fake_store()
    config.ENFORCE_FULL_VERSION_LIMIT = True
    config.FULL_VERSION_LIMIT = 5
    config.ENFORCE_TEMPLATE_DOWNLOAD_LIMIT = True
    config.FREE_TEMPLATE_DOWNLOADS = 1
    config.ENFORCE_FILE_EXPORT_PAYMENT = True
    config.FREE_FILE_EXPORTS = 0
    config.UNLIMITED_USER_IDS = set()
    chat_id = 555

    async def run():
        # Исчерпываем ТОЛЬКО template_download.
        assert await billing.can_use(chat_id, "template_download") is True
        await billing.consume(chat_id, "template_download")
        assert await billing.can_use(chat_id, "template_download") is False

        # project и file_export не должны быть затронуты этим вообще.
        assert await billing.can_use(chat_id, "project") is True
        assert await billing.can_use(chat_id, "file_export") is False, (
            "file_export с FREE_FILE_EXPORTS=0 должен требовать оплаты с первого раза, "
            "независимо от template_download"
        )

        await billing.add_paid_credit(chat_id, "file_export", 1)
        assert await billing.can_use(chat_id, "file_export") is True
        # А template_download остаётся заблокированным — оплата одного
        # ресурса не должна разблокировать другой.
        assert await billing.can_use(chat_id, "template_download") is False

    asyncio.run(run())
    print("OK: project/template_download/file_export — три независимых счётчика")


def test_real_read_migrates_old_flat_shape():
    """Проверяет РЕАЛЬНУЮ billing._read (не заглушку) на миграции старого
    плоского формата — вызывает redis-клиент напрямую подменённым best-effort
    получением сырых данных."""
    import json

    class _FakeRedis:
        def __init__(self, raw):
            self._raw = raw

        async def get(self, key):
            return self._raw

    orig_get_client = billing._get_client
    orig_read = billing._read
    billing._read = _REAL_READ  # предыдущие тесты в этом файле не восстанавливают оригинал
    billing._get_client = lambda: _FakeRedis(json.dumps({"free_used": 2, "paid_credits": 1}))
    try:
        async def run():
            record = await billing._read(999)
            assert record["project"] == {"free_used": 2, "paid_credits": 1}, (
                f"old flat record must migrate into project bucket, got {record}"
            )
            assert record["template_download"] == {"free_used": 0, "paid_credits": 0}
            assert record["file_export"] == {"free_used": 0, "paid_credits": 0}

        asyncio.run(run())
    finally:
        billing._get_client = orig_get_client
        billing._read = orig_read
    print("OK: real billing._read migrates the old flat {free_used,paid_credits} shape into 'project'")


if __name__ == "__main__":
    test_free_limit_blocks_after_n_projects()
    test_paid_credit_unblocks_and_is_consumed_once()
    test_unlimited_user_never_blocked_and_never_consumes_counters()
    test_enforcement_disabled_never_blocks()
    test_resources_are_independent_of_each_other()
    test_real_read_migrates_old_flat_shape()
    print("\nAll billing tests passed.")
