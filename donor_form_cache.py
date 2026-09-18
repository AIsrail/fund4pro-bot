"""Локальный кэш скачанных файлов форм донора — чтобы можно было переслать
пользователю ОРИГИНАЛЬНЫЙ файл в Telegram (не просто пересказ содержимого),
и повторно приложить его в конце рядом с финальным документом."""

import hashlib
import os

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".donor_forms_cache")


def save_donor_form(content: bytes, filename: str) -> str:
    """Сохраняет байты файла на диск, возвращает путь. Пустые байты не
    сохраняются (ничего скачать не удалось) — вызывающий код должен
    проверить непустоту content до вызова."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    digest = hashlib.sha1(content).hexdigest()[:12]
    safe_name = "".join(c for c in filename if c.isalnum() or c in "._- ") or "donor_form"
    # РЕАЛЬНЫЙ ИНЦИДЕНТ: сайт немецкого посольства отдавал URL-закодированное
    # кириллическое имя файла; без urllib.parse.unquote() выше по цепочке
    # (donor_scrape.py) в safe_name склеивались десятки hex-символов (они
    # alnum и проходят фильтр), итоговое имя перевалило за лимит файловой
    # системы (OSError: File name too long) и весь ход агента падал с
    # необработанным исключением. unquote() чинит ЭТОТ конкретный случай,
    # но длина имени файла в принципе ничем не ограничена — общая защита
    # нужна независимо от того, что именно однажды снова сделает имя
    # слишком длинным (другой донор, другой баг выше по цепочке). digest
    # уже гарантирует уникальность файла на диске, так что safe_name — это
    # только для читаемости человеком, обрезать его безопасно.
    ext = os.path.splitext(safe_name)[1][:10]
    stem = safe_name[:len(safe_name) - len(ext)] if ext else safe_name
    safe_name = stem[:80] + ext
    path = os.path.join(CACHE_DIR, f"{digest}_{safe_name}")
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(content)
    return path
