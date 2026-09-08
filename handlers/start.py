from aiogram import Router, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from keyboards import start_keyboard, use_saved_org_keyboard, ui_language_keyboard
from org_profile import load_org_profile
from states import ProjectFlow

router = Router()

LANG_NAMES = {"ru": "русском", "ky": "кыргызском", "en": "английском"}

WELCOME = (
    "Привет! Я помогаю разрабатывать грантовые проекты и бизнес-планы "
    "по методологии «3 деревьев». Важно: я могу ошибаться — обязательно "
    "перепроверяй все данные, цифры и факты перед подачей."
)

# РЕАЛЬНЫЙ ИНЦИДЕНТ: пользователь написал "начни" в АКТИВНОМ состоянии
# (например, ожидание данных бюджета) — текст ушёл как обычный ответ в
# текущий LLM-вызов шага вместо распознавания как явного намерения начать
# заново. Заодно тот конкретный LLM-вызов упал (Anthropic API: закончился
# баланс), и пользователь увидел непонятное "не удалось получить ответ от
# модели" вместо приветствия, хотя явно просил именно начать. Этот
# перехватчик работает В ЛЮБОМ состоянии (не привязан к конкретному State,
# похоже на global:restart) и должен запускаться РАНЬШЕ остальных
# текстовых хендлеров — регистрируется первым в bot.py.
START_INTENT_PHRASES = {
    "начни", "начать", "хочу начать", "давай начнём", "давай начнем",
    "хочу разработать", "старт", "/start", "начни заново", "начать заново",
    "заново", "с начала", "начни сначала",
}


def is_start_intent(text: str) -> bool:
    # ТОЧНОЕ совпадение всего (обрезанного) сообщения — не подстрока,
    # иначе ложно сработало бы на реальном тексте пользователя, где эти
    # слова встречаются как часть содержательного ответа (например, донор-
    # информация со словом "начать сотрудничество").
    t = (text or "").strip().lower().rstrip(".!")
    return t in START_INTENT_PHRASES


@router.message(F.text.func(is_start_intent))
async def restart_intent_anywhere(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(WELCOME, reply_markup=start_keyboard())


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(WELCOME, reply_markup=start_keyboard())


@router.callback_query(F.data.startswith("flow:"))
async def choose_flow(callback: CallbackQuery, state: FSMContext):
    flow = callback.data.split(":")[1]  # "grant" | "bizplan"
    await state.update_data(flow=flow, full_version_count=0)
    await state.set_state(ProjectFlow.waiting_ui_language)
    await callback.message.answer(
        "На каком языке тебе удобнее общаться со мной в этом чате?",
        reply_markup=ui_language_keyboard(),
    )
    await callback.answer()


@router.callback_query(ProjectFlow.waiting_ui_language, F.data.startswith("uilang:"))
async def choose_ui_language(callback: CallbackQuery, state: FSMContext):
    ui_lang = callback.data.split(":")[1]
    await state.update_data(ui_language=ui_lang)
    data = await state.get_data()
    flow = data.get("flow", "grant")
    label = "проекта" if flow == "grant" else "бизнеса"

    # РЕАЛЬНЫЙ ИНЦИДЕНТ: раньше здесь отдельным шагом спрашивали "на каком
    # языке должен быть готовый документ" — избыточный вопрос пользователю.
    # Заявка подаётся на языке сайта/формы самого конкурса (это и есть
    # реальное требование донора), а не на произвольно выбранном языке —
    # doc_language теперь определяется автоматически по тексту донора
    # (см. llm.detect_doc_language, вызывается в donor_info.py как только
    # получен текст/форма донора). ui_language остаётся резервным
    # значением для doc_language_clause, если у донора вообще нет текста
    # для анализа.

    # Если для этого пользователя раньше уже сохранялся профиль организации
    # (не стирается сбросом сессии) — предлагаем использовать его вместо
    # того, чтобы заново просить документы/описание.
    saved_org = load_org_profile(callback.from_user.id)
    if saved_org.strip():
        await state.set_state(ProjectFlow.waiting_org_info)
        preview = saved_org.strip()[:200] + ("..." if len(saved_org.strip()) > 200 else "")
        await callback.message.answer(
            f"У меня есть сохранённый профиль организации:\n\n«{preview}»\n\n"
            "Использовать эту информацию?",
            reply_markup=use_saved_org_keyboard(),
        )
        await callback.answer()
        return

    await state.set_state(ProjectFlow.waiting_org_info)
    await callback.message.answer(
        f"📎 Пришли информацию об организации/{label} — документы, профиль, "
        f"опыт работы (docx/pdf принимаются)."
    )
    await callback.answer()


@router.callback_query(ProjectFlow.waiting_org_info, F.data == "org:use_saved")
async def use_saved_org(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    saved_org = load_org_profile(callback.from_user.id)
    await state.update_data(org_info=saved_org)
    data = await state.get_data()
    flow = data.get("flow", "grant")
    label = "доноре/конкурсе" if flow == "grant" else "инвесторе/банке/акселераторе"
    await state.set_state(ProjectFlow.waiting_donor_info)
    await callback.message.answer(f"✅ Использую сохранённый профиль. 📎 Пришли ссылку и/или документы о {label}")


@router.callback_query(ProjectFlow.waiting_org_info, F.data == "org:new")
async def org_new(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    flow = data.get("flow", "grant")
    label = "проекта" if flow == "grant" else "бизнеса"
    await callback.message.answer(
        f"📎 Пришли информацию об организации/{label} — документы, профиль, "
        f"опыт работы (docx/pdf принимаются)."
    )


# Работает В ЛЮБОМ состоянии (не привязан к конкретному State) — это кнопка
# аварийного выхода, если пользователь застрял (потерял кнопки текущего
# шага, сессия зависла и т.п.) и не хочет разбираться, что нажимать дальше.
@router.callback_query(F.data == "global:restart")
async def global_restart(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer(WELCOME, reply_markup=start_keyboard())
    await callback.answer()


# Fallback: любое сообщение вне активного FSM-состояния (новый пользователь,
# открывший бота без нажатия системной кнопки /start, либо пользователь,
# завершивший предыдущий флоу и написавший что-то без контекста). Без этого
# бот молчит на первое сообщение — пользователь видит пустой чат и не
# понимает, что делать. Должен регистрироваться ПОСЛЕДНИМ (после всех
# state-specific хендлеров в других роутерах), чтобы не перехватывать
# сообщения, адресованные активному шагу флоу.
@router.message(StateFilter(None))
async def fallback_welcome(message: Message, state: FSMContext):
    await message.answer(WELCOME, reply_markup=start_keyboard())
