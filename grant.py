"""Команда /grant для ручной выдачи проектов владельцем (временное решение).

Использование (только для OWNER_CHAT_ID):
  /grant <chat_id> <количество>
  /grant 123456789 10

Добавляет N платных кредитов на "project" ресурс.
"""

import logging

import config
import billing

logger = logging.getLogger("fund4pro.grant")


async def is_owner(user_id: int) -> bool:
    """Проверяет, владелец ли это бота."""
    if not config.OWNER_CHAT_ID:
        return False
    try:
        owner_id = int(config.OWNER_CHAT_ID)
        return user_id == owner_id
    except (ValueError, TypeError):
        return False


async def grant_projects(chat_id: int, quantity: int) -> tuple[bool, str]:
    """Выдать N проектов пользователю.

    Returns: (success: bool, message: str)
    """
    if not isinstance(chat_id, int) or chat_id <= 0:
        return False, "❌ chat_id должен быть положительным числом"

    if not isinstance(quantity, int) or quantity <= 0:
        return False, "❌ Количество должно быть положительным числом"

    if quantity > 100:
        return False, "❌ Лимит: максимум 100 проектов за раз (для безопасности)"

    try:
        await billing.add_paid_credit(chat_id, "project", quantity)
        logger.info(f"grant: выданы {quantity} проектов для chat {chat_id}")
        return True, f"✅ Выдано {quantity} проектов пользователю {chat_id}"
    except Exception as e:
        logger.exception(f"grant: ошибка для chat {chat_id}")
        return False, f"❌ Ошибка при выдаче: {str(e)}"
