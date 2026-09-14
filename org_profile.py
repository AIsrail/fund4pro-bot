"""Долгоживущая память об организации/бизнесе пользователя — переживает
ЛЮБОЙ сброс сессии (/start, кнопка "Начать заново", завершение проекта),
в отличие от FSM-состояния конкретного проекта (донор/идея/бюджет — эти
специфичны для одной заявки и правильно сбрасываются).

Раньше при каждом /start или новом проекте пользователю приходилось
заново присылать профиль организации, документы, регистрационные данные
— даже если это та же самая организация, просто новый донор/проект.
Теперь эта информация запоминается один раз и предлагается повторно."""

import json
import os

PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".org_profiles")


def _path(user_id: int) -> str:
    os.makedirs(PROFILE_DIR, exist_ok=True)
    return os.path.join(PROFILE_DIR, f"{user_id}.json")


def save_org_profile(user_id: int, org_info: str) -> None:
    if not org_info.strip():
        return
    path = _path(user_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"org_info": org_info}, f, ensure_ascii=False)
    os.replace(tmp, path)


def load_org_profile(user_id: int) -> str:
    path = _path(user_id)
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("org_info", "")
    except Exception:
        return ""
