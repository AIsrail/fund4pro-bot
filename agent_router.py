"""Единый агентный роутер (v3 редизайн).

Принимает ЛЮБОЕ сообщение пользователя, нормализует FSM-сессию
(включая прозрачную миграцию сессий из старой версии FSM без потери
данных организации, донора и бюджета) и направляет в agent_engine.

Никаких потерянных апдейтов: любые старые состояния (например,
ProjectFlow:budget_discussion) автоматически мигрируют в Flow.active,
а устаревшие inline-кнопки перехватываются и преобразуются в осмысленные
действия для агента.
"""

import base64
import io
import logging
import os
import tempfile

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import agent_engine
import project_memory
from typing_indicator import show_typing, show_working, show_live_progress

router = Router()
logger = logging.getLogger("fund4pro.agent_router")

WELCOME = (
    "Привет! Я помогаю разрабатывать грантовые проекты и бизнес-планы по "
    "методологии «3 деревьев». Пиши свободно, как консультанту — расскажи "
    "об организации, доноре, идее, я сам буду спрашивать, чего не хватает. "
    "Важно: я могу ошибаться — обязательно перепроверяй все данные и цифры "
    "перед подачей."
)

import re

START_WORDS = {
    "начни", "начать", "хочу начать", "давай начнём", "давай начнем",
    "старт", "/start", "начни заново", "начать заново", "заново",
    "с начала", "сначала", "restart", "сброс", "новый проект",
}


def is_start_command(text: str) -> bool:
    """Определяет команды перезапуска проекта с учётом опечаток и вариаций."""
    t = text.strip().lower()
    if t in START_WORDS:
        return True
    patterns = [
        r"^/?start\b",
        r"^старт\b",
        r"^нач[нч][а-я]*",
        r".*начать заново.*",
        r".*новый проект.*",
        r"^заново$",
        r"^сначала$",
        r"^с\s*начала$",
        r"^сброс$",
    ]
    return any(re.search(p, t) for p in patterns)


class Flow(StatesGroup):
    active = State()


def start_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Разработать проект", callback_data="agentflow:grant")
    kb.button(text="💼 Разработать бизнес-план", callback_data="agentflow:bizplan")
    kb.adjust(1)
    return kb.as_markup()


def restart_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Начать заново", callback_data="agent:restart")
    kb.adjust(1)
    return kb.as_markup()


def quick_reply_keyboard(options: list[str]):
    """Кнопки-подсказки к вопросу агента (см. agent_tools.suggest_quick_replies).
    callback_data хранит только индекс (лимит Telegram 64 байта на
    callback_data) — сам текст варианта читается из FSM-состояния по индексу
    в обработчике ниже."""
    kb = InlineKeyboardBuilder()
    for i, opt in enumerate(options):
        kb.button(text=opt[:64], callback_data=f"qr:{i}")
    kb.adjust(1)
    return kb.as_markup()


def _normalize_session(data: dict) -> dict:
    """Обеспечивает корректную структуру данных для агентного движка.
    Если пользователь пришёл из старой версии FSM (где поля лежали на
    верхнем уровне data: org_info, donor_info, budget_text и т.д.),
    прозрачно собирает их в словарь project_data, чтобы накопленный
    контекст не терялся при переходе."""
    if not isinstance(data, dict):
        data = {}

    project_data = data.setdefault("project_data", {})
    if not isinstance(project_data, dict):
        project_data = {}
        data["project_data"] = project_data

    # Миграция из плоских полей старой FSM
    field_mappings = {
        "org_info": "org_info",
        "donor_info": "donor_info",
        "donor_template": "donor_template",
        "selected_idea": "problem_and_idea",
        "goal_and_objectives": "goal_and_objectives",
        "budget_text": "activities_and_budget",
        "concept_text": "other_notes",
    }
    for old_k, target_k in field_mappings.items():
        val = data.get(old_k)
        if val and isinstance(val, str) and val.strip():
            if target_k not in project_data or not project_data[target_k]:
                project_data[target_k] = val.strip()

    if "flow" not in data:
        data["flow"] = "grant"
    if "ui_language" not in data:
        data["ui_language"] = "ru"
    if "history_openai" not in data or not isinstance(data.get("history_openai"), list):
        data["history_openai"] = []

    return data


