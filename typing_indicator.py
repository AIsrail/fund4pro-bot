"""Shared "in progress" indicators for long-running LLM/network calls.

Two complementary signals, used together for maximum visibility:

1. show_typing() — the small "печатает..." indicator under the input field
   (Telegram's native ChatActionSender, auto-refreshed while the block runs).
   Easy to miss — it's subtle and easy to overlook mid-conversation.

2. show_working() — edits the JUST-TAPPED message in place: strips its
   inline keyboard and swaps the text for a "⏳ ..." placeholder, then
   restores/replaces it once the wrapped block finishes. This is the far
   more visible cue, because it directly replaces what the user is looking
   at (the message they just tapped a button on), rather than a small
   indicator elsewhere on screen.

Earlier, before callback.answer() was moved to fire immediately (fix for
"query is too old" errors), Telegram's OWN client showed a brief loading
spinner directly on the tapped button while the callback was unanswered —
that native cue is exactly what disappeared once callback.answer() became
immediate. show_working() replaces that lost affordance with an explicit,
persistent one that doesn't depend on unanswered-callback timing.
"""
import asyncio
import contextlib

from aiogram.utils.chat_action import ChatActionSender


def show_typing(bot, chat_id: int):
    return ChatActionSender.typing(bot=bot, chat_id=chat_id)


@contextlib.asynccontextmanager
async def show_live_progress(message, initial_text: str = "💭 Думаю..."):
    """Создаёт живое сообщение-индикатор в чате прямо перед глазами пользователя.
    
    Каждые 3.5 секунды меняет статус, показывая реальный процесс работы.
    По завершении автоматически удаляет служебное сообщение, чтобы ответ пришёл чисто.
    """
    status_msg = None
    stop_event = asyncio.Event()
    anim_task = None
    chat_action = ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id)

    frames = [
        "💭 Думаю...",
        "💭 Анализирую детали проекта...",
        "💭 Составляю ответ...",
        "⏳ Почти готово...",
    ]

    async def _animate():
        idx = 0
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=3.5)
                break
            except asyncio.TimeoutError:
                idx = (idx + 1) % len(frames)
                if status_msg:
                    try:
                        await status_msg.edit_text(frames[idx])
                    except Exception:
                        pass

    try:
        status_msg = await message.answer(initial_text)
    except Exception:
        status_msg = None

    if status_msg:
        anim_task = asyncio.create_task(_animate())

    try:
        async with chat_action:
            yield status_msg
    finally:
        stop_event.set()
        if anim_task:
            anim_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await anim_task
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass


@contextlib.asynccontextmanager
async def show_working(message, placeholder: str = "⏳ Работаю над этим..."):
    """Edits `message` (the message the tapped button is attached to) to a
    placeholder with no keyboard while the wrapped block runs. Restoring the
    real content/keyboard afterward is the caller's job (send a fresh
    message, or edit this one again) — this only owns the "in progress"
    phase, not what comes after.
    """
    original_text = message.text or message.caption or ""
    try:
        await message.edit_text(placeholder, reply_markup=None)
    except Exception:
        pass  # message may be too old to edit, or already had no markup — non-fatal
    try:
        yield
    finally:
        pass  # caller sends/edits the real result; nothing to restore here

