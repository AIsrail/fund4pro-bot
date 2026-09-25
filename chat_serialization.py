"""Сериализация обработки апдейтов ОДНОГО чата.

РЕАЛЬНАЯ ЖАЛОБА (25.09.2026): прислали 3 файла об организации подряд — бот
дал 3 ОДИНАКОВЫХ ответа про организацию, и пока показывал первый из них,
уже успел ответить на донора (ссылку), присланного следом. "У него
появляется 3 думающих мозга — одного достаточно."

Причина — не баг конкретного хендлера, а архитектура aiogram по умолчанию.
Dispatcher._polling запускает обработку КАЖДОГО апдейта как отдельную
asyncio-задачу (handle_as_tasks=True) — специально, чтобы медленный хендлер
одного апдейта не блокировал приём следующих апдейтов ОТ ДРУГИХ чатов. Но
для апдейтов ОДНОГО И ТОГО ЖЕ чата это не защита, а гонка: если пользователь
шлёт 3 сообщения быстрее, чем бот успевает ответить на первое (а один ход
с LLM занимает секунды-минуты), все 3 обрабатываются ПАРАЛЛЕЛЬНО — каждая
задача читает FSM-сессию (project_data) в момент СВОЕГО старта, не видя
правок, которые сделают (или уже делают) две другие, независимо зовёт LLM
и независимо же пишет обратно. Результат ровно такой, как в жалобе:
дублирующиеся ответы, ответ не на то сообщение, победитель гонки записи
затирает работу остальных.

Фикс — asyncio.Lock на chat_id: апдейты ОДНОГО чата ждут друг друга и
обрабатываются строго по очереди в том порядке, в котором их доставил
Telegram (к моменту старта второго апдейта первый уже полностью завершён,
включая ответ пользователю и запись сессии — гонки не остаётся физически).
Апдейты РАЗНЫХ чатов по-прежнему идут параллельно — throughput бота на
разных пользователях не страдает, лочится только то, что и должно
обрабатываться последовательно."""

import asyncio
from collections import defaultdict

from aiogram import BaseMiddleware
from aiogram.types import Update


def _extract_chat_id(update: Update) -> int | None:
    if update.message:
        return update.message.chat.id
    if update.edited_message:
        return update.edited_message.chat.id
    if update.callback_query and update.callback_query.message:
        return update.callback_query.message.chat.id
    return None


class ChatSerializationMiddleware(BaseMiddleware):
    def __init__(self):
        # defaultdict создаёт Lock лениво по первому обращению к chat_id;
        # локи не освобождаются из словаря (число уникальных чатов на этом
        # боте — единицы-десятки, не источник утечки памяти на практике).
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def __call__(self, handler, event: Update, data: dict):
        chat_id = _extract_chat_id(event)
        if chat_id is None:
            # Апдейт без привязки к чату (например, poll-апдейт) — сериализовать нечего.
            return await handler(event, data)
        async with self._locks[chat_id]:
            return await handler(event, data)