@router.channel_post()
async def handle_channel_post(message: Message):
    """Слушает новые посты из канала @connect4_pro и обновляет каталог грантов."""
    text = message.text or message.caption or ""
    if not text.strip():
        return
    post_id = f"connect4_pro/{message.message_id}"
    date_str = message.date.isoformat() if message.date else ""
    try:
        from connect4pro_catalog import register_channel_post
        grant = register_channel_post(text, post_id=post_id, pub_date=date_str)
        if grant:
            logger.info("Saved channel grant announcement: %s", grant.get("title"))
    except Exception as e:
        logger.warning("Error processing channel_post: %s", e)


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(WELCOME, reply_markup=start_keyboard())


@router.callback_query(F.data.startswith("agentflow:"))
async def choose_flow(callback: CallbackQuery, state: FSMContext):
    flow = callback.data.split(":")[1]

    # РЕАЛЬНЫЙ ИНЦИДЕНТ: раньше этот хендлер БЕЗУСЛОВНО обнулял project_data
    # ("я то уже 2-ю неделю даю ему инфо про один и тот же конкурс и про ту
    # же самую организацию") — единственный вход в разработку проекта всегда
    # стартовал с чистого листа, даже если пользователь уже рассказывал
    # об этой же организации/доноре в прошлый раз (в другой день, после
    # рестарта Render, после "Начать заново"). Прежде чем стирать — смотрим,
    # есть ли сохранённый снепшот прошлого проекта ТОГО ЖЕ типа (grant/bizplan).
    last = await project_memory.load_last_project(callback.message.chat.id)
    if last and last.get("flow") == flow and last.get("project_data"):
        await state.set_state(Flow.active)
        await state.update_data(_pending_resume={
            "flow": flow,
            "project_data": last["project_data"],
            "donor_files": last.get("donor_files") or {},
        })
        summary = project_memory.summarize(last["project_data"])
        # РЕАЛЬНАЯ ЖАЛОБА: было только "продолжить этот же проект" / "начать
        # с нуля" — а частый случай "та же организация, но новый донор" не
        # покрывался ни одним из двух: продолжить нельзя (это другой донор
        # и другой проект), а "с нуля" стирал и профиль организации, из-за
        # чего приходилось заново присылать те же данные об организации,
        # которые уже даны 2 недели назад. Третья кнопка держит org_info/
        # org_contacts (в т.ч. «картотеку» контактов — см. document_reader.
        # extract_contact_facts), но сбрасывает всё донор-специфичное.
        msg = (
            f"Нашёл незавершённый проект — {summary}\n\n"
            f"Продолжить его, начать полностью новый проект, или это та же "
            f"организация, но для другого донора?"
        )
        opts = ["1. Продолжить этот проект", "2. Новый проект, та же организация", "3. Начать с чистого листа"]
        await state.update_data(_active_quick_replies_resume=opts)
        kb = InlineKeyboardBuilder()
        kb.button(text=opts[0], callback_data="agent:resume:yes")
        kb.button(text=opts[1], callback_data="agent:resume:sameorg")
        kb.button(text=opts[2], callback_data="agent:resume:no")
        kb.adjust(1)
        await callback.message.answer(msg, reply_markup=kb.as_markup())
        await callback.answer()
        return

    await _start_fresh_flow(callback.message, state, flow)
    await callback.answer()


