"""Защита от повторной обработки одного и того же Telegram-апдейта.

РЕАЛЬНЫЙ ИНЦИДЕНТ: при загрузке файла organизации бот отправил два
независимых, несовместимых ответа подряд (~10 сек друг за другом) на одно
сообщение пользователя — второй ответ игнорировал уже распознанный файл и
переспрашивал информацию заново. Видимого повторного действия пользователя
не было. Наиболее вероятная причина — Telegram (или сама инфраструктура,
например рестарт процесса на Render посреди обработки) повторно доставила
тот же update, и `receive_document`/`run_agent_turn` отработали дважды
параллельно, каждый раз читая/записывая FSM-сессию (state.get_data() /
state.set_data()) без защиты от гонки — результат второго вызова затирал
первый.

Это НЕ чинит саму гонку за session-данными (для этого нужна была бы
атомарная блокировка на chat_id, что при MemoryStorage/FileStorage не
тривиально), но убирает наиболее вероятный триггер — сам факт повторной
обработки одного и того же update_id. aiogram при polling обычно не
доставляет один update дважды в рамках одного процесса, но не даёт на это
жёсткой гарантии при рестарте/сбое между подтверждением получения апдейта и
завершением его обработки — именно такое окно и нужно закрыть здесь.

Простой bounded in-memory сет последних N update_id — этого достаточно,
чтобы поймать повтор в течение сессии процесса (не переживает рестарт, но
рестарт — как раз редкий смежный сценарий, а не частый повтор одного и того
же апдейта много раз подряд)."""

import logging
from collections import deque

from aiogram import BaseMiddleware
from aiogram.types import Update

logger = logging.getLogger("fund4pro.update_dedup")

MAX_TRACKED = 2000


class DedupMiddleware(BaseMiddleware):
    def __init__(self):
        self._seen_order: deque[int] = deque(maxlen=MAX_TRACKED)
        self._seen_set: set[int] = set()

    async def __call__(self, handler, event: Update, data: dict):
        update_id = event.update_id
        if update_id in self._seen_set:
            logger.warning(
                "Пропущен повторно доставленный update_id=%s (уже обработан в этом процессе)",
                update_id,
            )
            return None

        if len(self._seen_order) == MAX_TRACKED:
            # deque вот-вот вытеснит самый старый id (maxlen) — убираем его
            # из set тем же ходом, иначе set растёт безгранично на весь срок
            # жизни процесса, а deque молча остаётся источником правды.
            evicted = self._seen_order[0]
            self._seen_set.discard(evicted)
        self._seen_set.add(update_id)
        self._seen_order.append(update_id)

        return await handler(event, data)
