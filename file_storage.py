"""Файловое FSM-хранилище для aiogram — переживает перезапуск процесса
без внешних зависимостей (Redis и т.п.).

Раньше использовался MemoryStorage — весь контекст диалога (org_info,
выбранные идеи, донор, бюджет и т.д.) хранился только в оперативной
памяти процесса и полностью терялся при каждом перезапуске бота (а во
время разработки/тестирования бот перезапускается часто) — пользователю
приходилось каждый раз заново присылать всю информацию с нуля.

Простое решение: каждое состояние/данные конкретного чата сохраняются в
отдельный JSON-файл на диске сразу при изменении — при следующем запуске
процесса они читаются обратно. Не требует Redis/БД, работает "из коробки"
на любой машине."""

import json
import os
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey


class FileStorage(BaseStorage):
    def __init__(self, path: str = ".fsm_storage"):
        self.dir = path
        os.makedirs(self.dir, exist_ok=True)

    def _file(self, key: StorageKey) -> str:
        safe = f"{key.bot_id}_{key.chat_id}_{key.user_id}"
        return os.path.join(self.dir, f"{safe}.json")

    def _read(self, key: StorageKey) -> dict:
        path = self._file(key)
        if not os.path.exists(path):
            return {"state": None, "data": {}}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"state": None, "data": {}}

    def _write(self, key: StorageKey, record: dict) -> None:
        path = self._file(key)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False)
        os.replace(tmp, path)

    async def set_state(self, key: StorageKey, state: State | str | None = None) -> None:
        record = self._read(key)
        record["state"] = state.state if isinstance(state, State) else state
        self._write(key, record)

    async def get_state(self, key: StorageKey) -> str | None:
        return self._read(key).get("state")

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        record = self._read(key)
        record["data"] = data
        self._write(key, record)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        return self._read(key).get("data", {})

    async def close(self) -> None:
        pass
