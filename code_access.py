"""Работа с кодами доступа от сайта fundan.cc.

API:
  - POST /api/fund4pro/redeem: {"code": "..."} → {"plan": "pro"|"db", "remaining": N}
  - GET /api/fund4pro/status: {"code": "..."} → {"plan": "pro"|"db", "remaining": N}

plan:
  - "pro" = тариф 1500 или 8900 сом (полный доступ)
  - "db" = старый базовый тариф (не используется; "200 сом разово" коды не выдаёт)

remaining: количество проектов, оставшееся в этом месяце по тарифу.
"""

import logging
import httpx

logger = logging.getLogger("fund4pro.code_access")

FUNDAN_API_BASE = "https://fundan.cc/api/fund4pro"
TIMEOUT = 10  # секунд


async def redeem_code(code: str) -> dict | None:
    """Отправить код на redeem, вернуть {plan, remaining} или None при ошибке.

    Код — это 16 символов hex (пример: 751F3C29B0DE8ED1), выданный @c4faq_bot.
    """
    if not code or len(code) != 16:
        logger.warning("redeem_code: invalid code format (expected 16 hex chars)")
        return None

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                f"{FUNDAN_API_BASE}/redeem",
                json={"code": code}
            )
            resp.raise_for_status()
            data = resp.json()
            plan = data.get("plan")
            remaining = data.get("remaining")
            if plan and remaining is not None:
                logger.info(f"redeem_code: plan={plan}, remaining={remaining}")
                return {"plan": plan, "remaining": remaining}
            else:
                logger.warning(f"redeem_code: invalid response {data}")
                return None
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 400:
            logger.warning(f"redeem_code: invalid code (400)")
            return None
        elif e.response.status_code == 401:
            logger.warning(f"redeem_code: unauthorized (401) — API key missing?")
            return None
        else:
            logger.error(f"redeem_code: HTTP {e.response.status_code}")
            return None
    except Exception:
        logger.exception("redeem_code: request failed")
        return None


async def get_status(code: str) -> dict | None:
    """Проверить статус кода (не списывает его)."""
    if not code or len(code) != 16:
        logger.warning("get_status: invalid code format")
        return None

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(
                f"{FUNDAN_API_BASE}/status",
                params={"code": code}
            )
            resp.raise_for_status()
            data = resp.json()
            plan = data.get("plan")
            remaining = data.get("remaining")
            if plan and remaining is not None:
                return {"plan": plan, "remaining": remaining}
            else:
                logger.warning(f"get_status: invalid response {data}")
                return None
    except Exception:
        logger.exception("get_status: request failed")
        return None
