from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def start_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Разработать проект", callback_data="flow:grant")
    kb.button(text="💼 Разработать бизнес-план", callback_data="flow:bizplan")
    kb.adjust(1)
    return kb.as_markup()


def ui_language_keyboard() -> InlineKeyboardMarkup:
    """Язык, на котором бот общается с пользователем (вопросы, кнопки,
    подтверждения). Отдельно от языка итогового документа — донор часто
    требует конкретный язык заявки независимо от того, на каком языке
    удобнее вести диалог заявителю."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🇷🇺 Русский", callback_data="uilang:ru")
    kb.button(text="🇰🇬 Кыргызча", callback_data="uilang:ky")
    kb.button(text="🇬🇧 English", callback_data="uilang:en")
    kb.adjust(1)
    return kb.as_markup()


def doc_language_keyboard() -> InlineKeyboardMarkup:
    """Язык итогового документа (может отличаться от языка общения —
    например донор требует английский, а заявителю удобнее кыргызский)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🇷🇺 Русский", callback_data="doclang:ru")
    kb.button(text="🇰🇬 Кыргызча", callback_data="doclang:ky")
    kb.button(text="🇬🇧 English", callback_data="doclang:en")
    kb.adjust(1)
    return kb.as_markup()


def idea_defined_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, есть проблема", callback_data="idea:defined")
    kb.button(text="Нет, нужна помощь", callback_data="idea:need_help")
    kb.adjust(1)
    return kb.as_markup()


