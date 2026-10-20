"""Живой веб-поиск данных и статистики с цепочкой провайдеров
(решение владельца 20.10.2026: Gemini -> ChatGPT -> DeepSeek; Claude из цепочки
убран как самый дорогой по токенам, его можно вернуть переменной
SEARCH_PROVIDERS=claude,gemini,openai,deepseek).

ИСТОРИЯ: с 18.09.2026 основным путём был встроенный веб-поиск Claude, и бот
отлично находил нацстатистику и новости. Когда Anthropic стал недоступен (лимит),
путь молча возвращал пустой результат, и бот падал на слабый скрейпинг HTML —
отсюда «стал хуже находить данные».

Каждый провайдер best-effort: нет ключа / ошибка API / «не нашёл» -> следующий.
  - gemini   — нативный инструмент Google Search (grounding) в Gemini API.
  - openai   — нативный инструмент web_search в OpenAI Responses API.
  - deepseek — у DeepSeek поиска нет: ищем скрейпингом (data_search.py), а
               DeepSeek только извлекает факты из найденных страниц.
  - claude   — прежний web_search_and_summarize (по умолчанию выключен).

Возвращает (текст_с_фактами, [{"title","url"}], имя_провайдера); пустой текст —
ничего не найдено, вызывающий код ставит XYZ и не выдумывает цифры."""

import logging

import httpx

import config

logger = logging.getLogger("fund4pro.web_research")

DEFAULT_PROVIDERS = "gemini,openai,deepseek"
TIMEOUT = 60

SEARCH_SYSTEM_PROMPT = (
    "Ты — ассистент, который ищет РЕАЛЬНЫЕ, актуальные факты и цифры в открытом "
    "интернете по запросу для грантовой заявки. Используй веб-поиск (можно несколько "
    "запросов). Приоритет — официальные источники страны проекта (национальная "
    "статистика, министерства, местные органы власти), затем международные "
    "организации (ООН, Всемирный банк, ЮНВТО и др.), затем информагентства и "
    "профильные СМИ. В ответе — только конкретные найденные факты и цифры с "
    "указанием источника и года (название + что именно там написано), без домыслов "
    "и оценок от себя. Если релевантных данных по существу вопроса не нашлось — "
    "прямо напиши «Живых данных по этому запросу не нашлось», НЕ придумывай "
    "правдоподобную цифру и не подменяй запрошенное общими сведениями не по теме."
)


def _user_message(query: str, context: str) -> str:
    return query if not context else f"{query}\n\nКонтекст проекта: {context}"


def _is_no_data(summary: str) -> bool:
    low = (summary or "").lower()
    return not low.strip() or ("живых данных" in low and "не нашл" in low)


async def _search_gemini(query: str, context: str) -> tuple[str, list[dict]]:
    if not config.GEMINI_API_KEY:
        return "", []
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.FALLBACK_LLM_MODEL}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": SEARCH_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": _user_message(query, context)}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"maxOutputTokens": 1500},
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(url, json=body, headers={"x-goog-api-key": config.GEMINI_API_KEY})
        resp.raise_for_status()
        data = resp.json()
    cands = data.get("candidates") or []
    if not cands:
        return "", []
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
    sources = []
    for ch in (cands[0].get("groundingMetadata") or {}).get("groundingChunks") or []:
        web = ch.get("web") or {}
        if web.get("uri"):
            sources.append({"title": web.get("title") or web["uri"], "url": web["uri"]})
    return text, sources


async def _search_openai(query: str, context: str) -> tuple[str, list[dict]]:
    if not config.OPENAI_API_KEY:
        return "", []
    body = {
        "model": config.OPENAI_MODEL,
        "instructions": SEARCH_SYSTEM_PROMPT,
        "input": _user_message(query, context),
        "tools": [{"type": "web_search"}],
        "max_output_tokens": 1500,
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            "https://api.openai.com/v1/responses", json=body,
            headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
        )
        resp.raise_for_status()
        data = resp.json()
    texts: list[str] = []
    sources: list[dict] = []
    for item in data.get("output") or []:
        if item.get("type") != "message":
            continue
        for c in item.get("content") or []:
            if c.get("type") == "output_text":
                if c.get("text"):
                    texts.append(c["text"])
                for ann in c.get("annotations") or []:
                    if ann.get("type") == "url_citation" and ann.get("url"):
                        sources.append({"title": ann.get("title") or ann["url"], "url": ann["url"]})
    return "\n".join(texts).strip(), sources


async def _search_deepseek(query: str, context: str) -> tuple[str, list[dict]]:
    """DeepSeek не умеет искать — берём результаты скрейпинга и просим его
    извлечь только то, что в них реально написано."""
    from data_search import format_results_for_prompt, try_search_statistics
    import llm

    if llm._deepseek_client is None:
        return "", []
    results = await try_search_statistics(query)
    if not results:
        return "", []
    prompt = (
        f"{_user_message(query, context)}\n\nРезультаты веб-поиска:\n{format_results_for_prompt(results)}\n\n"
        "Выпиши только конкретные факты и цифры, которые ПРЯМО есть в этих результатах, с "
        "источником и годом. Если нужных данных там нет — напиши «Живых данных по этому запросу не нашлось»."
    )
    resp = await llm._deepseek_client.chat.completions.create(
        model=getattr(config, "DEEPSEEK_MODEL", "deepseek-chat"),
        messages=[{"role": "system", "content": SEARCH_SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
        max_tokens=1200,
    )
    text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
    return text, [{"title": r["title"], "url": r["url"]} for r in results[:5]]


async def _search_claude(query: str, context: str) -> tuple[str, list[dict]]:
    from llm import web_search_and_summarize

    return await web_search_and_summarize(query, context=context)


PROVIDERS = {
    "gemini": _search_gemini,
    "openai": _search_openai,
    "deepseek": _search_deepseek,
    "claude": _search_claude,
}


def provider_order() -> list[str]:
    import os

    raw = os.environ.get("SEARCH_PROVIDERS", DEFAULT_PROVIDERS)
    return [p.strip().lower() for p in raw.split(",") if p.strip().lower() in PROVIDERS]


async def web_research(query: str, context: str = "") -> tuple[str, list[dict], str]:
    for name in provider_order():
        try:
            summary, sources = await PROVIDERS[name](query, context)
        except Exception as exc:
            logger.warning("web_research: %s failed: %s: %s", name, type(exc).__name__, exc)
            continue
        if _is_no_data(summary):
            logger.info("web_research: %s found nothing, trying next", name)
            continue
        logger.info("web_research: answered by %s (%d sources)", name, len(sources))
        return summary.strip(), sources, name
    return "", [], ""