@router.callback_query(F.data == "agent:resume:yes")
async def resume_project_yes(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pending = data.get("_pending_resume") or {}
    project_data = pending.get("project_data") or {}
    flow = pending.get("flow", "grant")
    donor_files = pending.get("donor_files") or {}
    await state.set_state(Flow.active)
    await state.set_data({
        "flow": flow,
        "ui_language": "ru",
        "project_data": project_data,
        "history_openai": [],
        # РЕАЛЬНЫЙ ИНЦИДЕНТ: без этого project_data["donor_template"] (текст
        # структуры формы) восстанавливался, а сам файл формы донора — нет,
        # export_docx не находил его и откатывался на свободный формат.
        **donor_files,
    })
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    await _ensure_active(state)
    await _run_turn_and_reply(
        callback.message, state,
        "Продолжаем этот проект. Напомни коротко, на чём мы остановились, и спроси, что делать дальше.",
    )


@router.callback_query(F.data == "agent:resume:sameorg")
async def resume_project_same_org(callback: CallbackQuery, state: FSMContext):
    """Тот же заявитель, новый донор: держим org_info/org_contacts (профиль
    организации + «картотека» контактов), сбрасываем всё донор- и
    проект-специфичное (донор, форма донора, проблема, цели, бюджет) и файлы
    старой формы донора — иначе select_donor_form мог бы попытаться заново
    использовать форму ПРОШЛОГО донора для нового проекта."""
    data = await state.get_data()
    pending = data.get("_pending_resume") or {}
    old_project_data = pending.get("project_data") or {}
    flow = pending.get("flow", "grant")
    kept = {
        k: v for k, v in old_project_data.items()
        if k in ("org_info", "org_contacts") and v
    }
    await state.set_state(Flow.active)
    await state.set_data({
        "flow": flow,
        "ui_language": "ru",
        "project_data": kept,
        "history_openai": [],
    })
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    await _ensure_active(state)
    await _run_turn_and_reply(
        callback.message, state,
        "Начинаем новый проект для той же организации — данные об организации сохранены, "
        "повторно спрашивать их не нужно. Спроси про нового донора/конкурс.",
    )


@router.callback_query(F.data == "agent:resume:no")
async def resume_project_no(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pending = data.get("_pending_resume") or {}
    flow = pending.get("flow", "grant")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    await _start_fresh_flow(callback.message, state, flow)


async def _start_fresh_flow(message: Message, state: FSMContext, flow: str) -> None:
    await state.set_state(Flow.active)
    await state.set_data({
        "flow": flow,
        "ui_language": "ru",
        "project_data": {},
        "history_openai": [],
    })
    if flow == "grant":
        msg = (
            "Отлично! Начинаем разработку грантового проекта по методологии «3 деревьев».\n\n"
            "📌 **Шаг 1 из 5: Кто заявитель?**\n"
            "Расскажите о вашей организации или инициативной группе: название, город/регион, сфера деятельности и опыт (можно кратко написать текстом или прислать файл с описанием организации)."
        )
        opts = ["1. Опишу текстом", "2. Прикреплю файл", "3. Мы новая группа"]
    else:
        msg = (
            "Отлично! Начинаем разработку бизнес-плана.\n\n"
            "📌 **Шаг 1 из 5: Кто заявитель/бизнес?**\n"
            "Расскажите о вашем предприятии, ИП или стартапе: сфера деятельности, город, текущий статус."
        )
        opts = ["1. Действующий бизнес", "2. Стартап с нуля", "3. Опишу текстом"]

    await state.update_data(_active_quick_replies=opts)
    await message.answer(msg, reply_markup=quick_reply_keyboard(opts), parse_mode="Markdown")


@router.callback_query(F.data == "agent:restart")
async def restart(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer(WELCOME, reply_markup=start_keyboard())
    await callback.answer()


def _button_text_from_message(callback: CallbackQuery) -> str:
    """Текст кнопки, по которой нажали, из reply_markup исходного сообщения."""
    try:
        for row in callback.message.reply_markup.inline_keyboard:
            for btn in row:
                if btn.callback_data == callback.data:
                    return (btn.text or "").strip()
    except Exception:
        pass
    return ""


@router.callback_query(F.data.startswith("qr:"))
async def handle_quick_reply(callback: CallbackQuery, state: FSMContext):
    """Пользователь тапнул одну из кнопок-подсказок (suggest_quick_replies).
    Отправляем выбранный текст в агента как обычное сообщение — с точки
    зрения диалога это неотличимо от того, если бы пользователь его напечатал."""
    await callback.answer()
    data = await state.get_data()
    options = data.get("_active_quick_replies") or []
    # Текст нажатой кнопки берём из самого сообщения: он всегда совпадает с тем,
    # что видит пользователь. Список в FSM перезаписывается на каждом ходе и
    # пропадает при рестарте/деплое Render (диск бесплатного тарифа стирается),
    # из-за чего кнопка из старого сообщения «устаревала» или давала чужой пункт.
    chosen = _button_text_from_message(callback)
    if not chosen:
        try:
            chosen = options[int(callback.data.split(":", 1)[1])]
        except (ValueError, IndexError):
            await callback.message.answer("Эта кнопка устарела — напиши ответ текстом 👇")
            return
    # Убираем стрелки/пальцы вниз и призывы нажать кнопку, фиксируем выбор в сообщении
    try:
        old_text = callback.message.text or callback.message.caption or ""
        clean_lines = []
        for line in old_text.split("\n"):
            line_str = line.strip()
            if any(marker in line_str for marker in ["👇", "👉", "Нажмите кнопку", "нажмите кнопку", "Жду ваш ответ", "Жду ответ", "Жду выбор"]):
                continue
            clean_lines.append(line)
        cleaned = "\n".join(clean_lines).strip()

        if not cleaned:
            new_text = f"✅ Выбрано: {chosen}"
        else:
            new_text = f"{cleaned}\n\n✅ Выбрано: {chosen}"

        try:
            await callback.message.edit_text(new_text, reply_markup=None)
        except Exception:
            await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _ensure_active(state)
    await _run_turn_and_reply(callback.message, state, chosen)


@router.callback_query()
async def handle_legacy_or_unknown_callback(callback: CallbackQuery, state: FSMContext):
    """Обрабатывает нажатия на inline-кнопки старых сообщений (до миграции
    на агентную архитектуру), чтобы исключить зависания и ошибки
    'Update is not handled'."""
    data = callback.data or ""
    await callback.answer()

    # Если пользователь нажал кнопку согласования бюджета из старой версии
    if "budget:ready" in data or "budget_approve" in data:
        await _ensure_active(state)
        await _run_turn_and_reply(callback.message, state, "Бюджет меня устраивает, давай собирать финальный документ.")
        return

    # Если пользователь нажал отмену/главное меню
    if data in ("mm", "cancel"):
        await state.clear()
        await callback.message.answer(WELCOME, reply_markup=start_keyboard())
        return

    # Любая другая старая кнопка — подсказываем продолжить текстом
    current_state = await state.get_state()
    if not current_state:
        await callback.message.answer(WELCOME, reply_markup=start_keyboard())
    else:
        await callback.message.answer("Кнопка от предыдущего шага устарела. Напиши свой ответ или пожелание прямо сообщением в чат 👇")


async def _ensure_active(state: FSMContext) -> dict:
    """Гарантирует, что состояние переведено в Flow.active, а данные нормализованы."""
    raw = await state.get_data()
    norm = _normalize_session(raw)
    await state.set_state(Flow.active)
    await state.set_data(norm)
    return norm


@router.message(F.document)
async def receive_document(message: Message, state: FSMContext):
    """Документ (org profile, форма донора и т.п.) — извлекаем текст и
    отдаём агенту как обычное текстовое сообщение с пометкой источника."""
    from document_reader import UnsupportedFormatError, extract_text_from_telegram_file, extract_contact_facts

    await _ensure_active(state)

    try:
        async with show_typing(message.bot, message.chat.id):
            doc_text = await extract_text_from_telegram_file(message.bot, message.document)
    except UnsupportedFormatError as e:
        await message.answer(f"⚠️ {e}")
        return
    if not doc_text.strip():
        await message.answer(
            f"⚠️ Не смог извлечь текст из {message.document.file_name} "
            "(файл повреждён, пустой, или это скан без текстового слоя)."
        )
        return

    # РЕАЛЬНАЯ ЖАЛОБА: "бот должен заранее выносить контакты в отдельный
    # data-room по организации, а не полагаться на пересказ модели" — телефон/
    # email/сайт/адрес пропадали из готовой заявки, хотя были в присланном
    # профиле: раньше текст файла шёл в чат единым куском, и то, попадут ли
    # эти факты в org_info, целиком зависело от того, не срежет ли их модель
    # при свободном пересказе в update_project. Теперь — детерминированная
    # (без модели) картотека сбоку: извлекаем регэкспами/по меткам СРАЗУ при
    # загрузке файла, в project_data["org_contacts"], откуда её потом читает
    # _session_summary и (для kv-полей формы донора) fill_form_fields_batch.
    # Гейт "пока донор не известен" — намеренно: как только donor_info уже
    # есть, следующий присланный файл с большей вероятностью САМА форма
    # донора (и извлечение регэкспом по НЕЙ подставило бы КОНТАКТЫ ДОНОРА
    # вместо контактов заявителя — например адрес и телефон офиса ГГФ прямо
    # в шапке их формы). Уже известные факты никогда не перезаписываем —
    # только дополняем пробелы.
    session_data = await state.get_data()
    project_data = session_data.setdefault("project_data", {})
    if not project_data.get("donor_info"):
        try:
            new_facts = extract_contact_facts(doc_text)
        except Exception:
            new_facts = {}
        if new_facts:
            existing = project_data.setdefault("org_contacts", {})
            for k, v in new_facts.items():
                existing.setdefault(k, v)
            await state.set_data(session_data)

    # Если пользователь прислал .docx — сохраняем в кэш для доступности
    # через select_donor_form, но НЕ назначаем автоматически шаблоном.
    # Выбор шаблона — решение модели через инструмент select_donor_form.
    if message.document.file_name and message.document.file_name.lower().endswith(".docx"):
        import os
        from donor_form_cache import CACHE_DIR, save_donor_form
        os.makedirs(CACHE_DIR, exist_ok=True)
        try:
            # Сохраняем через save_donor_form, чтобы файл попал в кэш с нормальным именем.
            # bot.download(..., destination=...) возвращает сам destination
            # (BinaryIO), а не байты — save_donor_form ждёт bytes, отсюда
            # раньше падало с "object supporting the buffer API required" на
            # КАЖДОЙ загрузке .docx (тихо ловилось except ниже и просто не
            # попадало в кэш форм донора).
            buf = await message.bot.download(message.document, destination=io.BytesIO())
            raw = buf.getvalue()
            path = save_donor_form(raw, message.document.file_name)
            # Добавляем в список доступных форм для select_donor_form
            session_data = await state.get_data()
            saved = session_data.get("saved_donor_files", [])
            saved.append({
                "filename": message.document.file_name,
                "path": path,
                "text": doc_text,
                "url": "",
                # РЕАЛЬНЫЙ ИНЦИДЕНТ: этой ветки (пользователь прислал .docx
                # СВОИМ файлом, не через fetch_donor_page по ссылке) не было
                # в исходном фиксе восстановления шаблона после рестарта —
                # там content_b64 добавлялся только для файлов, скачанных
                # fetch_donor_page. Без него export_docx нечем было
                # восстановить файл, когда локальный диск/сессия терялись
                # (после рестарта ИЛИ после "Продолжить проект"), и бот снова
                # тихо откатывался на свободный формат, хотя пользователь
                # УЖЕ прислал форму донора вручную.
                "content_b64": base64.b64encode(raw).decode("ascii"),
            })
            session_data["saved_donor_files"] = saved
            await state.set_data(session_data)
        except Exception as err:
            logger.warning("Failed to save uploaded docx: %s", err)

    caption = (message.caption or "").strip()
    user_text = (
        f"{caption}\n\n" if caption else ""
    ) + f"[Прислан файл {message.document.file_name}]:\n{doc_text[:12000]}"

    await _run_turn_and_reply(message, state, user_text)


@router.message()
async def receive_any_message(message: Message, state: FSMContext):
    """Главный обработчик любых текстовых сообщений.
    Никогда не отбрасывает апдейты: проверяет намерения, подхватывает
    текущий контекст и передаёт ход агенту."""
    raw_text = (message.text or message.caption or "").strip()
    if not raw_text:
        await message.answer("Не увидел текста в этом сообщении — опиши словами, текстом.")
        return

    # Проверка на явный перезапуск
    if is_start_command(raw_text):
        await state.clear()
        await message.answer(WELCOME, reply_markup=start_keyboard())
        return

    # Проверяем, есть ли уже начатый проект
    current_state = await state.get_state()
    session_data = await state.get_data()

    # Если состояния нет и данных нет — показываем приветствие и выбор направления
    if not current_state and not session_data.get("project_data") and not session_data.get("org_info"):
        await message.answer(WELCOME, reply_markup=start_keyboard())
        return

    # Проект уже есть или был — активируем и обрабатываем сообщение
    await _ensure_active(state)

    # Если пользователь прислал цифру ("1", "2", ...), сопоставляем с активными кнопками
    active_opts = session_data.get("_active_quick_replies") or []
    if raw_text.isdigit() and active_opts:
        idx = int(raw_text) - 1
        if 0 <= idx < len(active_opts):
            raw_text = f"Выбираю вариант {raw_text}: {active_opts[idx]}"

    await _run_turn_and_reply(message, state, raw_text)


_last_llm_alert_ts = 0.0


async def _alert_owner_llm_down(message: Message) -> None:
    """Владелец узнаёт о том, что все LLM-провайдеры недоступны, сразу, а не
    по жалобам пользователей ("бот тупит"). Не чаще раза в 30 минут."""
    global _last_llm_alert_ts
    import time
    import config

    owner = getattr(config, "OWNER_CHAT_ID", None)
    if not owner and getattr(config, "UNLIMITED_USER_IDS", None):
        owner = min(config.UNLIMITED_USER_IDS)
    if not owner:
        return
    if time.time() - _last_llm_alert_ts < 1800:
        return
    _last_llm_alert_ts = time.time()
    try:
        await message.bot.send_message(
            owner,
            "🚨 fund4pro-bot: ни один LLM-провайдер не отвечает (лимит/баланс). "
            "Проверьте: Anthropic (месячный лимит), OpenAI (кредиты), Gemini (spend cap), "
            "DeepSeek (баланс). Пользователи видят сообщение об этом.",
        )
    except Exception:
        logger.warning("Failed to alert owner about LLM outage", exc_info=True)


async def _run_turn_and_reply(message: Message, state: FSMContext, user_text: str) -> None:
    session = await state.get_data()
    try:
        async with show_live_progress(message, "💭 Думаю..."):
            result = await agent_engine.run_agent_turn(session, user_text)
    except Exception as exc:
        logger.exception("agent turn crashed: %s", exc)
        await message.answer(
            "⚠️ Произошла техническая ошибка при обработке твоего сообщения. "
            "Попробуй повторить его ещё раз — если не поможет, начни заново "
            "кнопкой ниже.",
            reply_markup=restart_keyboard(),
        )
        return

    found_notes = session.pop("_found_data_notes", [])
    await state.set_data(session)  # сохраняем актуализированные данные

    if getattr(result, "llm_unavailable", False):
        await _alert_owner_llm_down(message)

    # Пользователь должен видеть, ЧТО именно бот нашёл в сети и на чём
    # строит цифры — раньше находки уходили только модели (в скрытый
    # результат инструмента) и в документ, а в чате их не было видно.
    if found_notes:
        from telegram_text import send_long
        blocks = []
        for n in found_notes:
            src = "\n".join(f"• {s['title']} — {s['url']}" for s in n.get("sources", [])[:3])
            blocks.append(f"🔎 Поиск: «{n['query']}»\n{n['summary'][:900]}" + (f"\n\nИсточники:\n{src}" if src else ""))
        try:
            await send_long(message, "\n\n———\n\n".join(blocks))
        except Exception:
            logger.warning("Failed to send found-data block", exc_info=True)

    # Долгоживущий снепшот (Redis, переживает /start и "Начать заново") —
    # best-effort, не должен ронять ответ пользователю при сбое Redis.
    # ВАЖНО: message здесь часто приходит из callback.message (нажатие
    # кнопки) — тогда message.from_user был бы САМИМ БОТОМ (это его
    # сообщение), а не пользователем. chat.id в приватном чате с ботом
    # совпадает с id пользователя независимо от того, кто отправитель
    # конкретного объекта message — используем его как надёжный ключ.
    try:
        donor_files = {
            k: session.get(k)
            for k in ("chosen_donor_form", "saved_donor_files", "chosen_donor_form_path")
            if session.get(k)
        }
        await project_memory.save_last_project(
            message.chat.id, session.get("project_data", {}), session.get("flow", "grant"), donor_files,
        )
    except Exception:
        logger.warning("project_memory.save_last_project failed", exc_info=True)

    if getattr(result, "file_attachments", None):
        for att in result.file_attachments:
            p = att.get("path")
            if p and os.path.exists(p):
                try:
                    await message.answer_document(
                        FSInputFile(p),
                        caption=att.get("caption", ""),
                    )
                except Exception as e:
                    logger.warning("Failed to send file attachment %s: %s", p, e)

    if result.document_ready and result.document_text.strip():
        await _send_docx(message, result.document_text, session)

    if result.reply.strip():
        from telegram_text import send_long
        if result.quick_replies:
            # Сохраняем варианты в FSM, чтобы handle_quick_reply мог достать
            # текст по индексу из callback_data (там помещается только число).
            await state.update_data(_active_quick_replies=result.quick_replies)
            await send_long(message, result.reply, reply_markup=quick_reply_keyboard(result.quick_replies))
        else:
            await send_long(message, result.reply)


async def _send_docx(message: Message, text: str, session: dict) -> None:
    from agent_docgen import export_docx

    try:
        async with show_working(message, "📄 Формирую Word-файл..."):
            path, official_template = await export_docx(text, session)
        await message.answer_document(FSInputFile(path))
        if not official_template:
            await message.answer(
                "⚠️ Не смог заполнить именно оригинальный файл формы донора (он не найден в текущей "
                "сессии — например, после перезапуска бота) — выше документ с тем же содержанием, но "
                "собранный в свободном формате. Пришли ещё раз файл шаблона донора, и я соберу заявку "
                "строго в нём, прежде чем отправлять донору."
            )
    except Exception as e:
        logger.exception("Failed to export/send docx: %s", e)
        await message.answer("⚠️ Не удалось собрать .docx файл. Попробуй ещё раз написать 'собери документ'.")
    finally:
        try:
            if 'path' in locals() and os.path.exists(path):
                os.unlink(path)
                os.rmdir(os.path.dirname(path))
        except Exception:
            pass