def idea_select_keyboard(ideas: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for i, idea in enumerate(ideas):
        preview = idea.strip()
        if len(preview) > 40:
            preview = preview[:37].rstrip() + "..."
        kb.button(text=f"{i + 1}. {preview}", callback_data=f"idea:select:{i}")
    kb.adjust(1)
    return kb.as_markup()


def donor_form_choice_keyboard(labels: list[str]) -> InlineKeyboardMarkup:
    """Донор опубликовал НЕСКОЛЬКО разных форм (например основная заявка +
    форма командировочных расходов) — пользователь выбирает, какую именно
    заполнять как основную заявку."""
    kb = InlineKeyboardBuilder()
    for i, label in enumerate(labels):
        text = label if len(label) <= 45 else label[:42].rstrip() + "..."
        kb.button(text=f"{i + 1}. {text}", callback_data=f"donorform:select:{i}")
    kb.adjust(1)
    return kb.as_markup()


def eligibility_check_keyboard() -> InlineKeyboardMarkup:
    """Да/Нет — подходит ли пользователь под критерии отбора донора,
    найденные на его странице конкурса."""
    kb = InlineKeyboardBuilder()
    kb.button(text="➡️ Продолжить с этим донором", callback_data="eligibility:yes")
    kb.button(text="🔄 Показать другие идеи/доноров", callback_data="eligibility:no")
    kb.adjust(1)
    return kb.as_markup()


def use_saved_org_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, использовать её", callback_data="org:use_saved")
    kb.button(text="✏️ Нет, это другая организация", callback_data="org:new")
    kb.adjust(1)
    return kb.as_markup()


def restart_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Начать заново", callback_data="global:restart")
    kb.adjust(1)
    return kb.as_markup()


def more_or_continue_keyboard(more_label: str = "➕ Есть ещё информация") -> InlineKeyboardMarkup:
    """Показывается после приёма данных на любом intake-шаге вместо
    жёсткого автоматического перехода к следующему вопросу. Раньше бот
    сразу после summary/ответа на вопрос пользователя ПРИНУДИТЕЛЬНО ехал
    дальше по сценарию ('Пришли ссылку о доноре...'), даже если сам же
    только что ответил 'информации не хватает' — получалась каша из двух
    несвязанных реплик подряд. Теперь пользователь (или бот на основе его
    выбора) сам решает, когда переходить дальше."""
    kb = InlineKeyboardBuilder()
    kb.button(text=more_label, callback_data="step:more")
    kb.button(text="➡️ Этого достаточно, дальше", callback_data="step:continue")
    kb.adjust(1)
    return kb.as_markup()


def donor_manual_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📎 Скачаю и пришлю сам", callback_data="donor:manual_upload")
    kb.button(text="🔗 Найду онлайн-форму и скопирую вопросы", callback_data="donor:manual_copy")
    kb.adjust(1)
    return kb.as_markup()


def data_availability_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Есть данные", callback_data="data:have")
    kb.button(text="Нет данных", callback_data="data:none")
    kb.adjust(1)
    return kb.as_markup()


def concept_speed_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Накидай черновик концепта сразу", callback_data="speed:draft")
    kb.button(text="🌳 Хочу подробнее проработать мероприятия", callback_data="speed:full")
    kb.adjust(1)
    return kb.as_markup()


def goal_objectives_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Согласен, продолжаем", callback_data="goal:approve")
    kb.button(text="✏️ Нужно поправить", callback_data="goal:revise")
    kb.adjust(1)
    return kb.as_markup()


def post_draft_detail_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Достаточно, готовь финальную версию", callback_data="detail:done")
    kb.button(text="🔍 Хочу детализировать мероприятия", callback_data="detail:more")
    kb.adjust(1)
    return kb.as_markup()


def tree_stage_keyboard() -> InlineKeyboardMarkup:
    """Опциональная кнопка подтверждения на каждом дереве (Шаг 5, по ТЗ —
    на усмотрение разработчика). Позволяет пользователю самому решить,
    когда переходить к следующему дереву, вместо автодетекта завершения.

    Вторая кнопка — та же подсказка для новых пользователей, что и в
    budget_ready_keyboard: экран с ОДНОЙ кнопкой не намекает, что можно
    просто написать текстом (хотя это уже работает, см. tree_building.py)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Достаточно, идём дальше", callback_data="tree:advance")
    kb.button(text="💬 Есть комментарий / хочу уточнить", callback_data="tree:hint_comment")
    kb.adjust(1)
    return kb.as_markup()


def concept_approval_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобряю, готовь финальную версию", callback_data="concept:approve")
    kb.button(text="✏️ Нужны правки к концепту", callback_data="concept:revise")
    kb.adjust(1)
    return kb.as_markup()


def budget_ready_keyboard() -> InlineKeyboardMarkup:
    # РЕАЛЬНЫЙ ИНЦИДЕНТ: пользователь написал текстом "Составь бюджет до
    # 15 тыс долларов, админ сделай не более 10%" вместо нажатия кнопки —
    # бот корректно это обработал (свободный текст здесь уже принимается),
    # но экран с ОДНОЙ кнопкой не подсказывает новому пользователю, что
    # так вообще можно. Кнопка ниже не меняет логику (сообщение всё равно
    # обрабатывается тем же текстовым хендлером) — только явно показывает,
    # что можно написать свой комментарий/уточнение, а не только нажать.
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Бюджет готов, собери финальную версию", callback_data="budget:done")
    kb.button(text="💬 Есть комментарий / хочу уточнить", callback_data="budget:hint_comment")
    kb.adjust(1)
    return kb.as_markup()


def final_version_keyboard(full_version_count: int, limit_reached: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Финально, всё устраивает", callback_data="final:done")
    if not limit_reached:
        kb.button(text="✏️ Версия #2 — последние правки", callback_data="final:revise")
    kb.adjust(1)
    return kb.as_markup()


def paid_revision_offer_keyboard() -> InlineKeyboardMarkup:
    """Показывается вместо обычной кнопки правок, когда лимит бесплатных
    версий достигнут И оплата включена (config.PAYMENT_ENABLED=true).
    Пока оплата выключена, эта клавиатура нигде не используется — см.
    handlers/final_version.py."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Финально, всё устраивает", callback_data="final:done")
    kb.button(text="💳 Купить ещё одну версию", callback_data="final:pay_revise")
    kb.adjust(1)
    return kb.as_markup()


def red_flags_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔧 Исправь сам", callback_data="redflags:autofix")
    kb.button(text="✏️ Я пришлю правки", callback_data="redflags:manual")
    kb.adjust(1)
    return kb.as_markup()
