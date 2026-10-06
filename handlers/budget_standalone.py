"""Отдельный сценарий «Составить бюджет» (кнопка на стартовом экране).

1. Донор: бот открывает ссылку, читает страницу и файлы (или принимает файл /
   текст), через LLM находит админ-долю, допустимость непредвиденных расходов,
   недопустимые расходы, Excel-шаблон бюджета или таблицу бюджета в Word-заявке.
2. Вводные: админ-доля, срок, валюта (+курс обмена), сумма, локация.
3. Интервью по разделам расходов с ориентировочными расценками.
4. Построчный бюджет через llm.generate_budget_proposal, правки текстом.
5. Экспорт: текст бесплатно; вписывание в Excel-шаблон донора — платный
   ресурс file_export (шаблон не меняется, заполняются только пустые ячейки).

Роутер подключается в bot.py ДО agent_router (у того catch-all).
"""

import base64
import io
import logging
import re

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

import billing
import config
import docx_budget_fill
import pdf_budget_fill
from budget_donor import analyze_donor_for_budget, extract_urls
from budget_guidelines import BUDGET_DEFAULTS, get_admin_share_recommendation
from donor_files import discover_page_files, download_direct, sniff_kind
from fx import get_usd_rate, normalize_currency
from keyboards import (
    budget_admin_confirm_keyboard,
    budget_accept_rates_keyboard,
    budget_all_usd_keyboard,
    budget_cofunding_keyboard,
    budget_donor_next_keyboard,
    budget_export_keyboard,
    budget_fx_keyboard,
    budget_grant_options_keyboard,
    budget_location_keyboard,
    budget_overlimit_keyboard,
    budget_ready_keyboard,
    budget_skip_keyboard,
)
from llm import LLMEmptyResponseError, generate_budget_proposal, user_facing_llm_error_message
from states import BudgetStandaloneFlow as S
from telegram_text import send_long
from typing_indicator import show_typing, show_working

router = Router()
logger = logging.getLogger("fund4pro.budget_standalone")

# Текстовые сообщения, не начинающиеся с "/", чтобы /start и другие команды
# доходили до agent_router, а не съедались как ответ на вопрос интервью.
TEXT = F.text & ~F.text.startswith("/")

SKIP_WORDS = {"skip", "нет", "пропустить", "no", "-", "не нужно", "не надо"}
NEXT_WORDS = {"дальше", "далее", "готово", "продолжить", "next", "ок", "ok"}
YES_WORDS = {"да", "верно", "ок", "ok", "yes", "подтверждаю"}
UNKNOWN_WORDS = {"не знаю", "незнаю", "не в курсе", "?", "не помню", "нет данных"}

MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_DONOR_CONTEXT = 20000

# Разделы интервью по порядку: (состояние, ключ в FSM-данных)
SECTIONS = [
    (S.ask_admin_team, "admin_team"),
    (S.ask_admin_salaries, "admin_salaries"),
    (S.ask_admin_overhead, "admin_overhead"),
    (S.ask_consultant_fees, "consultant_fees"),
    (S.ask_activities, "activities"),
    (S.ask_publications, "publications"),
    (S.ask_equipment, "equipment"),
    (S.ask_contingency, "contingency"),
]
SECTION_STATES = [st for st, _ in SECTIONS]


def _pick(pair: dict, loc: str):
    return pair[loc if loc in pair else "capital"]


def _money(x) -> str:
    return f"{x:g}"


def _parse_number(text: str) -> float | None:
    t = (text or "").lower().replace(" ", "")
    t = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", t).replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", t)
    if not m:
        return None
    value = float(m.group(0))
    rest = t[m.end():]
    if rest.startswith(("млн", "m", "mln")):
        value *= 1_000_000
    elif rest.startswith(("тыс", "т.р", "k", "к")):
        value *= 1000
    return value


HINT_KEYS = {"admin_overhead", "consultant_fees", "activities", "publications"}


def _section_prompt(key: str, d: dict) -> str:
    """Сначала спрашиваем ДАННЫЕ пользователя; ориентиры — только по кнопке
    «подскажи» (см. _section_hint)."""
    if key == "admin_team":
        return (
            "👥 Проектная команда\n\n"
            "Сколько человек в команде и кто по должности? Обычно нужны "
            "менеджер проекта и бухгалтер (часто на неполную ставку), "
            "иногда координатор.\n\n"
            "Если проект ведёте только вы — нажмите «Продолжить»."
        )
    if key == "admin_salaries":
        return (
            "💼 Зарплаты команды\n\n"
            "Какие желаемые зарплаты (в месяц или за весь проект), и на какую "
            "долю ставки?\n\n"
            "Налоги в абсолютном большинстве случаев входят в зарплату — "
            "отдельной строкой их просят выносить только в очень крупных "
            "проектах, поэтому указывайте сумму «с налогами»."
        )
    if key == "admin_overhead":
        return (
            "🏢 Админ-расходы\n\n"
            "Какие у вас расходы на содержание офиса в месяц: аренда, транспорт, коммунальные "
            "услуги, связь и интернет, канцтовары? Напишите ваши цифры — какие знаете.\n\n"
            "Если офис не нужен (работаете удалённо / из офиса организации) — «Продолжить». "
            "Если не знаете цифры — «Подскажи ориентиры»."
        )
    if key == "consultant_fees":
        return (
            "🎓 Гонорары консультантов и экспертов\n\n"
            "Кого планируете привлекать, какого уровня (начинающий / опытный / профи), на сколько "
            "дней и по какой ставке? Если никого — «Продолжить». Не знаете ставки — «Подскажи ориентиры»."
        )
    if key == "activities":
        return (
            "🎯 Мероприятия (тренинги, встречи, форумы)\n\n"
            "Сколько мероприятий, сколько участников, сколько дней, где проходят? Нужны ли участникам "
            "проезд и проживание, питание (кофе-брейки, обеды), материалы? Напишите, что знаете, "
            "и ваши цены, если они есть. Не знаете цены — «Подскажи ориентиры»."
        )
    if key == "publications":
        return (
            "📚 Публикации и печать\n\n"
            "Что и каким тиражом печатаете (буклеты, брошюры, книги)? Если у вас есть цена "
            "печати — напишите. Если ничего не печатаете — «Продолжить». Не знаете цены — «Подскажи ориентиры»."
        )
    if key == "equipment":
        return (
            "🖥 Оборудование\n\n"
            "Что нужно купить и по какой цене? Цены здесь только ваши — "
            "не угадываю. Заодно уточните: нужна ли страховка (для дорогого, "
            "не офисного оборудования), доставка, установка, растаможка.\n\n"
            "Если оборудование не нужно — «Продолжить»."
        )
    if key == "contingency":
        return (
            "⚠️ Непредвиденные расходы\n\n"
            "Большинство доноров для проектов до 100 тыс. долларов их не "
            "разрешают или не приветствуют — если только это прямо не "
            "разрешено в требованиях. Донор разрешает такую статью? Если "
            "да — напишите максимальный процент, если нет или не уверены — «Продолжить»."
        )
    return ""


def _section_hint(key: str, d: dict) -> str:
    """Ориентировочные расценки (USD) — показываются, только если пользователь
    попросил подсказку."""
    loc = d.get("location", "capital")
    where = "столица" if loc == "capital" else "регион"
    ov = BUDGET_DEFAULTS["admin_overhead"]
    act = BUDGET_DEFAULTS["activities"]
    pub = BUDGET_DEFAULTS["publications"]
    fees = BUDGET_DEFAULTS["consultant_fees"]
    note = "\n\n(ориентиры в долларах США; в итоговом бюджете пересчитаю по курсу)" if d.get("currency", "USD") != "USD" else ""
    if key == "admin_overhead":
        text = (
            f"💡 Ориентиры на месяц ({where}, USD):\n"
            f"• аренда офиса: {_money(_pick(ov['office_rent'], loc))}\n"
            f"• транспорт для офиса: {_money(_pick(ov['office_transport'], loc))}\n"
            f"• коммунальные услуги: {_money(_pick(ov['utilities'], loc))}\n"
            f"• связь и интернет: {_money(_pick(ov['communications'], loc))}\n"
            f"• канцтовары: {_money(_pick(ov['office_supplies'], loc))}"
        )
    elif key == "consultant_fees":
        text = (
            f"💡 Ориентиры за день (USD): начинающий — {fees['beginner']}, опытный — "
            f"{fees['experienced']}, профи — 250–300 и выше."
        )
    elif key == "activities":
        text = (
            f"💡 Ориентиры на 1 человека ({where}, USD):\n"
            f"• кофе-брейк: {_money(_pick(act['coffee'], loc))}\n"
            f"• обед: {_money(_pick(act['meals'], loc))}\n"
            f"• проживание: {_money(_pick(act['accommodation'], loc))} за ночь с завтраком (местная гостиница)\n"
            f"• командировочные в день: {_money(_pick(act['perdiem'], loc))}\n"
            f"• канцтовары: {_money(_pick(act['supplies'], loc))}, раздатка: "
            f"{_money(_pick(act['handouts'], loc))}, сертификат: {_money(_pick(act['certificates'], loc))}\n"
            f"• проезд: авиабилет туда-обратно ~{act['transport_flight']}, такси или маршрутка ~{act['transport_local']}"
        )
    elif key == "publications":
        text = (
            f"💡 Ориентиры за экземпляр ({where}, USD): цветной буклет — "
            f"{_money(_pick(pub['brochure'], loc))}, книга/брошюра — {_money(_pick(pub['book'], loc))}."
        )
    else:
        return ""
    return text + note + "\n\nНапишите свои цифры или нажмите «Беру ориентиры»."


