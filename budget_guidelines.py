"""Справочник рекомендаций для составления бюджета грантовых проектов.

Содержит реальные цифры (в USD) для столицы и регионов:
- Зарплаты, гонорары
- Мероприятия, логистика
- Админ-расходы
- Рекомендуемые доли админ-расходов по размерам проектов
"""

BUDGET_DEFAULTS = {
    "admin_overhead": {
        "office_rent": {"capital": 200, "region": 100},      # $ в месяц
        "office_transport": {"capital": 50, "region": 25},   # водители, логистика
        "utilities": {"capital": 50, "region": 20},          # свет, вода, отопление
        "communications": {"capital": 50, "region": 20},     # интернет, телефон
        "office_supplies": {"capital": 30, "region": 10},    # канцтовары
    },
    "consultant_fees": {
        "beginner": 100,      # $/день
        "experienced": 150,   # $/день
        "expert": 250,        # $/день
    },
    "activities": {
        "coffee": {"capital": 5, "region": 3},               # на человека
        "meals": {"capital": 7, "region": 5},                # обед на человека
        "transport_flight": 100,                             # авиабилет туда-обратно
        "transport_local": 50,                               # такси/маршрутка на человека
        "perdiem": {"capital": 14, "region": 12},            # командировочные в день
        "supplies": {"capital": 2, "region": 1},             # канцтовары на мероприятие
        "handouts": {"capital": 3, "region": 2},             # раздатка на человека
        "certificates": {"capital": 1, "region": 1},         # сертификат на человека
    },
    "publications": {
        "brochure": {"capital": 1, "region": 0.5},           # цветной экз
        "book": {"capital": 3, "region": 1.5},               # за экземпляр
    },
    "bank_fees_percent": 0.2,  # 0.1-0.3% от общей суммы
}

# Рекомендуемая доля админ-расходов по размеру проекта (в процентах)
ADMIN_SHARE_RECOMMENDATIONS = {
    20000: 10,      # до $20k → 10%
    100000: 15,     # до $100k → 15%
    float('inf'): 20,  # выше $100k → 20%
}

def get_admin_share_recommendation(budget_amount: float) -> int:
    """Вернуть рекомендуемую долю админ-расходов (%) для суммы бюджета."""
    for threshold, share in sorted(ADMIN_SHARE_RECOMMENDATIONS.items()):
        if budget_amount <= threshold:
            return share
    return 20

def get_location_hint(location: str) -> str:
    """Текстовая подсказка для выбранной локации."""
    if location == "capital":
        return "📍 Столица (более высокие расходы)"
    elif location == "region":
        return "📍 Регион (более скромные расходы)"
    return ""

def format_guideline_hint(location: str) -> str:
    """Подсказка пользователю при выборе локации."""
    return (
        "Это влияет на рекомендуемые суммы для аренды, ЗП и логистики.\n"
        "Столица: ~2x выше регионов (Бишкек, Алматы)\n"
        "Регион: остальные города"
    )
