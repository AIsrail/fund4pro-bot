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
    path = os.path.join(CACHE_DIR, f"{digest}_{safe_name}")
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(content)
    return path
