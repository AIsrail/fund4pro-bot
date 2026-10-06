"""Валюта бюджета и курс обмена для черновика бюджета.

Курс нужен приблизительный (пользователь всё равно правит цифры): сначала
открытый API без ключа, при его сбое — обычный веб-поиск + извлечение числа
моделью. Результат всегда показывается пользователю с источником."""

import logging
import re

import httpx

logger = logging.getLogger("fund4pro.fx")

_ALIASES = {
    "TJS": ("tjs", "сомони"),
    "USD": ("usd", "$", "доллар", "долл", "dollar", "бакс"),
    "EUR": ("eur", "€", "евро", "euro"),
    "KGS": ("kgs", "сом", "som", "сомах", "сомов", "сомы"),
    "KZT": ("kzt", "тенге", "tenge"),
    "RUB": ("rub", "₽", "руб", "рубл"),
    "UZS": ("uzs", "узбекский сум", "сум"),
    "GBP": ("gbp", "£", "фунт"),
    "UAH": ("uah", "грн", "гривн"),
    "GEL": ("gel", "лари"),
    "AMD": ("amd", "драм"),
    "AZN": ("azn", "манат"),
    "MDL": ("mdl", "лей"),
    "TRY": ("try", "лир"),
    "CNY": ("cny", "юань", "yuan"),
}


def normalize_currency(text: str) -> str:
    t = (text or "").strip().lower()
    if re.fullmatch(r"[a-z]{3}", t):
        return t.upper()
    for code, keys in _ALIASES.items():
        if any(k in t for k in keys):
            return code
    return (text or "").strip().upper()[:20]


async def _rate_from_api(code: str) -> tuple[float, str] | None:
    async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as client:
        resp = await client.get("https://open.er-api.com/v6/latest/USD")
        resp.raise_for_status()
        data = resp.json()
    rate = (data.get("rates") or {}).get(code)
    if data.get("result") == "success" and rate:
        return float(rate), "open.er-api.com (курсы обновляются раз в сутки)"
    return None


async def _rate_from_search(code: str) -> tuple[float, str] | None:
    from data_search import format_results_for_prompt, try_search_statistics
    from llm import call_claude

    results = await try_search_statistics(f"1 USD to {code} exchange rate today", max_results=5)
    if not results:
        return None
    raw = await call_claude(
        "Из результатов поиска извлеки текущий курс: сколько единиц валюты "
        f"{code} за 1 доллар США. Верни ТОЛЬКО число (например 87.45) или слово "
        "NONE, если в результатах нет явного курса. Ничего не придумывай.",
        format_results_for_prompt(results),
        max_tokens=30,
    )
    m = re.search(r"\d+(?:[.,]\d+)?", raw or "")
    if not m or "NONE" in (raw or "").upper():
        return None
    return float(m.group(0).replace(",", ".")), f"веб-поиск ({results[0].get('url', '')})"


async def get_usd_rate(code: str) -> tuple[float, str] | None:
    """(сколько единиц `code` за 1 USD, источник) или None, если не нашли."""
    code = normalize_currency(code)
    if code == "USD":
        return 1.0, "—"
    for fetch in (_rate_from_api, _rate_from_search):
        try:
            found = await fetch(code)
        except Exception as exc:
            logger.warning("get_usd_rate: %s failed: %s: %s", fetch.__name__, type(exc).__name__, exc)
            continue
        if found and found[0] > 0:
            return found
    return None
