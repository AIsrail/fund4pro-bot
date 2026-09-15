"""Модуль каталога актуальных грантов из Telegram-канала Connect4pro (@connect4_pro).

Позволяет агенту подбирать реально открытые конкурсы грантов с актуальными
дедлайнами, суммами и ссылками для заявителей из Кыргызстана и Центральной Азии.
"""
import json
import logging
import os
import re
import time
import urllib.request
from datetime import date, datetime, timedelta
from bs4 import BeautifulSoup

logger = logging.getLogger("fund4pro.connect4pro")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "connect4pro_grants.json")
CHANNEL_URL = "https://t.me/s/connect4_pro"
CHANNEL_ID = -1001448518207

# РЕАЛЬНЫЙ ИНЦИДЕНТ: бот предложил пользователю "Микрогрант $2-4k, дедлайн
# 6 сентября" как срочный вариант ("торопимся"), хотя на момент разговора
# было уже 15 сентября — дедлайн реально прошёл 9 дней назад. Сам пост в
# канале действительно написан именно так (не баг парсинга), но каталог
# никогда не проверял дедлайны на актуальность — раз попав в кэш, просроченный
# конкурс продолжал предлагаться как "актуальный" бесконечно. Ниже — парсер
# русских дат вида "6 сентября"/"2 октября" и фильтр, отсеивающий то, что уже
# точно прошло, прежде чем отдавать список конкурсов модели.
_RU_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
_DEADLINE_DATE_RE = re.compile(r"(\d{1,2})\s+([а-яё]+)", re.IGNORECASE)
# Свежий кэш перечитывать не нужно — раз в CACHE_TTL_SECONDS проверяем канал
# заново, чтобы новые посты (как реальный GGF-пост с дедлайном 2 октября)
# попадали в выдачу без ручной очистки кэша.
CACHE_TTL_SECONDS = 6 * 60 * 60


def _parse_deadline_date(deadline: str, today: date) -> date | None:
    """Разбирает дату вида "6 сентября" в date текущего (или следующего, если
    дата уже "давно" в прошлом — типичный перенос через Новый год) года.
    Возвращает None для нечисловых дедлайнов ("не указан", "прием постоянно")
    — их не с чем сравнивать, и это не значит "просрочено"."""
    m = _DEADLINE_DATE_RE.search(deadline.lower())
    if not m:
        return None
    day = int(m.group(1))
    word = m.group(2)
    month = next((num for stem, num in _RU_MONTHS.items() if word.startswith(stem)), None)
    if month is None:
        return None
    try:
        candidate = date(today.year, month, day)
    except ValueError:
        return None
    # Если "дата" на 300+ дней в прошлом — это почти наверняка тот же день
    # в СЛЕДУЮЩЕМ году (пост про январский дедлайн, написанный в декабре),
    # а не по-настоящему протухший конкурс на месяцы, а не дни/недели.
    if (today - candidate).days > 300:
        try:
            candidate = date(today.year + 1, month, day)
        except ValueError:
            return None
    return candidate


def _is_expired(grant: dict, today: date) -> bool:
    deadline_date = _parse_deadline_date(grant.get("deadline", ""), today)
    if deadline_date is None:
        return False
    return deadline_date < today


def _parse_grant_element(text_el, post_id: str = "", pub_date: str = "") -> dict | None:
    raw_text = text_el.get_text(separator="\n")
    lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
    if not lines:
        return None

    # Проверяем наличие ключевых маркеров гранта/возможности
    has_markers = any(m in raw_text for m in ["👉", "💸", "📅", "Грант", "Дедлайн", "Инвестиции", "USD", "сомов", "$"])
    if not has_markers:
        return None

    title = ""
    amount = ""
    deadline = ""
    link = ""

    # Извлекаем ссылки из тегов <a>
    for a in text_el.find_all("a"):
        href = a.get("href", "")
        if href.startswith("http") and "grantmanual" not in href and "t.me/KGinvest" not in href:
            link = href
            break

    for line in lines:
        if line.startswith("👉"):
            title = line.lstrip("👉").strip()
        elif any(marker in line for marker in ["💸", "Грант:", "Гранты:", "Инвестиции:"]):
            amount = re.sub(r"^.*?💸\s*", "", line).strip()
            amount = re.sub(r"^(?:грант[ыа]?|инвестиции|призы|сумма)[:\s]*", "", amount, flags=re.IGNORECASE).strip()
        elif any(marker in line for marker in ["📅", "Дедлайн:", "Дата:"]):
            deadline = re.sub(r"^.*?📅\s*", "", line).strip()
            deadline = re.sub(r"^(?:дедлайн|дата|срок)[:\s]*", "", deadline, flags=re.IGNORECASE).strip()
        elif line.startswith("http") and not link and "grantmanual" not in line and "t.me/KGinvest" not in line:
            link = line.strip()

    if not title:
        for l in lines:
            clean_l = l.lstrip("👉-• ").strip()
            if len(clean_l) > 10 and not any(clean_l.startswith(p) for p in ["💸", "📅", "http"]):
                title = clean_l
                break

    if not title:
        return None

    title = re.split(r'💸|📅|http', title)[0].strip()

    return {
        "post_id": post_id,
        "title": title,
        "amount": amount or "По условиям донора",
        "deadline": deadline or "Уточняется",
        "link": link or "",
        "date": pub_date or datetime.now().isoformat(),
        "raw_text": raw_text,
    }


