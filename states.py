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


class BudgetStandaloneFlow(StatesGroup):
    """Standalone бюджет (отдельно от основного flow проекта)"""
    waiting_donor_info = State()           # Информация о доноре (ссылка/файл/текст)
    confirm_admin_share = State()          # Подтверждение доли админ-расходов
    ask_duration = State()                 # Срок проекта
    ask_currency = State()                 # Валюта
    ask_local_currency = State()           # Местная валюта, если бюджет в USD
    ask_fx_rate = State()                  # Курс обмена (вручную или поиском)
    waiting_xlsx_template = State()        # Пользователь присылает Excel-шаблон донора в конце
    ask_budget_size = State()              # Размер гранта
    choose_location = State()              # Столица или регион (для рекомендаций)
    ask_admin_team = State()               # Состав команды админ-уровня
    ask_admin_salaries = State()           # Зарплаты админ-персонала
    ask_admin_overhead = State()           # Админ-расходы (офис, транспорт, комм)
    ask_consultant_fees = State()          # Гонорары консультантов
    ask_activities = State()               # Мероприятия
    ask_publications = State()             # Публикации
    ask_equipment = State()                # Оборудование
    ask_contingency = State()              # Непредвиденные расходы
    review_budget = State()                # Согласование финального бюджета
    final_budget = State()                 # Готов к экспорту
