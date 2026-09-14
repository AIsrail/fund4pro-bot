"""Попытка скачать И прочитать формы заявки/бюджета/гайдлайны по ссылке.

Best-effort реализация: находит прямые ссылки на .pdf/.doc/.docx на странице,
СКАЧИВАЕТ каждый файл и извлекает текст (pypdf для .pdf, python-docx для
.docx; .doc — старый бинарный формат Word, нет лёгкого читателя без внешних
инструментов, пропускается с явной пометкой). Раньше эта функция только
собирала URL без скачивания содержимого — бот радостно писал "✅ Нашёл и
скачал", но у LLM на самом деле не было текста файлов, только их ссылки,
из-за чего generate_ideas честно (и путающе для пользователя) отвечал
"не могу открыть содержимое PDF". Теперь функция возвращает РЕАЛЬНЫЙ текст,
который handlers/donor_info.py сохраняет в donor_forms_text и llm.py
подмешивает в промпт для генерации идей/деревьев/документа.

Ничего не умеет обходить login wall/антибот/XFA-формы (SF-424 и подобные)
— в таких случаях просто возвращает пустой список форм, и хендлер в
donor_info.py показывает пользователю кнопки ручного flow, как и описано
в ТЗ.
"""

import io
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

FILE_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx")
MAX_FILES_TO_FETCH = 3  # не заливать LLM-контекст десятками найденных ссылок
MAX_CHARS_PER_FILE = 6000  # оставляет запас в контексте под остальной промпт


async def _crawl4ai_fetch_page(url: str) -> tuple[str, list[str]]:
    """Fallback-путь через Crawl4AI (реальный headless-браузер с рендерингом
    JS) для сайтов, которые блокируют/не отдают контент простому httpx-GET
    (Cloudflare-защита, контент, подгружаемый JS, и т.п.) — httpx+BeautifulSoup
    тогда либо падает с ошибкой, либо возвращает пустую/неполную страницу без
    единой ссылки на форму.

    Возвращает (markdown_текст_страницы, [все_найденные_href]). Best-effort —
    любая ошибка (crawl4ai не установлен, браузер недоступен, таймаут)
    молча даёт пустой результат, вызывающий код просто остаётся с тем, что
    уже получил от старого httpx-пути.
    """
    try:
        from crawl4ai import AsyncWebCrawler
    except ImportError:
        return "", []
    try:
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=url)
        text = (result.markdown or result.html or "").strip()
        links = []
        raw_links = getattr(result, "links", None) or {}
        for group in ("internal", "external"):
            for item in raw_links.get(group, []):
                href = item.get("href") if isinstance(item, dict) else None
                if href:
                    links.append(href)
        return text, links
    except Exception:
        return "", []


async def try_scrape_donor_forms(url: str) -> list[dict]:
    """Возвращает список {"url": str, "text": str, "filename": str,
    "content": bytes} для найденных форм.

    "text" содержит извлечённый текст (может быть пустым, если формат не
    поддержан или скачивание/парсинг не удался — вызывающий код должен
    относиться к пустому тексту как к "ссылка найдена, но не прочитана",
    не как к отсутствию формы вовсе). "content" — сырые байты файла (может
    быть пустым, если скачивание не удалось) — используется вызывающим
    кодом, чтобы переслать пользователю РЕАЛЬНЫЙ файл формы донора в
    Telegram, а не только описать его словами.

    Раньше искались только .pdf/.doc/.docx — бюджетные шаблоны доноры
    часто публикуют именно в .xlsx, и такие ссылки полностью
    игнорировались (бот не видел их вообще).
    """
    try:
        async with httpx.AsyncClient(
            timeout=10,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        page_text = soup.get_text(separator="\n", strip=True)
        raw_links = [a["href"] for a in soup.find_all("a", href=True)]
        base_url = str(resp.url)
    except Exception:
        # РЕАЛЬНЫЙ СЛУЧАЙ: httpx получает 403/пустой ответ от сайтов за
        # Cloudflare/антибот-защитой, или контент грузится JS-ом уже после
        # первичного рендера — httpx не видит ничего вообще. Fallback на
        # Crawl4AI (реальный headless-браузер) — он умеет то же, что видит
        # обычный пользователь в браузере.
        page_text, raw_links = await _crawl4ai_fetch_page(url)
        base_url = url
        if not page_text and not raw_links:
            return [], ""

    links = []
    seen = set()
    for href in raw_links:
        if href.lower().endswith(FILE_EXTENSIONS):
            full_url = urljoin(base_url, href)
            if full_url not in seen:
                seen.add(full_url)
                links.append(full_url)

    if not links:
        # httpx нашёл страницу, но НИ ОДНОЙ ссылки на файл формы — частый
        # случай на сайтах, где ссылки генерируются JS (карточки конкурсов,
        # кнопки "скачать" через фреймворк) и невидимы для httpx+BeautifulSoup.
        # Пробуем ещё раз через Crawl4AI ПЕРЕД тем, как сдаться.
        crawl_text, crawl_links = await _crawl4ai_fetch_page(url)
        for href in crawl_links:
            if href.lower().endswith(FILE_EXTENSIONS):
                full_url = urljoin(url, href)
                if full_url not in seen:
                    seen.add(full_url)
                    links.append(full_url)
        if crawl_text and len(crawl_text) > len(page_text):
            page_text = crawl_text
        if not links:
            return [], page_text

    results = []
    async with httpx.AsyncClient(
        timeout=20,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0"},
    ) as client:
        for file_url in links[:MAX_FILES_TO_FETCH]:
            text, content = await _fetch_and_extract(client, file_url)
            filename = file_url.rsplit("/", 1)[-1] or "donor_form"
            fmt = "xlsx" if filename.lower().endswith((".xls", ".xlsx")) else "other"
            results.append({
                "url": file_url, "text": text, "filename": filename,
                "content": content, "format": fmt,
            })
    return results, page_text


async def _fetch_and_extract(client: httpx.AsyncClient, file_url: str) -> tuple[str, bytes]:
    try:
        resp = await client.get(file_url)
        resp.raise_for_status()
        content = resp.content
    except Exception:
        return "", b""

    lower = file_url.lower()
    try:
        if lower.endswith(".pdf"):
            return _extract_pdf_text(content), content
        if lower.endswith(".docx"):
            return _extract_docx_text(content), content
        if lower.endswith(".xlsx"):
            return _extract_xlsx_text(content), content
        # .doc/.xls (legacy binary Office) — нет лёгкого читателя без внешних
        # инструментов (antiword/LibreOffice); честно возвращаем пусто,
        # а не молча пропускаем ссылку. Байты всё равно возвращаем — файл
        # можно переслать пользователю, даже если текст извлечь не смогли.
        return "", content
    except Exception:
        return "", content


def _extract_pdf_text(content: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    parts = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    text = "\n".join(parts).strip()
    return text[:MAX_CHARS_PER_FILE]


def _extract_docx_text(content: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(content))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text.strip():
                    parts.append(cell.text.strip())
    text = "\n".join(parts).strip()
    return text[:MAX_CHARS_PER_FILE]


def _extract_xlsx_text(content: bytes) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    parts = []
    for sheet in wb.worksheets:
        parts.append(f"[Лист: {sheet.title}]")
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                parts.append(" | ".join(cells))
    text = "\n".join(parts).strip()
    return text[:MAX_CHARS_PER_FILE]
