"""Shared helper for sending long generated text to Telegram.

Telegram rejects a single message over ~4096 chars with
'Bad Request: message is too long'. LLM-generated concepts/final
documents easily exceed that, so every handler that shows such text
must split it instead of calling message.answer() directly.
"""

TELEGRAM_MESSAGE_LIMIT = 4096


async def send_long(message, text: str, reply_markup=None) -> None:
    """Send `text` to `message`'s chat, splitting into <=4096-char chunks
    on paragraph/line boundaries when needed. `reply_markup` is attached
    only to the last chunk."""
    text = (text or "").strip()
    if not text:
        return
    if len(text) <= TELEGRAM_MESSAGE_LIMIT:
        await message.answer(text, reply_markup=reply_markup)
        return
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= TELEGRAM_MESSAGE_LIMIT:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n\n", 0, TELEGRAM_MESSAGE_LIMIT)
        if cut < 500:
            cut = remaining.rfind("\n", 0, TELEGRAM_MESSAGE_LIMIT)
        if cut < 500:
            cut = TELEGRAM_MESSAGE_LIMIT
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        await message.answer(chunk, reply_markup=reply_markup if is_last else None)