# ============================================================================
# 1. Донор: ссылка / файл / текст
# ============================================================================

@router.callback_query(F.data == "agentflow:budget_standalone")
async def start_budget_standalone(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await state.update_data(ui_language="ru", flow="budget_standalone")
    await state.set_state(S.waiting_donor_info)
    await callback.message.answer(
        "📊 Составим бюджет вместе.\n\n"
        "Для начала расскажите о доноре. Пришлите любое из:\n"
        "• ссылку на донора или на форму заявки/бюджета — я открою страницу, "
        "прочитаю требования и найду допустимую долю админ-расходов\n"
        "• файл: Excel-шаблон бюджета или заявку (Word/PDF) — у небольших "
        "доноров бюджет обычно внутри заявки\n"
        "• текст: если заявка заполняется через портал, скопируйте сюда его "
        "вопросы о бюджете\n\n"
        "Можно прислать несколько сообщений подряд, затем нажать «Дальше». "
        "Если ничего нет — напишите «нет», составим по общим ориентирам. "
        "(Скриншоты я пока не читаю — вставьте текст.)\n\n"
        "Важно: я могу ошибаться, все цифры перепроверяйте перед подачей."
    )


KIND_LABEL = {"xlsx": "📊 Excel", "docx": "📝 Word", "pdf": "📄 PDF"}
MAX_TEMPLATES = 4
_KIND_PRIORITY = {"xlsx": 1, "docx": 2, "pdf": 3}


def _register_template(templates: list[dict], name: str, content: bytes, kind: str, lenient: bool = False) -> str | None:
    """Добавляет файл в список шаблонов, если в него можно вписать бюджет.
    Возвращает пояснение, почему файл не подошёл (или None)."""
    if kind == "legacy":
        return f"{name}: старый формат .doc/.xls я не читаю — пересохраните в .docx / .xlsx и пришлите снова"
    entry = {"name": name, "kind": kind, "b64": base64.b64encode(content).decode("ascii"), "prio": _KIND_PRIORITY.get(kind, 9)}
    if kind == "xlsx":
        if re.search(r"бюджет|budget", name, re.IGNORECASE):
            entry["prio"] = 0
    elif kind == "docx":
        if not docx_budget_fill.find_budget_tables(content, lenient=lenient):
            return f"{name}: таблицу бюджета в этом Word-файле не нашёл"
    elif kind == "pdf":
        if not pdf_budget_fill.extract_pdf_form_schema(content):
            return f"{name}: PDF без заполняемых полей (скан или плоский) — вписать в него автоматически не смогу"
    else:
        return None
    templates[:] = [t for t in templates if t["name"] != name] + [entry]
    templates.sort(key=lambda t: t["prio"])
    del templates[MAX_TEMPLATES:]
    return None


def _file_text(kind: str, content: bytes, ready_text: str = "") -> str:
    if ready_text:
        return ready_text
    import document_reader as dr

    try:
        if kind == "docx":
            return dr._extract_docx(content)
        if kind == "xlsx":
            return dr._extract_xlsx(content)
        if kind == "pdf":
            return dr._extract_pdf(content)
    except Exception:
        logger.warning("text extraction failed for %s file", kind, exc_info=True)
    return ""


def _summary_of_donor(d: dict, urls_total: int, page_ok: bool, names: list[str], notes: list[str]) -> str:
    a = d.get("donor_analysis") or {}
    templates = d.get("donor_templates") or []
    lines = ["✅ Принял."]
    if urls_total:
        lines.append(
            "Страницу донора прочитал." if page_ok else
            "⚠️ Страницу открыть не получилось (защита от ботов или пустой ответ)."
        )
    if names:
        lines.append("Скачал файлы: " + ", ".join(names[:8]))
    elif urls_total:
        lines.append(
            "⚠️ Файлов формы на странице найти не удалось. Пришлите форму сюда сообщением "
            "(Word, Excel или PDF), либо прямую ссылку на файл или на Google Docs."
        )
    for t in templates:
        what = {
            "xlsx": "Excel-шаблон бюджета",
            "docx": "Word-заявка, нашёл в ней таблицу бюджета",
            "pdf": "PDF-форма с заполняемыми полями",
        }[t["kind"]]
        lines.append(f"{KIND_LABEL[t['kind']]}: {t['name']} — {what}.")
    if templates:
        lines.append(
            "В конце впишу бюджет прямо в файл донора: заполняю только пустые ячейки/поля, "
            "заголовки и остальной текст не меняю."
        )
    for n in notes:
        lines.append(f"⚠️ {n}.")
    if a.get("admin_share_pct") is not None:
        lines.append(f"Админ-расходы по донору: не более {a['admin_share_pct']:g}%.")
    cf = a.get("cofunding") or {}
    if cf.get("required") is True:
        bits = []
        if cf.get("min_pct"):
            bits.append(f"не менее {cf['min_pct']:g}% {_cofunding_basis_ru(cf)}")
        if cf.get("types"):
            bits.append("виды: " + ", ".join(COFUND_TYPE_RU[t] for t in cf["types"]))
        lines.append("Со-вклад заявителя требуется" + (": " + "; ".join(bits) if bits else "") + ".")
    if a.get("contingency") == "forbidden":
        lines.append("Непредвиденные расходы донор не допускает.")
    elif a.get("contingency") == "allowed":
        lines.append("Непредвиденные расходы донор допускает.")
    if a.get("ineligible_costs"):
        lines.append(f"Не финансируется: {a['ineligible_costs']}")
    if len(a.get("grant_options") or []) >= 2:
        lines.append("Варианты гранта: " + "; ".join(
            o["label"] + (f" — до {o['max_grant']:g}" if o.get("max_grant") else "") for o in a["grant_options"]) + ".")
    if a.get("currency") or (a.get("max_grant") and len(a.get("grant_options") or []) < 2):
        bits = []
        if a.get("max_grant") and len(a.get("grant_options") or []) < 2:
            bits.append(f"грант до {a['max_grant']:g}")
        if a.get("currency"):
            bits.append(f"валюта {a['currency']}")
        lines.append("По документам: " + ", ".join(bits) + ".")
    lines.append("\nМожете прислать ещё ссылку, файл или текст — или нажмите «Дальше».")
    return "\n".join(lines)


async def _refresh_analysis(state: FSMContext) -> None:
    data = await state.get_data()
    found = await analyze_donor_for_budget(data.get("donor_context", ""))
    previous = data.get("donor_analysis") or {}
    for key, value in previous.items():
        if found.get(key) in (None, "", "unknown") and value not in (None, "", "unknown"):
            found[key] = value
    await state.update_data(donor_analysis=found)


async def _collect_files(url: str) -> tuple[str, list[dict]]:
    """(текст страницы, [{name, content, kind, text}]) — прямая ссылка на файл,
    Google Docs/Drive, форма на странице (в том числе без расширения в ссылке)."""
    from donor_scrape import try_scrape_donor_forms

    direct = await download_direct(url)
    if direct:
        return "", [direct]
    try:
        forms, page_text = await try_scrape_donor_forms(url)
    except Exception:
        logger.warning("try_scrape_donor_forms failed for %s", url, exc_info=True)
        forms, page_text = [], ""
    files = []
    for f in forms:
        content = f.get("content") or b""
        kind = sniff_kind(content) if content else None
        if kind:
            files.append({"name": f.get("filename") or "donor_form", "content": content, "kind": kind, "text": f.get("text", "")})
    if not any(f["kind"] in ("docx", "xlsx", "pdf") for f in files):
        try:
            files += await discover_page_files(url)
        except Exception:
            logger.warning("discover_page_files failed for %s", url, exc_info=True)
    return page_text, files


async def _ingest_donor_text(message: Message, state: FSMContext, text: str) -> None:
    data = await state.get_data()
    context = data.get("donor_context", "") + f"\n\n[Сообщение пользователя]\n{text}"
    templates: list[dict] = list(data.get("donor_templates") or [])
    urls = extract_urls(text)
    names: list[str] = []
    notes: list[str] = []
    page_ok = False

    async with show_working(message, "🔎 Открываю ссылку, скачиваю формы и ищу бюджетные требования..."):
        for url in urls[:2]:
            page_text, files = await _collect_files(url)
            if page_text:
                page_ok = True
                context += f"\n\n[Страница {url}]\n{page_text[:7000]}"
            seen_hashes = set()
            for f in files:
                key = hash(f["content"])
                if key in seen_hashes:
                    continue
                seen_hashes.add(key)
                names.append(f["name"])
                body = _file_text(f["kind"], f["content"], f.get("text", ""))
                if body:
                    context += f"\n\n[Файл донора: {f['name']}]\n{body[:4000]}"
                note = _register_template(templates, f["name"], f["content"], f["kind"])
                if note:
                    notes.append(note)
        known_urls = list(dict.fromkeys(list(data.get("donor_urls") or []) + urls[:2]))
        await state.update_data(donor_context=context[:MAX_DONOR_CONTEXT], donor_templates=templates, donor_urls=known_urls)
        await _refresh_analysis(state)

    data = await state.get_data()
    await message.answer(
        _summary_of_donor(data, len(urls), page_ok, names, notes),
        reply_markup=budget_donor_next_keyboard(),
    )


@router.message(StateFilter(S.waiting_donor_info), F.photo)
async def donor_info_photo(message: Message, state: FSMContext):
    await message.answer(
        "Скриншоты я пока не читаю. Скопируйте текст вопросов о бюджете сюда "
        "(или пришлите ссылку / файл)."
    )


async def _download_document(message: Message, document) -> bytes | None:
    if (document.file_size or 0) > MAX_FILE_BYTES:
        await message.answer("Файл слишком большой (больше 15 МБ). Пришлите файл поменьше или вставьте текст.")
        return None
    file = await message.bot.get_file(document.file_id)
    buf = io.BytesIO()
    await message.bot.download_file(file.file_path, destination=buf)
    return buf.getvalue()


@router.message(StateFilter(S.waiting_donor_info), F.document)
async def donor_info_file(message: Message, state: FSMContext):
    name = message.document.file_name or "file"
    content = await _download_document(message, message.document)
    if content is None:
        return
    kind = sniff_kind(content)
    if kind not in ("docx", "xlsx", "pdf", "legacy"):
        await message.answer("Принимаю Word (.docx), Excel (.xlsx) и PDF. Или вставьте текст сообщением.")
        return

    templates: list[dict] = list((await state.get_data()).get("donor_templates") or [])
    note = _register_template(templates, name, content, kind)
    text = _file_text(kind, content) if kind != "legacy" else ""
    if not text.strip() and not (kind == "xlsx" or any(t["name"] == name for t in templates)):
        await message.answer(
            (note + ". " if note else "") +
            "Текст из файла прочитать не получилось (возможно, это скан). Вставьте нужный текст сообщением."
        )
        return
    data = await state.get_data()
    context = data.get("donor_context", "") + (f"\n\n[Файл пользователя: {name}]\n{text[:6000]}" if text else "")
    await state.update_data(donor_context=context[:MAX_DONOR_CONTEXT], donor_templates=templates)
    async with show_working(message, "🔎 Читаю файл и ищу бюджетные требования..."):
        await _refresh_analysis(state)
    data = await state.get_data()
    await message.answer(
        _summary_of_donor(data, 0, False, [name], [note] if note else []),
        reply_markup=budget_donor_next_keyboard(),
    )


@router.message(StateFilter(S.waiting_donor_info), TEXT)
async def receive_donor_info(message: Message, state: FSMContext):
    text = message.text.strip()
    low = text.lower().rstrip(".!")
    if low in SKIP_WORDS or low in NEXT_WORDS:
        await _go_admin_share(message, state)
        return
    await _ingest_donor_text(message, state, text)


@router.callback_query(StateFilter(S.waiting_donor_info), F.data == "budget:donor_next")
async def donor_next(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _go_admin_share(callback.message, state)


# ============================================================================
# 2. Вводные: админ-доля, срок, валюта и курс, сумма, локация
# ============================================================================

async def _go_admin_share(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    options = (data.get("donor_analysis") or {}).get("grant_options") or []
    if len(options) >= 2 and not data.get("grant_option_done"):
        await _ask_grant_option(message, state, options)
        return
    await _ask_admin_share(message, state)


async def _ask_grant_option(message: Message, state: FSMContext, options: list[dict]) -> None:
    await state.set_state(S.choose_grant_option)
    lines = []
    for o in options:
        amount = f" — до {o['max_grant']:g}" if o.get("max_grant") else ""
        lines.append(f"• {o['label']}{amount}")
    await message.answer(
        "Донор предлагает несколько вариантов гранта:\n" + "\n".join(lines) +
        "\n\nНа какой вариант готовим бюджет? Нажмите кнопку (или напишите свой вариант).",
        reply_markup=budget_grant_options_keyboard(options),
    )


async def _apply_grant_option(message: Message, state: FSMContext, chosen: dict | None, custom: str = "") -> None:
    data = await state.get_data()
    analysis = dict(data.get("donor_analysis") or {})
    if chosen:
        if chosen.get("max_grant"):
            analysis["max_grant"] = chosen["max_grant"]
        if chosen.get("admin_share_pct") is not None:
            analysis["admin_share_pct"] = chosen["admin_share_pct"]
        label = chosen["label"]
    else:
        analysis["max_grant"] = None
        label = custom or "вариант не выбран"
    await state.update_data(donor_analysis=analysis, chosen_grant_option=label, grant_option_done=True)
    await _ask_admin_share(message, state)


@router.callback_query(StateFilter(S.choose_grant_option), F.data.startswith("budget_grant:"))
async def grant_option_chosen(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    key = callback.data.split(":", 1)[1]
    options = ((await state.get_data()).get("donor_analysis") or {}).get("grant_options") or []
    chosen = options[int(key)] if key.isdigit() and int(key) < len(options) else None
    await _apply_grant_option(callback.message, state, chosen)


@router.message(StateFilter(S.choose_grant_option), TEXT)
async def grant_option_typed(message: Message, state: FSMContext):
    await _apply_grant_option(message, state, None, custom=message.text.strip()[:120])


async def _ask_admin_share(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    analysis = data.get("donor_analysis") or {}
    share = analysis.get("admin_share_pct")
    await state.set_state(S.confirm_admin_share)
    if share is not None:
        quote = f" («{analysis['admin_share_quote']}»)" if analysis.get("admin_share_quote") else ""
        await message.answer(
            f"В материалах донора нашёл: админ-расходы не более {share:g}%{quote}.\n\n"
            "Верно? Нажмите «Верно» или напишите другое число.",
            reply_markup=budget_admin_confirm_keyboard(),
        )
    else:
        await message.answer(
            "В материалах донора долю админ-расходов не нашёл. Какую максимальную "
            "долю допускает донор, в %? Обычно от 0 до 20%, реже до 30% (для "
            "очень крупных проектов).\n\nЕсли не знаете — напишите «не знаю», "
            "подскажу ориентир по размеру проекта."
        )


COFUND_TYPE_RU = {"cash": "денежный", "material": "материальный", "intangible": "нематериальный"}
_PCT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")


def _cofunding_required(d: dict) -> dict | None:
    cf = (d.get("donor_analysis") or {}).get("cofunding") or {}
    return cf if cf.get("required") is True else None


def _cofunding_basis_ru(cf: dict) -> str:
    return {"total": "от общей стоимости проекта", "grant": "от суммы гранта"}.get(cf.get("basis"), "от бюджета")


async def _after_admin_share(message: Message, state: FSMContext) -> None:
    """Со-вклад считаем ТОЛЬКО если его требует донор (или пользователь сам
    попросит — через свободный текст/правки, см. _build_brief)."""
    cf = _cofunding_required(await state.get_data())
    if cf:
        await _ask_cofunding(message, state, cf)
    else:
        await _ask_duration(message, state)


async def _ask_cofunding(message: Message, state: FSMContext, cf: dict) -> None:
    await state.set_state(S.ask_cofunding)
    if cf.get("min_pct") and cf.get("max_pct"):
        share = f"от {cf['min_pct']:g}% до {cf['max_pct']:g}%"
    elif cf.get("min_pct"):
        share = f"не менее {cf['min_pct']:g}%"
    else:
        share = "(доля в материалах донора не названа)"
    types = ", ".join(COFUND_TYPE_RU[t] for t in cf.get("types") or []) or "виды не уточнены"
    quote = f"\n«{cf['quote']}»" if cf.get("quote") else ""
    await message.answer(
        f"🤝 Со-вклад заявителя\n\nДонор требует со-вклад: {share} {_cofunding_basis_ru(cf)}. "
        f"Допустимые виды: {types}.{quote}\n\n"
        "Какой процент и в какой форме вы готовы внести? Например: «30%: часть зарплаты менеджера, "
        "админ-расходы, ноутбук б/у». Если не знаете, как распределить — нажмите кнопку, "
        "распределю сам (зарплата персонала, админ-расходы, транспорт и проживание участников, "
        "оборудование по рыночной стоимости).",
        reply_markup=budget_cofunding_keyboard(cf.get("min_pct")),
    )


async def _save_cofunding(message: Message, state: FSMContext, pct: float | None, text: str) -> None:
    await state.update_data(cofunding_active=True, cofunding_pct=pct, cofunding_text=text)
    await _ask_duration(message, state)


@router.callback_query(StateFilter(S.ask_cofunding), F.data == "budget:cf_min")
async def cofunding_minimum(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    cf = _cofunding_required(await state.get_data()) or {}
    await _save_cofunding(callback.message, state, cf.get("min_pct"), "пользователь поручил распределить самому")


@router.callback_query(StateFilter(S.ask_cofunding), F.data == "budget:cf_none")
async def cofunding_none(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(cofunding_active=False)
    await _ask_duration(callback.message, state)


@router.message(StateFilter(S.ask_cofunding), TEXT)
async def receive_cofunding(message: Message, state: FSMContext):
    text = message.text.strip()
    if text.lower().rstrip(".!") in SKIP_WORDS | {"не нужен", "не нужно"}:
        await state.update_data(cofunding_active=False)
        await _ask_duration(message, state)
        return
    m = _PCT_RE.search(text)
    pct = float(m.group(1).replace(",", ".")) if m else _parse_number(text) if text.replace(".", "").replace(",", "").isdigit() else None
    cf = _cofunding_required(await state.get_data()) or {}
    if pct is not None and cf.get("min_pct") and pct < cf["min_pct"]:
        await message.answer(
            f"⚠️ По документам донора минимум {cf['min_pct']:g}%. Продолжаю с вашей цифрой — проверьте её перед подачей."
        )
    await _save_cofunding(message, state, pct, text)


async def _ask_duration(message: Message, state: FSMContext) -> None:
    await state.set_state(S.ask_duration)
    await message.answer("Какой срок проекта? Например: «12 месяцев» или «с сентября по декабрь 2026».")


@router.callback_query(StateFilter(S.confirm_admin_share), F.data == "budget:admin_ok")
async def admin_share_confirmed(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    share = (data.get("donor_analysis") or {}).get("admin_share_pct")
    await state.update_data(donor_admin_share=share)
    await _after_admin_share(callback.message, state)


@router.message(StateFilter(S.confirm_admin_share), TEXT)
async def receive_admin_share(message: Message, state: FSMContext):
    text = message.text.strip().lower()
    data = await state.get_data()
    found = (data.get("donor_analysis") or {}).get("admin_share_pct")
    if text in YES_WORDS and found is not None:
        await state.update_data(donor_admin_share=found)
    elif text in UNKNOWN_WORDS or text in SKIP_WORDS:
        await state.update_data(donor_admin_share=None)
    else:
        value = _parse_number(text)
        if value is None or not (0 <= value <= 100):
            await message.answer("Не понял. Напишите число от 0 до 100 (например 15) или «не знаю».")
            return
        await state.update_data(donor_admin_share=value)
    await _after_admin_share(message, state)


@router.message(StateFilter(S.ask_duration), TEXT)
async def receive_duration(message: Message, state: FSMContext):
    await state.update_data(project_duration=message.text.strip())
    await state.set_state(S.ask_currency)
    data = await state.get_data()
    hint = ""
    donor_cur = (data.get("donor_analysis") or {}).get("currency")
    if donor_cur:
        hint = f"\n(В документах донора указана валюта {donor_cur}.)"
    await message.answer(
        "В какой валюте составить бюджет? Например: доллары, евро, сом, тенге, рубль." + hint
    )


async def _ask_size(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    cur = data.get("currency", "USD")
    max_grant = (data.get("donor_analysis") or {}).get("max_grant")
    hint = f"\n(По документам донора грант до {max_grant:g}.)" if max_grant else ""
    await state.set_state(S.ask_budget_size)
    await message.answer(
        f"Какая общая сумма гранта в {cur} (или максимум по условиям донора)? "
        f"Например: 50000 или «50 тыс».{hint}"
    )


async def _ask_fx(message: Message, state: FSMContext, code: str) -> None:
    await state.update_data(fx_currency=code)
    await state.set_state(S.ask_fx_rate)
    await message.answer(
        f"Мои ориентиры по расценкам в долларах, поэтому нужен курс обмена: "
        f"сколько {code} за 1 доллар?\n\nНапишите курс числом (например 87.5) "
        "или нажмите кнопку — я найду его в интернете. Это черновик, "
        "курс потом можно поправить.",
        reply_markup=budget_fx_keyboard(),
    )


@router.message(StateFilter(S.ask_currency), TEXT)
async def receive_currency(message: Message, state: FSMContext):
    code = normalize_currency(message.text)
    await state.update_data(currency=code, fx_rate=None, fx_currency=None, fx_source=None)
    if code == "USD":
        await state.set_state(S.ask_local_currency)
        await message.answer(
            "Бюджет будет в долларах. Свои цифры (зарплаты, цены) вы называете "
            "тоже в долларах или в местной валюте? Если в местной — напишите её "
            "(сом, тенге, рубль…), пересчитаю по курсу.",
            reply_markup=budget_all_usd_keyboard(),
        )
    else:
        await _ask_fx(message, state, code)


@router.callback_query(StateFilter(S.ask_local_currency), F.data == "budget:fx_none")
async def all_in_usd(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _ask_size(callback.message, state)


@router.message(StateFilter(S.ask_local_currency), TEXT)
async def receive_local_currency(message: Message, state: FSMContext):
    code = normalize_currency(message.text)
    if code == "USD" or message.text.strip().lower() in SKIP_WORDS:
        await _ask_size(message, state)
        return
    await state.update_data(local_currency=code)
    await _ask_fx(message, state, code)


@router.callback_query(StateFilter(S.ask_fx_rate), F.data == "budget:fx_search")
async def fx_search(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    code = data.get("fx_currency") or data.get("currency")
    async with show_working(callback.message, f"🔎 Ищу курс USD → {code}..."):
        found = await get_usd_rate(code)
    if not found:
        await callback.message.answer("Курс найти не получилось. Напишите его числом (сколько " f"{code} за 1 доллар).")
        return
    rate, source = found
    await state.update_data(fx_rate=rate, fx_source=source)
    await callback.message.answer(
        f"Нашёл: 1 USD ≈ {rate:g} {code} (источник: {source}). Если у вас другой "
        "курс — поправите на этапе правок бюджета."
    )
    await _ask_size(callback.message, state)


@router.message(StateFilter(S.ask_fx_rate), TEXT)
async def receive_fx_rate(message: Message, state: FSMContext):
    rate = _parse_number(message.text)
    if not rate or rate <= 0:
        await message.answer("Не понял курс. Напишите число (сколько единиц валюты за 1 доллар) или нажмите «Найти курс самому».")
        return
    await state.update_data(fx_rate=rate, fx_source="указан пользователем")
    await _ask_size(message, state)


@router.message(StateFilter(S.ask_budget_size), TEXT)
async def receive_budget_size(message: Message, state: FSMContext):
    size = _parse_number(message.text)
    if not size or size <= 0:
        await message.answer("Не понял сумму. Напишите число, например 50000 или «50 тыс».")
        return
    await state.update_data(budget_size=size)
    data = await state.get_data()
    max_grant = (data.get("donor_analysis") or {}).get("max_grant")
    if max_grant and data.get("currency") == (data.get("donor_analysis") or {}).get("currency", data.get("currency")) and size > max_grant:
        await message.answer(
            f"⚠️ По документам донора для выбранного варианта максимум {max_grant:g}. "
            "Продолжаю с вашей суммой — проверьте её перед подачей."
        )
    admin_share = data.get("donor_admin_share")
    if admin_share is None:
        # Пороги заданы в долларах — пересчитываем сумму в USD, если курс известен.
        cur, rate = data.get("currency", "USD"), data.get("fx_rate")
        size_usd = size / rate if cur != "USD" and rate and data.get("fx_currency") == cur else size
        admin_share = get_admin_share_recommendation(size_usd)
        await state.update_data(admin_share_is_recommended=True)
        await message.answer(
            f"Доля админ-расходов не указана, беру ориентир {admin_share}% "
            "(10% для проектов до 20 тыс. долларов, до 15% — до 100 тыс.). "
            "Если у донора другой лимит — скажите на этапе правок."
        )
    await state.update_data(admin_share=admin_share)
    await state.set_state(S.choose_location)
    await message.answer(
        "Где будет реализован проект? От этого зависят ориентиры по ценам.",
        reply_markup=budget_location_keyboard(),
    )


@router.callback_query(StateFilter(S.choose_location), F.data.startswith("budget_loc:"))
async def set_location(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(location=callback.data.split(":")[1])
    await _ask_section(callback.message, state, 0)


# ============================================================================
# 3. Разделы интервью
# ============================================================================

async def _ask_section(message: Message, state: FSMContext, idx: int) -> None:
    st, key = SECTIONS[idx]
    data = await state.get_data()
    if key == "contingency" and (data.get("donor_analysis") or {}).get("contingency") == "forbidden":
        await state.update_data(contingency=None)
        await message.answer("⚠️ Непредвиденные расходы пропускаю: по документам донора они не допускаются.")
        await _generate_first_budget(message, state)
        return
    await state.set_state(st)
    prompt = _section_prompt(key, data)
    if key == "contingency" and (data.get("donor_analysis") or {}).get("contingency") == "allowed":
        prompt += "\n\nПо документам донора такая статья допускается."
    await message.answer(
        prompt + f"\n\n(шаг {idx + 1} из {len(SECTIONS)})",
        reply_markup=budget_skip_keyboard(with_hint=key in HINT_KEYS),
    )


async def _store_and_advance(message: Message, state: FSMContext, value: str | None) -> None:
    current = await state.get_state()
    idx = next(i for i, (st, _) in enumerate(SECTIONS) if st.state == current)
    await state.update_data({SECTIONS[idx][1]: value})
    if idx + 1 < len(SECTIONS):
        await _ask_section(message, state, idx + 1)
    else:
        await _generate_first_budget(message, state)


@router.message(StateFilter(*SECTION_STATES), TEXT)
async def receive_section(message: Message, state: FSMContext):
    text = message.text.strip()
    await _store_and_advance(message, state, None if text.lower() in SKIP_WORDS else text)


@router.callback_query(StateFilter(*SECTION_STATES), F.data == "budget:hint")
async def show_section_hint(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    current = await state.get_state()
    key = next(k for st, k in SECTIONS if st.state == current)
    hint = _section_hint(key, await state.get_data())
    if hint:
        await callback.message.answer(hint, reply_markup=budget_accept_rates_keyboard())


@router.callback_query(StateFilter(*SECTION_STATES), F.data == "budget:accept_rates")
async def accept_section_rates(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _store_and_advance(callback.message, state, "по ориентирам (пользователь не знает свои цифры)")


@router.callback_query(StateFilter(*SECTION_STATES), F.data == "budget:skip")
async def skip_section(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _store_and_advance(callback.message, state, None)


# ============================================================================
# 4. Генерация и согласование
# ============================================================================

_COFUND_HOW = (
    "Если пользователь не расписал, чем вносит со-вклад, распредели его так: часть зарплаты персонала "
    "(10-50% строки), админ-расходы (10-100%), транспортные расходы участников (с их согласия или за их "
    "счёт), проживание участников мероприятий (включи в мероприятия статью проживания: местная гостиница "
    "{acc} USD за ночь с завтраком), оборудование (укажи его рыночную стоимость как б/у). Донорскими деньгами "
    "такие позиции не покрывай."
)


def _cofunding_brief(d: dict, size: float, cur: str) -> str:
    acc = BUDGET_DEFAULTS["activities"]["accommodation"]
    acc_txt = f"{acc['capital']} (столица) / {acc['region']} (регион)"
    how = _COFUND_HOW.format(acc=acc_txt)
    table_rule = (
        "Для КАЖДОЙ строки бюджета дай три суммы: «Всего», «Сумма от донора» и «Со-вклад заявителя» "
        "(всего = от донора + со-вклад); сумма столбца «от донора» не должна превышать сумму гранта."
    )
    if not d.get("cofunding_active"):
        return (
            "СО-ВКЛАД: донор его не требует и пользователь не просил — НЕ считай со-вклад и не добавляй "
            "столбцы со-вклада. Только если пользователь сам попросит посчитать со-вклад (в своих ответах "
            "выше или в правках) — тогда добавь столбцы «Сумма от донора» и «Со-вклад заявителя». "
            + how
        )
    cf = (d.get("donor_analysis") or {}).get("cofunding") or {}
    pct = d.get("cofunding_pct")
    basis = cf.get("basis")
    parts = ["СО-ВКЛАД ЗАЯВИТЕЛЯ (требование донора)."]
    if cf.get("min_pct"):
        parts.append(f"Требование: не менее {cf['min_pct']:g}% {_cofunding_basis_ru(cf)}.")
    if cf.get("types"):
        parts.append("Допустимые виды: " + ", ".join(COFUND_TYPE_RU[t] for t in cf["types"]) + ".")
    if cf.get("notes"):
        parts.append(f"Условия донора: {cf['notes']}")
    parts.append(f"Готовность пользователя: {f'{pct:g}%' if pct else 'доля не названа'} — «{d.get('cofunding_text') or ''}».")
    if pct and size:
        cof = size * pct / 100 if basis == "grant" else size * pct / (100 - pct) if pct < 100 else None
        if cof:
            parts.append(
                f"Сумма гранта (запрашиваемая у донора) = {size:g} {cur}; ориентир по со-вкладу ≈ {cof:g} {cur}, "
                f"общая стоимость проекта ≈ {size + cof:g} {cur}."
            )
    parts.append(table_rule)
    parts.append("Зачитывай только допустимые виды со-вклада.")
    parts.append(how)
    parts.append(
        "В конце покажи итоги: сумма от донора, со-вклад, общая стоимость, фактические доли и сравнение с требованием донора."
    )
    return " ".join(parts)


def _build_brief(d: dict) -> str:
    loc = d.get("location", "capital")
    cur = d.get("currency", "USD")
    where = "столица" if loc == "capital" else "регион"
    size = d.get("budget_size", 0)
    admin_share = d.get("admin_share")
    share_src = "рекомендованный ориентир" if d.get("admin_share_is_recommended") else "лимит донора"
    na = "не указано / не планируется"
    analysis = d.get("donor_analysis") or {}

    ov = BUDGET_DEFAULTS["admin_overhead"]
    act = BUDGET_DEFAULTS["activities"]
    pub = BUDGET_DEFAULTS["publications"]
    fees = BUDGET_DEFAULTS["consultant_fees"]
    rates = (
        f"аренда офиса {_pick(ov['office_rent'], loc)}/мес, транспорт офиса "
        f"{_pick(ov['office_transport'], loc)}/мес, коммунальные {_pick(ov['utilities'], loc)}/мес, "
        f"связь {_pick(ov['communications'], loc)}/мес, канцтовары {_pick(ov['office_supplies'], loc)}/мес; "
        f"гонорары за день: начинающий {fees['beginner']}, опытный {fees['experienced']}, профи 250-300+; "
        f"кофе-брейк {_pick(act['coffee'], loc)}, обед {_pick(act['meals'], loc)}, командировочные "
        f"{_pick(act['perdiem'], loc)}/день, канцтовары {_pick(act['supplies'], loc)}, раздатка "
        f"{_pick(act['handouts'], loc)}, сертификат {_pick(act['certificates'], loc)} (на человека); "
        f"проезд бенефициара: авиа туда-обратно ~{act['transport_flight']}, такси/маршрутка ~{act['transport_local']}; "
        f"буклет {_pick(pub['brochure'], loc)}, книга {_pick(pub['book'], loc)} за экземпляр."
    )

    rate, fx_cur = d.get("fx_rate"), d.get("fx_currency")
    if rate and fx_cur:
        if cur == "USD":
            money = (
                f"Бюджет в USD. Курс: 1 USD = {rate:g} {fx_cur} ({d.get('fx_source')}). Цифры, которые "
                f"пользователь назвал без указания валюты, считай в {fx_cur} и пересчитай в USD по этому курсу."
            )
        else:
            money = (
                f"Бюджет в {cur}. Курс: 1 USD = {rate:g} {fx_cur} ({d.get('fx_source')}). Справочные расценки "
                f"ниже даны в USD — пересчитай их в {cur} по этому курсу; цифры пользователя без указания "
                f"валюты — уже в {cur}."
            )
    else:
        money = f"Все суммы в {cur}. Справочные расценки ниже даны в USD (если валюта не USD — пересчитай по разумному курсу и назови его)."

    donor_bits = []
    if analysis.get("ineligible_costs"):
        donor_bits.append(f"Расходы, которые донор НЕ финансирует: {analysis['ineligible_costs']}")
    if analysis.get("budget_notes"):
        donor_bits.append(f"Прочие бюджетные требования донора: {analysis['budget_notes']}")
    templates = d.get("donor_templates") or []
    if templates:
        t = templates[0]
        try:
            raw = base64.b64decode(t["b64"])
            if t["kind"] == "xlsx":
                from xlsx_patch import extract_xlsx_grid
                structure = extract_xlsx_grid(raw, max_chars=5000)
            elif t["kind"] == "docx":
                idx = docx_budget_fill.find_budget_tables(raw, lenient=True)
                structure = docx_budget_fill.render_tables(raw, idx)[:5000]
            else:
                structure = pdf_budget_fill.describe_fields(pdf_budget_fill.extract_pdf_form_schema(raw))[:4000]
            donor_bits.append(
                f"Бюджет будет вписан в файл донора «{t['name']}» ({t['kind']}) — строй статьи так, чтобы они "
                "ложились в его строки/колонки (название, количество, ставка, период, сумма…) и повторяли "
                "названия статей донора. Структура файла:\n" + structure
            )
        except Exception:
            logger.warning("could not render donor template structure for brief", exc_info=True)
    donor_block = ("\n\n" + "\n\n".join(donor_bits)) if donor_bits else ""

    return (
        "Составь построчный бюджет проекта по данным интервью ниже и предложи его на согласование.\n\n"
        f"Донор / требования: {(d.get('donor_context') or '').strip()[:1500] or na}\n"
        f"Срок проекта: {d.get('project_duration', na)}\n"
        f"{money}\n"
        f"Сумма гранта: {size:g} {cur}\n"
        f"Вариант гранта, на который подаёмся: {d.get('chosen_grant_option') or 'единственный / не уточнялся'}\n"
        f"Максимальная доля админ-расходов: {admin_share}% ({share_src}) — не превышай её.\n"
        f"Локация: {where}\n\n"
        f"Команда: {d.get('admin_team') or na}\n"
        f"Зарплаты (налоги входят в зарплату, отдельной строкой не выноси): {d.get('admin_salaries') or na}\n"
        f"Админ-расходы (офис, транспорт, связь, канцтовары): {d.get('admin_overhead') or na}\n"
        f"Гонорары консультантов: {d.get('consultant_fees') or na}\n"
        f"Мероприятия: {d.get('activities') or na}\n"
        f"Публикации: {d.get('publications') or na}\n"
        f"Оборудование (учти страховку, доставку, установку, растаможку, если упомянуты): {d.get('equipment') or na}\n"
        f"Непредвиденные расходы: {d.get('contingency') or 'не включать — донор не разрешает или не подтверждено'}\n\n"
        f"Банковские расходы: заложи 0,1-0,3% от общей суммы бюджета.\n"
        f"Справочные расценки для незаполненных пользователем позиций ({where}, USD): {rates}"
        f"{donor_block}\n\n"
        f"{_cofunding_brief(d, size, cur)}\n\n"
        "Ответы пользователя выше могут содержать данные не по своей теме — разнеси их по нужным "
        "статьям бюджета.\n"
        "Требования к результату: каждая статья построчно (расчёт «кол-во × ставка × период = итог»), "
        "итоговая сумма должна сойтись с суммой гранта и не превышать её, админ-доля не выше лимита; "
        "явно помечай цифры, которые взяты из ориентиров, а не от пользователя; покажи итоги по "
        "категориям и долю админ-расходов; в конце коротко спроси, что скорректировать, и назови, "
        "какие расходы могли быть пропущены (например аудит, перевод, страхование, мониторинг и оценка)."
    )


def _session_for_llm(d: dict) -> dict:
    return {"ui_language": d.get("ui_language", "ru"), "donor_info": (d.get("donor_context") or "")[:6000]}


async def _generate_first_budget(message: Message, state: FSMContext) -> None:
    await state.set_state(S.review_budget)
    data = await state.get_data()
    brief = _build_brief(data)
    try:
        async with show_working(message, "⏳ Составляю бюджет по вашим ответам..."):
            async with show_typing(message.bot, message.chat.id):
                proposal = await generate_budget_proposal(_session_for_llm(data), brief)
    except LLMEmptyResponseError as e:
        await message.answer(user_facing_llm_error_message(e, "Напишите что-нибудь, чтобы попробовать снова."))
        return
    await state.update_data(
        budget_text=proposal,
        budget_history=[
            {"role": "user", "content": brief},
            {"role": "assistant", "content": proposal},
        ],
    )
    await send_long(message, proposal, reply_markup=budget_ready_keyboard())


@router.message(StateFilter(S.review_budget), TEXT)
async def revise_budget(message: Message, state: FSMContext):
    data = await state.get_data()
    history = data.get("budget_history", [])
    if not history:
        await _generate_first_budget(message, state)
        return
    try:
        async with show_typing(message.bot, message.chat.id):
            reply = await generate_budget_proposal(_session_for_llm(data), message.text, history)
    except LLMEmptyResponseError as e:
        await message.answer(user_facing_llm_error_message(e))
        return
    new_history = history + [
        {"role": "user", "content": message.text},
        {"role": "assistant", "content": reply},
    ]
    # Первое сообщение (бриф с данными интервью) держим всегда, остальное — хвост.
    await state.update_data(budget_text=reply, budget_history=[new_history[0]] + new_history[1:][-10:])
    await send_long(message, reply, reply_markup=budget_ready_keyboard())


@router.callback_query(StateFilter(S.review_budget), F.data == "budget:hint_comment")
async def budget_hint_comment(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "Напишите, что поправить, текстом — например «увеличь зарплату менеджера» "
        "или «добавь ещё один тренинг»."
    )


async def _show_export_menu(message: Message, state: FSMContext, text: str, compact: bool = False) -> None:
    data = await state.get_data()
    old = data.get("export_menu_msg")
    if old:
        # Прежнее меню убираем, иначе нужная кнопка тонет среди дублей.
        try:
            await message.bot.edit_message_reply_markup(chat_id=message.chat.id, message_id=old, reply_markup=None)
        except Exception:
            pass
    sent = await message.answer(text, reply_markup=budget_export_keyboard(data.get("donor_templates") or [], compact=compact))
    await state.update_data(export_menu_msg=sent.message_id)


def _budget_meta(d: dict, tpl: dict) -> dict:
    meta = {"currency": d.get("currency", ""), "donor_name": tpl["name"] if tpl.get("kind") != "standalone" else ""}
    if d.get("fx_rate") and d.get("fx_currency"):
        meta["fx"] = f"Курс: 1 USD = {d['fx_rate']:g} {d['fx_currency']} ({d.get('fx_source')})."
    if d.get("admin_share") is not None:
        meta["admin"] = f"Админ-расходы: не более {d['admin_share']:g}% бюджета."
    return meta


@router.callback_query(StateFilter(S.review_budget), F.data == "budget:done")
async def budget_done(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    size = data.get("budget_size") or 0
    # Итог бюджета не должен превышать запрошенный грант — проверяем кодом по таблице статей.
    if size and (data.get("budget_text") or "").strip():
        async with show_working(callback.message, "🔎 Проверяю итоги бюджета..."):
            struct = await docx_budget_fill.extract_budget_struct(data["budget_text"])
        if struct:
            donor_total = struct["total_donor"] if struct.get("has_cofunding") else struct["total"]
            if donor_total > size * 1.01:
                cur = data.get("currency", "")
                await callback.message.answer(
                    f"⚠️ Итог бюджета {docx_budget_fill.fmt_amount(donor_total)} {cur} превышает запрошенный грант "
                    f"{docx_budget_fill.fmt_amount(size)} {cur} на {docx_budget_fill.fmt_amount(donor_total - size)}. "
                    "Скорректируем бюджет или оставить как есть?",
                    reply_markup=budget_overlimit_keyboard(),
                )
                return
    await _finish_budget(callback.message, state)


@router.callback_query(StateFilter(S.review_budget), F.data == "budget:done_force")
async def budget_done_force(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _finish_budget(callback.message, state)


async def _finish_budget(message: Message, state: FSMContext) -> None:
    await state.set_state(S.final_budget)
    await _show_export_menu(
        message, state,
        "✅ Бюджет согласован. Проверьте итоговую сумму и лимит админ-расходов "
        "по условиям донора перед подачей.",
    )


# ============================================================================
# 5. Экспорт
# ============================================================================

@router.callback_query(StateFilter(S.final_budget), F.data == "budget_export:text")
async def export_text(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    await send_long(callback.message, data.get("budget_text", ""))
    from handlers.feedback_handlers import offer_feedback
    await offer_feedback(callback.message, state)


@router.callback_query(StateFilter(S.final_budget), F.data == "budget_export:revise")
async def revise_final_budget(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(S.review_budget)
    await callback.message.answer("Что поменять в бюджете? Напишите текстом.")


@router.callback_query(StateFilter(S.final_budget), F.data == "budget_export:upload")
async def ask_template_upload(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(S.waiting_xlsx_template)
    await callback.message.answer(
        "Пришлите форму донора с бюджетом: Excel (.xlsx), Word (.docx) или PDF с заполняемыми полями. "
        "Я впишу бюджет только в пустые ячейки/поля, остальное не трону. "
        "Передумали — напишите «отмена»."
    )


@router.message(StateFilter(S.waiting_xlsx_template, S.final_budget), F.document)
async def receive_template_file(message: Message, state: FSMContext):
    name = message.document.file_name or "template"
    content = await _download_document(message, message.document)
    if content is None:
        return
    kind = sniff_kind(content)
    if kind not in ("docx", "xlsx", "pdf", "legacy"):
        await message.answer("Нужен Word (.docx), Excel (.xlsx) или PDF. Пришлите другой файл или напишите «отмена».")
        return
    templates: list[dict] = list((await state.get_data()).get("donor_templates") or [])
    note = _register_template(templates, name, content, kind, lenient=True)
    idx = next((i for i, t in enumerate(templates) if t["name"] == name), None)
    if idx is None:
        await state.set_state(S.final_budget)
        await _show_export_menu(
            message, state,
            f"⚠️ {note or 'Файл не подошёл'}. Могу собрать бюджет отдельным Word-документом "
            "(кнопка ниже) — таблицу вы перенесёте в заявку сами, либо пришлите другой файл.",
        )
        return
    await state.update_data(donor_templates=templates)
    await state.set_state(S.final_budget)
    await deliver_budget_template(message, state, idx)


@router.message(StateFilter(S.waiting_xlsx_template), TEXT)
async def cancel_template_upload(message: Message, state: FSMContext):
    await state.set_state(S.final_budget)
    await _show_export_menu(message, state, "Хорошо. Что дальше?")


@router.callback_query(StateFilter(S.final_budget), F.data == "budget_export:doc")
async def export_standalone_doc(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await deliver_budget_template(callback.message, state, -1)


@router.callback_query(StateFilter(S.final_budget), F.data.startswith("budget_export:fill:"))
async def export_into_template(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await deliver_budget_template(callback.message, state, int(callback.data.rsplit(":", 1)[-1]))


async def _fill_xlsx(content: bytes, budget_text: str, meta: dict | None = None) -> dict | None:
    from excel_fill import fill_xlsx_template_report, generate_budget_cell_mapping
    from xlsx_patch import extract_xlsx_grid

    mapping = await generate_budget_cell_mapping(extract_xlsx_grid(content), budget_text)
    if not mapping:
        return None
    filled, applied, skipped, replaced = fill_xlsx_template_report(content, mapping)
    if not applied:
        return None
    refused = [c.split("!")[-1] for c, why in skipped if "формул" in why or "донора" in why or "объединён" in why]
    caption = (
        f"вписано ячеек — {len(applied)}. Шаблон не менялся: заполнены только пустые ячейки, формулы и "
        "заголовки на месте, итоги пересчитаются при открытии в Excel."
    )
    if replaced:
        caption += f"\nЗаменены примеры/подсказки шаблона: {', '.join(c.split('!')[-1] for c in replaced[:12])}."
    if refused:
        caption += f"\nНе трогал (формулы/заголовки/объединённые ячейки): {', '.join(refused[:8])}."
    return {"bytes": filled, "caption": caption}


async def _fill_docx(content: bytes, budget_text: str, meta: dict | None = None) -> dict | None:
    # 1) таблица статей расходов (в том числе вложенная в ячейку-вопрос): статьи
    #    пишутся построчно, недостающие строки добавляются перед «ИТОГО»
    struct = None
    if docx_budget_fill.find_line_item_tables(content):
        struct = await docx_budget_fill.extract_budget_struct(budget_text)
        if struct:
            res = docx_budget_fill.fill_budget_into_docx(content, struct)
            if res:
                filled, rep = res
                cur = struct.get("currency") or (meta or {}).get("currency", "")
                caption = (
                    f"вписано статей — {rep['items']}, добавлено строк — {rep['rows_added']}, "
                    f"итог посчитан: {docx_budget_fill.fmt_amount(rep['total'])} {cur}. "
                    "Остальной текст и таблицы заявки не менялись (другие разделы заявки в этом режиме "
                    "не заполняются)."
                )
                return {"bytes": filled, "caption": caption.replace("  ", " ")}
    # 2) произвольная сетка: модель сопоставляет цифры с адресами ячеек
    indices = docx_budget_fill.find_budget_tables(content) or docx_budget_fill.find_budget_tables(content, lenient=True)
    if not indices:
        return None
    mapping = await docx_budget_fill.map_budget_to_tables(docx_budget_fill.render_tables(content, indices), budget_text)
    if not mapping:
        return None
    filled, applied, skipped, replaced, fixed = docx_budget_fill.fill_docx_budget(content, mapping)
    if not applied:
        return None
    caption = (
        f"вписано ячеек таблицы бюджета — {len(applied)}. Заголовки таблицы и весь остальной текст заявки "
        "не менялись (другие разделы заявки в этом режиме не заполняются)."
    )
    if replaced:
        caption += f"\nЗаменены подсказки шаблона: {', '.join(c.split('!')[-1] for c in replaced[:10])}."
    if fixed:
        caption += f"\nИтоги пересчитал по столбцам: {'; '.join(fixed[:4])}."
    return {"bytes": filled, "caption": caption}


async def _fill_standalone(budget_text: str, meta: dict) -> dict:
    """Отдельный Word-документ с бюджетом — когда вписать в форму донора некуда
    (или пользователь сам попросил): таблицу переносят в заявку вручную."""
    struct = await docx_budget_fill.extract_budget_struct(budget_text)
    notes = []
    if meta.get("donor_name"):
        notes.append(f"Бюджет подготовлен для заявки: {meta['donor_name']}.")
    if meta.get("fx"):
        notes.append(meta["fx"])
    if meta.get("admin"):
        notes.append(meta["admin"])
    notes.append("Цифры ориентировочные — проверьте перед подачей.")
    data = docx_budget_fill.build_budget_docx(struct, budget_text, "Бюджет проекта", notes)
    return {"bytes": data, "caption": "перенесите таблицу в форму донора."}


async def _fill_pdf(content: bytes, budget_text: str, meta: dict | None = None) -> dict | None:
    fields = pdf_budget_fill.extract_pdf_form_schema(content)
    if not fields:
        return None
    answers = await pdf_budget_fill.map_budget_to_pdf_fields(fields, budget_text)
    if not answers:
        return None
    filled, count, non_latin = pdf_budget_fill.fill_pdf_budget(content, fields, answers)
    if not count:
        return None
    caption = f"заполнено полей формы — {count}. Остальные поля и страницы не менялись; итоги проверьте вручную."
    if non_latin:
        caption += "\n⚠️ В части полей кириллица — шрифт PDF-формы может её не отобразить."
    return {"bytes": filled, "caption": caption}


_FILLERS = {"xlsx": _fill_xlsx, "docx": _fill_docx, "pdf": _fill_pdf}
_PROGRESS = {
    "xlsx": "📊 Вписываю бюджет в Excel-шаблон донора...",
    "docx": "📝 Вписываю бюджет в таблицу Word-заявки донора...",
    "pdf": "📄 Заполняю бюджетные поля PDF-формы донора...",
}


async def deliver_budget_template(message: Message, state: FSMContext, idx: int | None = None) -> None:
    """Вписывает согласованный бюджет в файл донора (Excel/Word/PDF) и отправляет
    его. Платный ресурс file_export; вызывается и после оплаты (agent_router.
    resume_after_file_export_payment; индекс тогда лежит в _pending_budget_export)."""
    chat_id = message.chat.id
    data = await state.get_data()
    if idx is None:
        idx = (data.get("_pending_budget_export") or {}).get("idx", 0)
    templates = data.get("donor_templates") or []
    budget_text = data.get("budget_text", "")
    if not budget_text.strip() or idx >= len(templates):
        await _show_export_menu(message, state, "Нет шаблона или готового бюджета — нечего заполнять.")
        return
    tpl = templates[idx] if idx >= 0 else {"name": "Бюджет проекта.docx", "kind": "standalone", "b64": ""}

    # Один бюджет = одно прохождение сценария: первый файл бесплатно, после него
    # любые форматы того же бюджета (Excel/Word/PDF) уже оплачены/бесплатны.
    already = bool(data.get("budget_export_counted"))
    allowed = True
    if not already:
        try:
            allowed = await billing.can_use(chat_id, "budget_export")
        except Exception:
            logger.warning("billing.can_use(budget_export) failed, defaulting to allow", exc_info=True)
    if not allowed:
        import payments

        await state.update_data(_pending_budget_export={"idx": idx})
        await message.answer(
            "Первый бюджет в файле вы уже получили бесплатно. Следующие — "
            f"{config.BUDGET_EXPORT_PRICE_KGS} сом (≈{config.PAID_BUDGET_EXPORT_PRICE_XTR} ⭐), "
            "сейчас тестовый режим, цена может измениться. Текст бюджета в чате — всегда бесплатно. "
            "После оплаты пришлю файл сразу."
        )
        await payments.send_budget_export_invoice(message.bot, chat_id)
        return

    meta = _budget_meta(data, tpl)
    result = None
    fell_back = False
    try:
        if tpl["kind"] != "standalone":
            async with show_working(message, _PROGRESS[tpl["kind"]]):
                result = await _FILLERS[tpl["kind"]](base64.b64decode(tpl["b64"]), budget_text, meta)
        if not result:
            fell_back = tpl["kind"] != "standalone"
            async with show_working(message, "📄 Собираю бюджет отдельным Word-документом..."):
                result = await _fill_standalone(budget_text, meta)
    except Exception:
        logger.exception("deliver_budget_template failed (%s)", tpl["kind"])
    if not result:
        await _show_export_menu(
            message, state,
            "⚠️ Не получилось собрать файл. Деньги не списаны. Бюджет текстом — по кнопке ниже.",
        )
        return

    if fell_back:
        name, out_name = "Бюджет проекта", "Бюджет проекта.docx"
        caption = (
            f"📄 {tpl['name']}: не нашёл в этой форме место, куда можно вписать бюджет, поэтому "
            f"подготовил его отдельным документом — {result['caption']}"
        )
    elif tpl["kind"] == "standalone":
        name, out_name = "Бюджет проекта", "Бюджет проекта.docx"
        caption = f"📄 Бюджет отдельным Word-документом: {result['caption']}"
    else:
        name = tpl["name"]
        out_name = name if name.lower().startswith("filled_") else f"filled_{name}"
        caption = f"{KIND_LABEL[tpl['kind']]} {name}: {result['caption']}\nПроверьте цифры перед отправкой донору."
    await message.answer_document(BufferedInputFile(result["bytes"], filename=out_name), caption=caption[:1020])
    first_free = False
    if not already:
        try:
            first_free = not await _has_paid_credit(chat_id)
            await billing.consume(chat_id, "budget_export")
        except Exception:
            logger.warning("billing.consume(budget_export) failed", exc_info=True)
        await state.update_data(budget_export_counted=True)
    note = ""
    if first_free and config.ENFORCE_BUDGET_EXPORT_PAYMENT and config.PAYMENT_ENABLED:
        note = (
            f"\n\nЭто ваш бесплатный бюджет в файле. Следующие — {config.BUDGET_EXPORT_PRICE_KGS} сом "
            "(тестовый режим, цена может измениться). Другие форматы этого же бюджета — без доплаты."
        )
    from handlers.feedback_handlers import offer_feedback
    await offer_feedback(message, state)
    await _show_export_menu(message, state, "Что дальше?" + note, compact=True)


# ============================================================================
# 6. Переход к заявке (основной сценарий)
# ============================================================================

def _build_handoff(d: dict) -> dict:
    """Всё, что основной сценарий (agent_router) должен знать из бюджета:
    согласованный бюджет, донор и его условия."""
    a = d.get("donor_analysis") or {}
    facts = []
    if d.get("chosen_grant_option"):
        facts.append(f"Вариант гранта: {d['chosen_grant_option']}" + (f" (до {a['max_grant']:g})" if a.get("max_grant") else ""))
    elif a.get("max_grant"):
        facts.append(f"Грант до {a['max_grant']:g}")
    if d.get("admin_share") is not None:
        facts.append(f"Админ-расходы: не более {d['admin_share']:g}%")
    if d.get("budget_size"):
        facts.append(f"Запрошенная сумма: {d['budget_size']:g} {d.get('currency', '')}")
    if d.get("project_duration"):
        facts.append(f"Срок проекта: {d['project_duration']}")
    cf = a.get("cofunding") or {}
    if d.get("cofunding_active"):
        facts.append(f"Со-вклад заявителя: {d.get('cofunding_pct') or 'доля не названа'}% ({d.get('cofunding_text') or ''})")
    elif cf.get("required") is True:
        facts.append("Со-вклад донором требуется")
    if a.get("ineligible_costs"):
        facts.append(f"Не финансируется: {a['ineligible_costs']}")
    if a.get("budget_notes"):
        facts.append(f"Прочие бюджетные требования: {a['budget_notes']}")
    urls = d.get("donor_urls") or []
    return {
        "budget_text": d.get("budget_text", ""),
        "donor_urls": urls,
        "donor_facts": facts,
        "donor_context": (d.get("donor_context") or "")[:3000],
        "currency": d.get("currency", ""),
    }


@router.callback_query(StateFilter(S.final_budget), F.data == "budget_export:application")
async def go_fill_application(callback: CallbackQuery, state: FSMContext):
    """Пользователь хочет заполнить заявку: передаём бюджет и донора в основной
    сценарий (организация -> донор -> ... ) без повторных вопросов о них."""
    await callback.answer()
    data = await state.get_data()
    if not (data.get("budget_text") or "").strip():
        await _show_export_menu(callback.message, state, "Сначала нужен согласованный бюджет.")
        return
    # Убираем кнопки у меню, с которого ушли, чтобы не возвращаться в него случайно.
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    import agent_router

    await agent_router.start_application_from_budget(callback.message, state, _build_handoff(data))


async def _has_paid_credit(chat_id: int) -> bool:
    """Есть ли у пользователя купленный (платный) кредит на бюджет в файле —
    чтобы не называть бесплатным то, за что только что заплатили."""
    try:
        record = await billing._read(chat_id)
        return record["budget_export"].get("paid_credits", 0) > 0
    except Exception:
        return False


async def resume_after_budget_payment(message: Message, state: FSMContext) -> None:
    """Вызывается из handlers/payments_handlers.py после оплаты ещё одного бюджета
    в файле: отдаёт файл, ради которого пользователь упёрся в лимит."""
    pending = (await state.get_data()).get("_pending_budget_export") or {}
    await state.update_data(_pending_budget_export=None)
    await deliver_budget_template(message, state, pending.get("idx", 0))
