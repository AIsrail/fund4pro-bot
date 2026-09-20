import os

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Порядок фолбэка (по явному запросу пользователя): Anthropic -> OpenAI (ChatGPT)
# -> Gemini -> DeepSeek последним. Каждый провайдер включается только если для
# него задан ключ — отсутствующий ключ просто пропускается в цепочке, ничего
# не падает.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.1")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
FALLBACK_LLM_MODEL = os.environ.get("FALLBACK_LLM_MODEL", "gemini-2.5-flash")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# Модель для генерации контента. См. product-self-knowledge, если понадобится
# свериться с актуальным списком моделей перед деплоем.
LLM_MODEL = "claude-sonnet-5"

# Куда слать служебные уведомления владельцу (например, "все LLM-провайдеры
# недоступны"). Если не задан — берётся первый из UNLIMITED_USER_IDS.
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "").strip() or None

# Лимит бесплатных полных версий документа (Шаг 7).
# ВРЕМЕННО (2026-09): лимит НЕ блокирует правки (см. ENFORCE_FULL_VERSION_LIMIT
# ниже) — идёт этап свободного тестирования продукта. Число ниже используется
# только как порог для мягкого предупреждения пользователю, что лимиты скоро
# появятся по-настоящему. Включить блокировку обратно: ENFORCE_FULL_VERSION_LIMIT=true.
FULL_VERSION_LIMIT = int(os.environ.get("FULL_VERSION_LIMIT", "2"))
ENFORCE_FULL_VERSION_LIMIT = os.environ.get("ENFORCE_FULL_VERSION_LIMIT", "false").lower() == "true"

# ID пользователей Telegram, для которых лимит бесплатных версий (выше) не
# применяется даже когда ENFORCE_FULL_VERSION_LIMIT=true — например, владелец
# бота во время тестирования/итерации.
# Список через запятую в .env: UNLIMITED_USER_IDS=506123531,111222333
UNLIMITED_USER_IDS = {
    int(uid.strip())
    for uid in os.environ.get("UNLIMITED_USER_IDS", "").split(",")
    if uid.strip().isdigit()
}

# --- Платежи (заготовка, ВЫКЛЮЧЕНО) --------------------------------------
# Оплата сознательно отложена. Инфраструктура (payments.py, ветки в
# keyboards.py/final_version.py) уже на месте — включается одной переменной
# окружения, когда будет готов провайдер (ожидается примерно через месяц):
#   1. Подключить платёжного провайдера в @BotFather (/mybots -> Payments)
#      и получить PROVIDER_TOKEN.
#   2. Прописать PROVIDER_TOKEN и PAYMENT_ENABLED=true в .env.
#   3. Задать цену через PAID_VERSION_PRICE_XTR (в Telegram Stars) или
#      адаптировать payments.py под конкретного провайдера (Stripe и т.п.).
# Пока PAYMENT_ENABLED=false — вся платёжная ветка неактивна, поведение
# бота идентично версии без оплаты (лимит просто скрывает кнопку правок).
PAYMENT_ENABLED = os.environ.get("PAYMENT_ENABLED", "false").lower() == "true"
PROVIDER_TOKEN = os.environ.get("PROVIDER_TOKEN", "")
PAID_VERSION_PRICE_XTR = int(os.environ.get("PAID_VERSION_PRICE_XTR", "199"))

# --- Хранилище FSM --------------------------------------------------------
# Если задан REDIS_URL — состояния переживают рестарт процесса (важно для
# продакшена: без этого любой деплой/краш обнуляет все незавершённые
# диалоги пользователей). Если не задан — используется MemoryStorage,
# как раньше (ок для локальной разработки).
REDIS_URL = os.environ.get("REDIS_URL", "")

# --- Красные флаги и антиИИ-стиль ----------------------------------------
# Максимум автоматических циклов "нашли красные флаги -> сам исправил" перед
# тем, как всё равно показать документ пользователю (чтобы не зациклиться
# и не сжигать бюджет на LLM-вызовы бесконечно).
RED_FLAG_AUTOFIX_ATTEMPTS = int(os.environ.get("RED_FLAG_AUTOFIX_ATTEMPTS", "1"))
