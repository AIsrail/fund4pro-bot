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
from budget_donor import (
    analyze_donor_for_budget,
    extract_urls,
    find_budget_docx_tables,
    pick_budget_xlsx,
)
from budget_guidelines import BUDGET_DEFAULTS, get_admin_share_recommendation
from fx import get_usd_rate, normalize_currency
from keyboards import (
    budget_admin_confirm_keyboard,
    budget_all_usd_keyboard,
    budget_donor_next_keyboard,
    budget_export_keyboard,
    budget_fx_keyboard,
    budget_location_keyboard,
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


def _section_prompt(key: str, d: dict) -> str:
    loc = d.get("location", "capital")
    cur = d.get("currency", "USD")
    where = "столица" if loc == "capital" else "регион"
    ov = BUDGET_DEFAULTS["admin_overhead"]
    act = BUDGET_DEFAULTS["activities"]
    pub = BUDGET_DEFAULTS["publications"]
    fees = BUDGET_DEFAULTS["consultant_fees"]
    usd_note = "\n(ориентиры в долларах США; в итоговом бюджете пересчитаю по курсу)" if cur != "USD" else ""

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
            f"🏢 Админ-расходы (ориентиры на месяц, {where}, USD)\n\n"
            f"• аренда офиса: {_money(_pick(ov['office_rent'], loc))}\n"
            f"• транспорт для офиса: {_money(_pick(ov['office_transport'], loc))}\n"
            f"• коммунальные услуги: {_money(_pick(ov['utilities'], loc))}\n"
            f"• связь и интернет: {_money(_pick(ov['communications'], loc))}\n"
            f"• канцтовары: {_money(_pick(ov['office_supplies'], loc))}\n\n"
            "Напишите свои цифры или «по ориентирам». Если офис не нужен "
            "(работаете удалённо / из офиса организации) — «Продолжить»."
        )
    if key == "consultant_fees":
        return (
            "🎓 Гонорары консультантов и экспертов\n\n"
            f"Ориентиры за день (USD): начинающий — {fees['beginner']}, опытный — "
            f"{fees['experienced']}, профи — 250–300 и выше.\n\n"
            "Кого привлекаете, какого уровня и на сколько дней? Если никого — «Продолжить»."
        )
    if key == "activities":
        return (
            f"🎯 Мероприятия (тренинги, встречи, форумы), ориентиры на 1 человека, {where}, USD:\n\n"
            f"• кофе-брейк: {_money(_pick(act['coffee'], loc))}\n"
            f"• обед: {_money(_pick(act['meals'], loc))}\n"
            f"• командировочные в день: {_money(_pick(act['perdiem'], loc))}\n"
            f"• канцтовары: {_money(_pick(act['supplies'], loc))}, раздатка: "
            f"{_money(_pick(act['handouts'], loc))}, сертификат: {_money(_pick(act['certificates'], loc))}\n"
            f"• транспорт бенефициаров: авиабилет туда-обратно ~{act['transport_flight']}, "
            f"такси или маршрутка ~{act['transport_local']}\n\n"
            "Сколько мероприятий, сколько участников, сколько дней, где проходят "
            "и нужен ли участникам проезд/проживание?"
        )
    if key == "publications":
        return (
            "📚 Публикации и печать\n\n"
            f"Ориентиры за экземпляр ({where}, USD): цветной буклет — "
            f"{_money(_pick(pub['brochure'], loc))}, книга/брошюра — {_money(_pick(pub['book'], loc))}.\n\n"
            "Что и каким тиражом печатаете? Если ничего — «Продолжить»."
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


def _summary_of_donor(d: dict, urls_total: int, page_ok: bool, names: list[str]) -> str:
    a = d.get("donor_analysis") or {}
    lines = ["✅ Принял."]
    if urls_total:
        lines.append(
            "Страницу донора прочитал." if page_ok else
            "⚠️ Страницу открыть не получилось (защита от ботов или пустой ответ) — "
            "вставьте сюда нужный текст или пришлите файл."
        )
    if names:
        lines.append("Нашёл файлы: " + ", ".join(names[:6]))
    if d.get("donor_xlsx_b64"):
        lines.append(
            f"📊 Excel-шаблон бюджета: {d.get('donor_xlsx_name')} — в конце впишу бюджет "
            "именно в него: шаблон не меняю, заполняю только пустые ячейки."
        )
    elif d.get("donor_budget_table"):
        lines.append(
            "Нашёл таблицу бюджета внутри заявки (Word) — повторю её статьи. Сам Word-файл "
            "я пока не заполняю: бюджет пришлю текстом по структуре этой таблицы."
        )
    if a.get("admin_share_pct") is not None:
        lines.append(f"Админ-расходы по донору: не более {a['admin_share_pct']:g}%.")
    if a.get("contingency") == "forbidden":
        lines.append("Непредвиденные расходы донор не допускает.")
    elif a.get("contingency") == "allowed":
        lines.append("Непредвиденные расходы донор допускает.")
    if a.get("ineligible_costs"):
        lines.append(f"Не финансируется: {a['ineligible_costs']}")
    if a.get("currency") or a.get("max_grant"):
        bits = []
        if a.get("max_grant"):
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


async def _ingest_donor_text(message: Message, state: FSMContext, text: str) -> None:
    from donor_scrape import try_scrape_donor_forms

    data = await state.get_data()
    context = data.get("donor_context", "") + f"\n\n[Сообщение пользователя]\n{text}"
    updates: dict = {}
    urls = extract_urls(text)
    names: list[str] = []
    page_ok = False

    async with show_working(message, "🔎 Читаю материалы донора и ищу бюджетные требования..."):
        for url in urls[:2]:
            try:
                forms, page_text = await try_scrape_donor_forms(url)
            except Exception:
                logger.warning("try_scrape_donor_forms failed for %s", url, exc_info=True)
                forms, page_text = [], ""
            if page_text:
                page_ok = True
                context += f"\n\n[Страница {url}]\n{page_text[:7000]}"
            for f in forms:
                names.append(f.get("filename", "?"))
                if f.get("text"):
                    context += f"\n\n[Файл донора: {f.get('filename')}]\n{f['text'][:4000]}"
                fn = (f.get("filename") or "").lower()
                if fn.endswith(".docx") and f.get("content") and not data.get("donor_budget_table"):
                    table = find_budget_docx_tables(f["content"])
                    if table:
                        updates["donor_budget_table"] = table
            xlsx = pick_budget_xlsx(forms)
            if xlsx:
                updates["donor_xlsx_b64"] = base64.b64encode(xlsx["content"]).decode("ascii")
                updates["donor_xlsx_name"] = xlsx["filename"]
        await state.update_data(donor_context=context[:MAX_DONOR_CONTEXT], **updates)
        await _refresh_analysis(state)

    data = await state.get_data()
    await message.answer(
        _summary_of_donor(data, len(urls), page_ok, names),
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
    import document_reader as dr

    name = message.document.file_name or "file"
    lower = name.lower()
    if lower.endswith((".doc", ".xls", ".ppt")):
        await message.answer("Старый формат Office я не читаю. Пересохраните в .docx / .xlsx / .pdf и пришлите снова.")
        return
    if not lower.endswith((".xlsx", ".docx", ".pdf")):
        await message.answer("Принимаю .xlsx, .docx и .pdf. Или вставьте текст сообщением.")
        return
    content = await _download_document(message, message.document)
    if content is None:
        return

    updates: dict = {}
    text = ""
    try:
        if lower.endswith(".xlsx"):
            text = dr._extract_xlsx(content)
            updates["donor_xlsx_b64"] = base64.b64encode(content).decode("ascii")
            updates["donor_xlsx_name"] = name
        elif lower.endswith(".docx"):
            text = dr._extract_docx(content)
            table = find_budget_docx_tables(content)
            if table:
                updates["donor_budget_table"] = table
        else:
            text = dr._extract_pdf(content)
    except Exception:
        logger.warning("donor file extraction failed for %s", name, exc_info=True)
    if not text.strip() and "donor_xlsx_b64" not in updates:
        await message.answer(
            "Не получилось прочитать текст из файла (возможно, это скан). "
            "Вставьте нужный текст сообщением."
        )
        return

    data = await state.get_data()
    context = data.get("donor_context", "") + f"\n\n[Файл пользователя: {name}]\n{text[:6000]}"
    await state.update_data(donor_context=context[:MAX_DONOR_CONTEXT], **updates)
    async with show_working(message, "🔎 Читаю файл и ищу бюджетные требования..."):
        await _refresh_analysis(state)
    data = await state.get_data()
    await message.answer(_summary_of_donor(data, 0, False, [name]), reply_markup=budget_donor_next_keyboard())


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


async def _ask_duration(message: Message, state: FSMContext) -> None:
    await state.set_state(S.ask_duration)
    await message.answer("Какой срок проекта? Например: «12 месяцев» или «с сентября по декабрь 2026».")


@router.callback_query(StateFilter(S.confirm_admin_share), F.data == "budget:admin_ok")
async def admin_share_confirmed(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    share = (data.get("donor_analysis") or {}).get("admin_share_pct")
    await state.update_data(donor_admin_share=share)
    await _ask_duration(callback.message, state)


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
    await _ask_duration(message, state)


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
        reply_markup=budget_skip_keyboard(),
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


@router.callback_query(StateFilter(*SECTION_STATES), F.data == "budget:skip")
async def skip_section(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _store_and_advance(callback.message, state, None)


# ============================================================================
# 4. Генерация и согласование
# ============================================================================

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
    if d.get("donor_budget_table"):
        donor_bits.append(
            "Таблица бюджета из заявки донора — повтори её структуру и названия статей:\n" + d["donor_budget_table"]
        )
    if d.get("donor_xlsx_b64"):
        try:
            from xlsx_patch import extract_xlsx_grid
            grid = extract_xlsx_grid(base64.b64decode(d["donor_xlsx_b64"]), max_chars=5000)
            donor_bits.append(
                "Бюджет будет вписан в Excel-шаблон донора — строй статьи так, чтобы они ложились в его "
                "колонки (название, количество, ставка, период…). Структура шаблона:\n" + grid
            )
        except Exception:
            logger.warning("could not render donor xlsx grid for brief", exc_info=True)
    donor_block = ("\n\n" + "\n\n".join(donor_bits)) if donor_bits else ""

    return (
        "Составь построчный бюджет проекта по данным интервью ниже и предложи его на согласование.\n\n"
        f"Донор / требования: {(d.get('donor_context') or '').strip()[:1500] or na}\n"
        f"Срок проекта: {d.get('project_duration', na)}\n"
        f"{money}\n"
        f"Сумма гранта: {size:g} {cur}\n"
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


async def _show_export_menu(message: Message, state: FSMContext, text: str) -> None:
    data = await state.get_data()
    await message.answer(text, reply_markup=budget_export_keyboard(has_xlsx=bool(data.get("donor_xlsx_b64"))))


@router.callback_query(StateFilter(S.review_budget), F.data == "budget:done")
async def budget_done(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(S.final_budget)
    await _show_export_menu(
        callback.message, state,
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


@router.callback_query(StateFilter(S.final_budget), F.data == "budget_export:revise")
async def revise_final_budget(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(S.review_budget)
    await callback.message.answer("Что поменять в бюджете? Напишите текстом.")


@router.callback_query(StateFilter(S.final_budget), F.data == "budget_export:upload")
async def ask_xlsx_template(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(S.waiting_xlsx_template)
    await callback.message.answer(
        "Пришлите Excel-шаблон бюджета донора (.xlsx). Я впишу бюджет только в пустые "
        "ячейки, остальное в шаблоне не трону. Передумали — напишите «отмена»."
    )


@router.message(StateFilter(S.waiting_xlsx_template), F.document)
async def receive_xlsx_template(message: Message, state: FSMContext):
    name = message.document.file_name or "template.xlsx"
    if not name.lower().endswith(".xlsx"):
        await message.answer("Нужен файл .xlsx. Если у вас .xls — пересохраните в .xlsx и пришлите снова.")
        return
    content = await _download_document(message, message.document)
    if content is None:
        return
    await state.update_data(donor_xlsx_b64=base64.b64encode(content).decode("ascii"), donor_xlsx_name=name)
    await state.set_state(S.final_budget)
    await deliver_budget_xlsx(message, state)


@router.message(StateFilter(S.waiting_xlsx_template), TEXT)
async def cancel_xlsx_upload(message: Message, state: FSMContext):
    await state.set_state(S.final_budget)
    await _show_export_menu(message, state, "Хорошо. Что дальше?")


@router.callback_query(StateFilter(S.final_budget), F.data == "budget_export:xlsx")
async def export_xlsx(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await deliver_budget_xlsx(callback.message, state)


async def deliver_budget_xlsx(message: Message, state: FSMContext) -> None:
    """Вписывает согласованный бюджет в Excel-шаблон донора и отправляет файл.
    Платный ресурс file_export; вызывается и после оплаты (agent_router.
    resume_after_file_export_payment)."""
    from excel_fill import fill_xlsx_template_report, generate_budget_cell_mapping
    from xlsx_patch import extract_xlsx_grid

    chat_id = message.chat.id
    data = await state.get_data()
    b64, name = data.get("donor_xlsx_b64"), data.get("donor_xlsx_name") or "budget.xlsx"
    budget_text = data.get("budget_text", "")
    if not b64 or not budget_text.strip():
        await _show_export_menu(message, state, "Нет шаблона или готового бюджета — нечего заполнять.")
        return

    allowed = True
    try:
        allowed = await billing.can_use(chat_id, "file_export")
    except Exception:
        logger.warning("billing.can_use(file_export) failed, defaulting to allow", exc_info=True)
    if not allowed:
        import payments

        await state.update_data(_pending_budget_export=True)
        await payments.send_file_export_invoice(message.bot, chat_id)
        await message.answer(
            "Заполненный Excel — платная услуга (текст бюджета остаётся бесплатным). "
            "После оплаты пришлю файл сразу."
        )
        return

    content = base64.b64decode(b64)
    try:
        async with show_working(message, "📊 Вписываю бюджет в Excel-шаблон донора..."):
            structure = extract_xlsx_grid(content)
            mapping = await generate_budget_cell_mapping(structure, budget_text)
            filled, applied, skipped, replaced = (
                fill_xlsx_template_report(content, mapping) if mapping else (content, [], [], [])
            )
    except Exception:
        logger.exception("deliver_budget_xlsx failed")
        applied, skipped, replaced, filled = [], [], [], content
    if not applied:
        await _show_export_menu(
            message, state,
            "⚠️ Не смог уверенно определить, в какие ячейки вписывать цифры (структура шаблона "
            "нестандартная). Деньги не списаны. Перенесите цифры вручную из бюджета выше.",
        )
        return

    out_name = name if name.lower().startswith("filled_") else f"filled_{name}"
    caption = (
        f"📊 {name}: вписано ячеек — {len(applied)}. Шаблон донора не менялся: заполнены только "
        "пустые ячейки, формулы и заголовки на месте, итоги пересчитаются при открытии в Excel. "
        "Проверьте цифры перед отправкой донору."
    )
    if replaced:
        caption += f"\nЗаменены примеры/подсказки шаблона: {', '.join(c.split('!')[-1] for c in replaced[:12])}."
    refused = [c.split("!")[-1] for c, why in skipped if "формул" in why or "донора" in why or "объединён" in why]
    if refused:
        caption += f"\nНе трогал (формулы/заголовки/объединённые ячейки): {', '.join(refused[:8])}."
    await message.answer_document(BufferedInputFile(filled, filename=out_name), caption=caption[:1020])
    try:
        await billing.consume(chat_id, "file_export")
    except Exception:
        logger.warning("billing.consume(file_export) failed", exc_info=True)
    await _show_export_menu(message, state, "Что дальше?")
