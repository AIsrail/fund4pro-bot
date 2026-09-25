"""Регрессионный тест для chat_serialization.ChatSerializationMiddleware.

РЕАЛЬНАЯ ЖАЛОБА (25.09.2026): 3 файла об организации подряд -> 3 ОДИНАКОВЫХ
ответа, и бот параллельно успел ответить на присланного следом донора —
несколько РАЗНЫХ апдейтов одного чата обрабатывались одновременно (aiogram
по умолчанию не сериализует апдейты внутри чата), каждый читал сессию до
того, как другой её обновил.

    python -m tests.test_chat_serialization
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeMessage:
    def __init__(self, chat_id):
        self.chat = _FakeChat(chat_id)


class _FakeUpdate:
    def __init__(self, chat_id):
        self.message = _FakeMessage(chat_id)
        self.edited_message = None
        self.callback_query = None


def test_updates_from_the_same_chat_never_overlap():
    from chat_serialization import ChatSerializationMiddleware

    mw = ChatSerializationMiddleware()
    active = {"count": 0}
    max_overlap = {"seen": 0}

    async def slow_handler(event, data):
        active["count"] += 1
        max_overlap["seen"] = max(max_overlap["seen"], active["count"])
        await asyncio.sleep(0.05)
        active["count"] -= 1
        return "ok"

    async def run():
        updates = [_FakeUpdate(chat_id=111) for _ in range(3)]
        await asyncio.gather(*(mw(slow_handler, u, {}) for u in updates))

    asyncio.run(run())
    assert max_overlap["seen"] == 1, (
        f"three updates from the same chat must be processed strictly one at a time, "
        f"but {max_overlap['seen']} ran concurrently at once"
    )
    print("OK: three concurrent updates from the same chat never overlap")


def test_updates_from_different_chats_still_run_concurrently():
    from chat_serialization import ChatSerializationMiddleware

    mw = ChatSerializationMiddleware()
    active = {"count": 0}
    max_overlap = {"seen": 0}

    async def slow_handler(event, data):
        active["count"] += 1
        max_overlap["seen"] = max(max_overlap["seen"], active["count"])
        await asyncio.sleep(0.05)
        active["count"] -= 1
        return "ok"

    async def run():
        updates = [_FakeUpdate(chat_id=i) for i in range(5)]
        await asyncio.gather(*(mw(slow_handler, u, {}) for u in updates))

    asyncio.run(run())
    assert max_overlap["seen"] > 1, (
        "updates from DIFFERENT chats must still run concurrently — "
        "serialization must be per-chat, not a single global lock"
    )
    print(f"OK: updates from different chats run concurrently (peak overlap: {max_overlap['seen']})")


if __name__ == "__main__":
    test_updates_from_the_same_chat_never_overlap()
    test_updates_from_different_chats_still_run_concurrently()
    print("\nAll chat-serialization tests passed.")