def fetch_and_update_from_web() -> list[dict]:
    """Стягивает последние посты из веб-просмотра канала https://t.me/s/connect4_pro."""
    try:
        req = urllib.request.Request(
            CHANNEL_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        soup = BeautifulSoup(html, "html.parser")
        wraps = soup.find_all("div", class_="tgme_widget_message_wrap")

        existing_grants = load_cached_grants()
        grants_by_id = {g.get("post_id") or g.get("title"): g for g in existing_grants}

        new_count = 0
        for w in wraps:
            msg_div = w.find("div", class_="tgme_widget_message")
            data_post = msg_div.get("data-post", "") if msg_div else ""
            text_el = w.find("div", class_="tgme_widget_message_text")
            if not text_el:
                continue
            time_el = w.find("time")
            pub_date = time_el.get("datetime", "") if time_el else ""

            grant = _parse_grant_element(text_el, post_id=data_post, pub_date=pub_date)
            if grant:
                pid = grant["post_id"] or grant["title"]
                if pid not in grants_by_id:
                    grants_by_id[pid] = grant
                    new_count += 1
                else:
                    grants_by_id[pid].update(grant)

        all_grants = list(grants_by_id.values())
        all_grants.sort(key=lambda x: x.get("date", ""), reverse=True)
        save_grants(all_grants)
        logger.info("Updated connect4pro grants catalog: total=%d (new=%d)", len(all_grants), new_count)
        return all_grants
    except Exception as e:
        logger.warning("Failed to fetch connect4pro web preview: %s", e)
        return load_cached_grants()


def load_cached_grants() -> list[dict]:
    if not os.path.exists(CACHE_FILE):
        return []
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Failed to read %s: %s", CACHE_FILE, e)
        return []


def save_grants(grants: list[dict]) -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(grants, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("Failed to write %s: %s", CACHE_FILE, e)


def register_channel_post(raw_text: str, post_id: str = "", pub_date: str = "") -> dict | None:
    """Обрабатывает входящий пост канала через aiogram в реальном времени."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(f"<div>{raw_text}</div>", "html.parser")
    grant = _parse_grant_element(soup, post_id=post_id, pub_date=pub_date)
    if not grant:
        return None
    grants = load_cached_grants()
    filtered = [g for g in grants if g.get("post_id") != grant["post_id"] and g.get("title") != grant["title"]]
    filtered.insert(0, grant)
    save_grants(filtered)
    logger.info("Registered new grant from channel post: %s", grant["title"])
    return grant


def search_matching_grants(query: str = "", limit: int = 4) -> list[dict]:
    """Ищет релевантные гранты в каталоге. Если каталог пуст ИЛИ старше
    CACHE_TTL_SECONDS, стягивает из канала заново (best-effort — при неудаче
    остаётся на том, что уже есть в кэше). Просроченные по дедлайну конкурсы
    отфильтровываются перед подбором/возвратом — их наличие в кэше не значит,
    что их стоит предлагать пользователю как актуальные."""
    grants = load_cached_grants()
    cache_stale = not os.path.exists(CACHE_FILE) or (time.time() - os.path.getmtime(CACHE_FILE)) > CACHE_TTL_SECONDS
    if not grants or cache_stale:
        grants = fetch_and_update_from_web() or grants

    today = date.today()
    grants = [g for g in grants if not _is_expired(g, today)]

    if not query or not query.strip():
        return grants[:limit]

    # Разбиваем запрос на значимые слова (>3 символов)
    terms = [t.lower() for t in re.findall(r'\b\w{3,}\b', query)]
    if not terms:
        return grants[:limit]

    scored = []
    for g in grants:
        text_corp = (g["title"] + " " + g.get("raw_text", "")).lower()
        score = 0
        for t in terms:
            # Префиксный поиск (например "эколог" -> "экология", "туриз" -> "туризм")
            root = t[:5] if len(t) > 5 else t
            if root in text_corp:
                score += 1
            if root in g["title"].lower():
                score += 3

        if score > 0:
            scored.append((score, g))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = [item[1] for item in scored[:limit]]

    if not results:
        results = grants[:limit]

    return results
