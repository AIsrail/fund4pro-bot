"""Fallback-хендлер для состояний, где бот ждёт нажатия INLINE-кнопки
(idea_defined_check, data_availability_check, eligibility_check,
donor_form_selection, concept_speed_check, post_draft_detail_check,
concept_approval, final_version и т.п.).

РАНЬШЕ, если пользователь в такой момент присылал текстовое сообщение
вместо нажатия кнопки, для этого не было НИ ОДНОГО обработчика — апдейт
молча помечался aiogram как "not handled", и бот просто ничего не отвечал.
Со стороны выглядело как "бот завис/не работает" — хотя процесс был жив и
соединение с Telegram в порядке, просто на конкретное сообщение
буквально не было кода, который бы на него ответил.

Этот роутер регистрируется ПОСЛЕДНИМ в bot.py — aiogram сначала пробует
более специфичные хендлеры (у которых есть свой @router.message(...) для
конкретного State), и только если ни один не подошёл, срабатывает этот —
так что для состояний с реальным текстовым вводом (waiting_org_info,
waiting_data и т.п.) он никогда не вмешивается."""

from aiogram import Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from keyboards import restart_keyboard
from states import ProjectFlow

router = Router()


@router.message(StateFilter(ProjectFlow))
async def unhandled_text_in_button_state(message: Message, state: FSMContext):
    # Раньше текст говорил "нажми кнопку ВЫШЕ", но кнопка restart_keyboard
    # прикреплена К ЭТОМУ ЖЕ сообщению и физически рендерится Telegram'ом
    # НИЖЕ текста — сообщение противоречило само себе (пользователь видел
    # кнопку снизу, а текст отправлял его искать что-то сверху, которого
    # там нет, если история была прокручена). Убрал направление ("выше") —
    # формулировка не должна зависеть от того, где физически отрисуется
    # кнопка в конкретном клиенте.
    await message.answer(
        "Сейчас жду, что ты нажмёшь кнопку под последним сообщением бота "
        "(не текстовое сообщение) — так бот понимает, что делать дальше. "
        "Если кнопок не видно (пролистал историю) — жми кнопку ниже:",
        reply_markup=restart_keyboard(),
    )
