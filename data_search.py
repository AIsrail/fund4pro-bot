"""Best-effort веб-поиск релевантной статистики, когда пользователь отвечает
"нет данных" на Шаге 4 (или для любого другого места, где нужны реальные
цифры/факты вместо предположений).

Раньше был единственный источник (DuckDuckGo HTML) — часто блокируется/
ограничивается, из-за чего бот выглядел так, будто "зацикливается на одних
и тех же стандартных сайтах" (на деле — просто не находил ничего живого и
молча возвращал пустой список). Теперь пробуем несколько независимых
бесплатных источников по очереди (без API-ключей): DuckDuckGo HTML,
Startpage (использует тот же индекс Google, другой фронтенд, реже
блокируется) и Wikipedia REST API (для базовых статистических/справочных
фактов о странах/регионах/явлениях). Все источники best-effort — при
неудаче каждого пробуем следующий, при неудаче всех возвращаем пустой
список, вызывающий код обязан явно пометить данные как оценку (§4.3
плейбука), а не выдавать их как измеренный факт.
"""

import httpx
from bs4 import BeautifulSoup

DDG_URL = "https://html.duckduckgo.com/html/"
STARTPAGE_URL = "https://www.startpage.com/sp/search"
WIKIPEDIA_SEARCH_URL = "https://ru.wikipedia.org/w/api.php"

_HEADERS = {"User-Agent": "Mozilla/5.0"}


async def try_search_statistics(query: str, max_results: int = 5) -> list[dict]:
    """Возвращает список {"title": str, "url": str, "snippet": str}.

    Пробует несколько источников по очереди, возвращает результат первого,
    который дал хоть что-то. Пустой список только если ВСЕ источники
    неудачны — вызывающий код должен воспринимать это как "не нашёл", а не
    падать."""
    for search_fn in (_search_duckduckgo, _search_startpage, _search_wikipedia, _search_crawl4ai):
        try:
            results = await search_fn(query, max_results)
        except Exception:
            results = []
        if results:
            return results
    return []


async def _search_duckduckgo(query: str, max_results: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=_HEADERS) as client:
        resp = await client.post(DDG_URL, data={"q": query})
        resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for result in soup.select(".result")[:max_results]:
        title_el = result.select_one(".result__title a")
        snippet_el = result.select_one(".result__snippet")
        if not title_el:
            continue
        results.append({
            "title": title_el.get_text(strip=True),
            "url": title_el.get("href", ""),
            "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
        })
    return results


async def _search_startpage(query: str, max_results: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=_HEADERS) as client:
        resp = await client.get(STARTPAGE_URL, params={"query": query})
        resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for result in soup.select("div.w-gl__result")[:max_results]:
        title_el = result.select_one("a.w-gl__result-title") or result.select_one("h3")
        snippet_el = result.select_one("p.w-gl__description")
        if not title_el:
            continue
        results.append({
            "title": title_el.get_text(strip=True),
            "url": title_el.get("href", ""),
            "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
        })
    return results


async def _search_wikipedia(query: str, max_results: int) -> list[dict]:
    """Fallback для базовых справочных/статистических фактов, когда живой
    веб-поиск заблокирован — не заменяет актуальную статистику доноров, но
    даёт хоть какую-то опору (демография, география, общие цифры по
    стране/региону), лучше чем полностью пустой результат."""
    async with httpx.AsyncClient(timeout=10, headers=_HEADERS) as client:
        resp = await client.get(WIKIPEDIA_SEARCH_URL, params={
            "action": "query",
            "list": "search",
            "srsearch": query,
            "format": "json",
            "srlimit": max_results,
        })
        resp.raise_for_status()
        data = resp.json()
    results = []
    for item in data.get("query", {}).get("search", []):
        title = item.get("title", "")
        snippet = BeautifulSoup(item.get("snippet", ""), "html.parser").get_text()
        results.append({
            "title": title,
            "url": f"https://ru.wikipedia.org/wiki/{title.replace(' ', '_')}",
            "snippet": snippet,
        })
    return results


async def _search_crawl4ai(query: str, max_results: int) -> list[dict]:
    """Последний fallback: рендерит страницу поиска DuckDuckGo через
    Crawl4AI (реальный headless-браузер) вместо простого httpx-POST.

    РЕАЛЬНЫЙ СЛУЧАЙ: если DuckDuckGo/Startpage начинают отдавать
    антибот-страницу/капчу простому httpx-запросу (частый повторяющийся
    паттерн блокировки — не одноразовый сбой), браузерный рендеринг видит
    то же, что обычный пользователь, и обходит эту защиту чаще. Best-effort
    — при отсутствии crawl4ai или любой ошибке просто возвращает пустой
    список, как и остальные источники здесь."""
    try:
        from crawl4ai import AsyncWebCrawler
    except ImportError:
        return []
    async with AsyncWebCrawler() as crawler:
        result = await crawler.arun(url=f"{DDG_URL}?q={query.replace(' ', '+')}")
    html = result.html or ""
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for item in soup.select(".result")[:max_results]:
        title_el = item.select_one(".result__title a")
        snippet_el = item.select_one(".result__snippet")
        if not title_el:
            continue
        results.append({
            "title": title_el.get_text(strip=True),
            "url": title_el.get("href", ""),
            "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
        })
    return results


def format_results_for_prompt(results: list[dict]) -> str:
    if not results:
        return ""
    lines = []
    for r in results:
        lines.append(f"- {r['title']} ({r['url']}): {r['snippet']}")
    return "\n".join(lines)
