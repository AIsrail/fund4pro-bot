"""Интеграция с Fundan API для передачи контекста через deep-links.

Deep-link формат:
  https://t.me/fund4_bot?start=draft_<id>   (разработка проекта)
  https://t.me/fund4_bot?start=budget_<id>  (только бюджет)

<id> = 10 hex символов (например, 8f5655e70a)

API: GET https://fundan.cc/api/app-hint?id=<id>
Ответ: {text: "описание задачи"} или 404
TTL: 10 минут (переиспользуемый)
"""

import logging
import httpx

logger = logging.getLogger("fund4pro.fundan_integration")

FUNDAN_API_BASE = "https://fundan.cc/api"
TIMEOUT = 10


async def get_app_hint(hint_id: str) -> dict | None:
    """Получить контекст (описание задачи) из Fundan.
    
    Args:
        hint_id: 10 hex символов (например, 8f5655e70a)
    
    Returns:
        {text: "описание"} или None при ошибке/404
    """
    if not hint_id or len(hint_id) != 10:
        logger.warning(f"get_app_hint: invalid id format (expected 10 hex): {hint_id}")
        return None
    
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(
                f"{FUNDAN_API_BASE}/app-hint",
                params={"id": hint_id}
            )
            
            if resp.status_code == 200:
                data = resp.json()
                text = data.get("text")
                if text:
                    logger.info(f"get_app_hint: received {len(text)} chars for {hint_id}")
                    return {"text": text}
                else:
                    logger.warning(f"get_app_hint: empty text in response")
                    return None
            elif resp.status_code == 404:
                logger.info(f"get_app_hint: not found or expired (404) for {hint_id}")
                return None
            else:
                logger.warning(f"get_app_hint: HTTP {resp.status_code}")
                return None
    except Exception:
        logger.exception(f"get_app_hint: request failed for {hint_id}")
        return None


def parse_start_params(args: str | None) -> tuple[str, str | None] | None:
    """Парсить /start параметры.
    
    Args:
        args: строка аргументов (например, "draft_8f5655e70a" или "budget_abc123def4")
    
    Returns:
        (mode, hint_id) где mode="draft"|"budget" или None
    """
    if not args:
        return None
    
    args = args.strip().lower()
    
    # draft_<id>
    if args.startswith("draft_"):
        hint_id = args[6:].strip()
        if len(hint_id) == 10:
            return ("draft", hint_id)
    
    # budget_<id>
    if args.startswith("budget_"):
        hint_id = args[7:].strip()
        if len(hint_id) == 10:
            return ("budget", hint_id)
    
    return None
