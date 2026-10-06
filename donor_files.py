"""Скачивание файлов форм донора надёжнее, чем donor_scrape.

donor_scrape.try_scrape_donor_forms находит только ссылки, оканчивающиеся на
.pdf/.doc/.docx/.xls/.xlsx. Пропускает: ссылки вида /download?id=3, ?file=form.docx,
Google Docs/Sheets/Drive, а также прямую ссылку на сам файл. Здесь это закрыто:
формат определяется по содержимому (сигнатуре), а не по имени.
"""

import io
import logging
import re
import zipfile
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("fund4pro.donor_files")

HEADERS = {"User-Agent": "Mozilla/5.0"}
MAX_BYTES = 15 * 1024 * 1024
FILE_EXT = (".docx", ".doc", ".xlsx", ".xls", ".pdf")
_HREF_HINTS = ("download", "attachment", "upload", "getfile", "file=", "/files/", "wp-content",
               "form", "template", "budget", "application", "blank", "docs.google", "drive.google")
_TEXT_HINTS = re.compile(
    r"скача|download|форма|заявк|шаблон|бюджет|анкет|application|template|budget|form|proposal",
    re.IGNORECASE)
_EXT_BY_KIND = {"docx": ".docx", "xlsx": ".xlsx", "pdf": ".pdf", "pptx": ".pptx"}


def sniff_kind(content: bytes) -> str | None:
    """docx | xlsx | pptx | pdf | legacy (старый бинарный Office) | None (не файл)."""
    if content[:5] == b"%PDF-":
        return "pdf"
    if content[:8] == bytes.fromhex("D0CF11E0A1B11AE1"):
        return "legacy"
    if content[:4] == b"PK\x03\x04":
        try:
            names = zipfile.ZipFile(io.BytesIO(content)).namelist()
        except Exception:
            return None
        if "word/document.xml" in names:
            return "docx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        if "ppt/presentation.xml" in names:
            return "pptx"
    return None


def google_export_url(url: str) -> str | None:
    """Ссылка на Google Docs/Sheets/Drive -> прямая ссылка на скачивание."""
    m = re.search(r"docs\.google\.com/document/d/([\w-]+)", url)
    if m:
        return f"https://docs.google.com/document/d/{m.group(1)}/export?format=docx"
    m = re.search(r"docs\.google\.com/spreadsheets/d/([\w-]+)", url)
    if m:
        return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=xlsx"
    m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:export=download&)?id=)([\w-]+)", url)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    return None


def _filename(resp: httpx.Response, kind: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.IGNORECASE) or re.search(r'filename="?([^";]+)"?', cd)
    name = unquote(m.group(1).strip().strip('"')) if m else unquote(urlparse(str(resp.url)).path.rsplit("/", 1)[-1])
    name = name or "donor_form"
    ext = _EXT_BY_KIND.get(kind)
    if ext and not name.lower().endswith(ext):
        name = name.rsplit(".", 1)[0] + ext if "." in name[-6:] else name + ext
    return name[:120]


async def _fetch(client: httpx.AsyncClient, url: str) -> dict | None:
    try:
        resp = await client.get(url)
        resp.raise_for_status()
    except Exception:
        return None
    content = resp.content
    if not content or len(content) > MAX_BYTES:
        return None
    kind = sniff_kind(content)
    if not kind:
        return None
    return {"name": _filename(resp, kind), "content": content, "kind": kind, "url": str(resp.url)}


async def download_direct(url: str) -> dict | None:
    """Если ссылка сама ведёт на файл (прямая, Google Docs/Sheets/Drive) —
    скачивает его; для обычной веб-страницы возвращает None."""
    target = google_export_url(url)
    path = urlparse(url).path.lower()
    if not target and not path.endswith(FILE_EXT):
        return None
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=HEADERS) as client:
        return await _fetch(client, target or url)


async def discover_page_files(url: str, limit: int = 5) -> list[dict]:
    """Ищет на странице файловые ссылки БЕЗ расширения в href (ссылки-скачивания,
    ?file=…, Google Docs) и проверяет каждую по содержимому."""
    html = ""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=HEADERS) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html, base = resp.text, str(resp.url)
    except Exception:
        base = url
    anchors: list[tuple[str, str]] = []
    if html:
        soup = BeautifulSoup(html, "html.parser")
        anchors = [(a["href"], a.get_text(" ", strip=True)) for a in soup.find_all("a", href=True)]
    if not anchors:
        try:
            from donor_scrape import _crawl4ai_fetch_page

            _, links = await _crawl4ai_fetch_page(url)
            anchors = [(h, "") for h in links]
        except Exception:
            logger.warning("discover_page_files: crawl4ai fallback failed", exc_info=True)

    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for href, text in anchors:
        h = href.strip()
        if not h or h.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base, h)
        if full in seen:
            continue
        seen.add(full)
        low = full.lower()
        score = 0
        if any(e in low for e in FILE_EXT):
            score += 3
        if google_export_url(full):
            score += 3
        if any(k in low for k in _HREF_HINTS):
            score += 1
        if _TEXT_HINTS.search(text):
            score += 2
        if score:
            scored.append((score, full))
    scored.sort(key=lambda x: -x[0])

    files: list[dict] = []
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=HEADERS) as client:
        for _, link in scored[:limit * 2]:
            got = await _fetch(client, google_export_url(link) or link)
            if got:
                files.append(got)
            if len(files) >= limit:
                break
    return files
