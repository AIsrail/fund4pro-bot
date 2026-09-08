from aiogram.fsm.state import State, StatesGroup


class ProjectFlow(StatesGroup):
    waiting_ui_language = State()          # Выбор языка общения (сразу после flow:grant/bizplan)
    waiting_doc_language = State()         # Выбор языка итогового документа
    waiting_org_info = State()             # Шаг 1
    waiting_donor_info = State()           # Шаг 2
    donor_form_selection = State()         # Шаг 2, ветка "донор дал несколько разных форм"
    eligibility_check = State()            # Шаг 2.5 — критерии отбора донора (Да/Нет подходит)
    idea_defined_check = State()           # Шаг 3 (вкл. генерацию/выбор идей)
    waiting_problem_description = State()  # Шаг 3, ветка "идея уже определена"
    data_availability_check = State()      # Шаг 4
    waiting_data = State()                 # Шаг 4, ветка "есть данные"
    goal_objectives_review = State()             # Бот сам предлагает цель+2-3 задачи, юзер одобряет/правит
    waiting_goal_objectives_revision = State()   # Ветка "поправить"
    concept_speed_check = State()          # Выбор: черновик сейчас / подробнее по мероприятиям
    post_draft_detail_check = State()      # После черновика — детализировать или достаточно
    tree_building = State()                # Диалог по мероприятиям/действиям (единственная стадия)
    budget_discussion = State()            # Согласование бюджета перед финалом (диалог с суммами)
    concept_approval = State()             # Шаг 6
    waiting_concept_revision = State()     # Шаг 6, ветка "нужны правки"
    final_version = State()                # Шаг 7
    waiting_final_revision = State()       # Шаг 7, ветка "версия #2"
