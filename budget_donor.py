"""Что нужно знать о доноре для бюджета: админ-доля, допустимость
непредвиденных расходов, недопустимые расходы, шаблон бюджета (Excel или
таблица внутри Word-заявки). Источники — страница донора, найденные на ней
файлы и всё, что прислал пользователь."""

import json
import logging
import re

logger = logging.getLogger("fund4pro.budget_donor")

URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
MAX_CONTEXT_CHARS = 16000

def extract_urls(text: str) -> list[str]:
    return [u.rstrip(".,;") for u in URL_RE.findall(text or "")]


async def analyze_donor_for_budget(context: str) -> dict:
    """Структурированные факты о бюджетных требованиях донора из текста.
    Только то, что ЯВНО написано; при любом сбое — пустой словарь."""
    from llm import call_claude

    if len(context.strip()) < 40:
        return {}
    system_prompt = (
        "Из текста донора (страница конкурса, руководство, форма заявки) "
        "извлеки бюджетные требования. Только то, что ЯВНО написано в тексте; "
        "ничего не угадывай и не выводи по общим знаниям. Верни СТРОГО JSON:\n"
        '{"admin_share_pct": число или null (максимальная допустимая доля '
        'административных / накладных / косвенных расходов в процентах),'
        ' "admin_share_quote": "короткая дословная цитата-основание" или "",'
        ' "max_grant": число или null (если вариант гранта ОДИН),'
        ' "grant_options": [{"label": "название варианта своими словами, как у донора '
        '(например «проекты в одной стране»)", "max_grant": число или null, '
        '"admin_share_pct": число или null (только если для этого варианта указана своя доля)}] — '
        'заполняй ТОЛЬКО если донор явно предлагает несколько вариантов/категорий с разными '
        'суммами или условиями (по одной стране и межстрановые, малые и крупные гранты, '
        'этапы); перечисли ВСЕ явно названные варианты со своими суммами, не выбирай за '
        'пользователя; если вариант один — пустой список [],'
        ' "currency": "код валюты гранта" или null,'
        ' "cofunding": {"required": true | false | null (null — в тексте про со-вклад / '
        'софинансирование / cost share / matching ничего нет), "min_pct": число или null, '
        '"max_pct": число или null (доля со-вклада заявителя в процентах), "basis": '
        '"total" (от общей стоимости проекта) | "grant" (от суммы гранта) | "unknown", '
        '"types": подмножество ["cash", "material", "intangible"] — какие виды со-вклада донор '
        'явно допускает (cash — денежный, material — материальный: оборудование, помещение, '
        'транспорт; intangible — нематериальный: труд, время, экспертиза, волонтёры) — пустой '
        'список, если не указано, "notes": "кратко: что донор пишет про со-вклад (что засчитывает, '
        'суммы, условия)", "quote": "короткая дословная цитата-основание"}'
        ' (если со-вклад нужен, но доля не названа — required: true, min_pct: null),'
        ' "contingency": "allowed" | "forbidden" | "unknown" (непредвиденные '
        'расходы / резерв),'
        ' "ineligible_costs": "кратко: какие расходы донор не финансирует" или "",'
        ' "budget_notes": "кратко: прочие требования к бюджету (формат, '
        'софинансирование, налоги, сроки трат)" или ""}'
    )
    try:
        raw = await call_claude(system_prompt, context[:MAX_CONTEXT_CHARS], max_tokens=1200)
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start:end + 1])
    except Exception as exc:
        logger.warning("analyze_donor_for_budget failed: %s: %s", type(exc).__name__, exc)
        return {}

    def _num(x):
        try:
            v = float(x)
            return v if v >= 0 else None
        except (TypeError, ValueError):
            return None

    share = _num(data.get("admin_share_pct"))
    if share is not None and share > 100:
        share = None
    options = []
    for o in (data.get("grant_options") or []) if isinstance(data.get("grant_options"), list) else []:
        if isinstance(o, dict) and str(o.get("label") or "").strip():
            pct = _num(o.get("admin_share_pct"))
            options.append({
                "label": str(o["label"]).strip()[:90],
                "max_grant": _num(o.get("max_grant")),
                "admin_share_pct": pct if pct is not None and pct <= 100 else None,
            })
    cf = data.get("cofunding") if isinstance(data.get("cofunding"), dict) else {}
    cf_min, cf_max = _num(cf.get("min_pct")), _num(cf.get("max_pct"))
    cofunding = {
        "required": cf.get("required") if isinstance(cf.get("required"), bool) else None,
        "min_pct": cf_min if cf_min is not None and cf_min <= 100 else None,
        "max_pct": cf_max if cf_max is not None and cf_max <= 100 else None,
        "basis": cf.get("basis") if cf.get("basis") in ("total", "grant") else "unknown",
        "types": [t for t in (cf.get("types") or []) if t in ("cash", "material", "intangible")]
        if isinstance(cf.get("types"), list) else [],
        "notes": str(cf.get("notes") or "")[:500],
        "quote": str(cf.get("quote") or "")[:300],
    }
    return {
        "cofunding": cofunding,
        "grant_options": options[:6],
        "admin_share_pct": share,
        "admin_share_quote": str(data.get("admin_share_quote") or "")[:300],
        "max_grant": _num(data.get("max_grant")),
        "currency": (str(data["currency"]).strip().upper()[:10] if data.get("currency") else None),
        "contingency": data.get("contingency") if data.get("contingency") in ("allowed", "forbidden") else "unknown",
        "ineligible_costs": str(data.get("ineligible_costs") or "")[:600],
        "budget_notes": str(data.get("budget_notes") or "")[:600],
    }
