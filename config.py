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

# Временные кнопки-рубильники: скрывают "📋 Разработать проект" (грантовый
# флоу) и/или "💼 Разработать бизнес-план" из стартовой клавиатуры и
# блокируют соответствующий agentflow:*. ИСПРАВЛЕНО (2026-09-23): владелец
# просил приостановить ТОЛЬКО бизнес-планы, грантовый флоу должен оставаться
# включённым — предыдущая версия по ошибке выключала оба. Включить бизнес-планы
# обратно: BIZPLAN_FLOW_ENABLED=true в .env/Render, либо поменять default ниже.
GRANT_FLOW_ENABLED = os.environ.get("GRANT_FLOW_ENABLED", "true").lower() == "true"
BIZPLAN_FLOW_ENABLED = os.environ.get("BIZPLAN_FLOW_ENABLED", "false").lower() == "true"

# Лимит бесплатных ПРОЕКТОВ (не документов — один проект может требовать
# несколько файлов формы донора, все они входят в одну "попытку"). Считает
# и применяет billing.py, вызывается из agent_router._paywall_or_consume в
# момент реального старта нового проекта (не при "Продолжить этот проект").
# Пока ENFORCE_FULL_VERSION_LIMIT=false — лимит не блокирует никого (этап
# свободного тестирования). Включить: ENFORCE_FULL_VERSION_LIMIT=true.
FULL_VERSION_LIMIT = int(os.environ.get("FULL_VERSION_LIMIT", "3"))
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

# --- Платежи (монетизация проектов сверх бесплатного лимита) -------------
# Telegram Stars "из коробки" (не требует внешнего платёжного провайдера) —
# живой путь: agent_router._paywall_or_consume -> payments.send_project_invoice
# -> handlers/payments_handlers.py (pre_checkout/successful_payment) ->
# billing.add_paid_credit -> agent_router.resume_after_payment (продолжает
# именно то действие, на котором пользователь упёрся в лимит).
# Чтобы включить на проде (Render -> Environment):
#   1. PAYMENT_ENABLED=true
#   2. ENFORCE_FULL_VERSION_LIMIT=true (без этого флага лимит не блокирует)
#   3. PROVIDER_TOKEN оставить пустым — оплата пойдёт через Telegram Stars
#      (currency=XTR, не требует /mybots -> Payments provider). Непустой
#      токен переключает на классического провайдера (карты и т.п.).
#   4. При необходимости — PAID_VERSION_PRICE_XTR (цена в Stars).
PAYMENT_ENABLED = os.environ.get("PAYMENT_ENABLED", "false").lower() == "true"
PROVIDER_TOKEN = os.environ.get("PROVIDER_TOKEN", "")
# ~150 Stars ≈ $3 по широко используемому ориентиру ~$0.02/Star (курс
# Telegram по бандлам плавает — если это принципиально, свериться в
# @BotFather -> Payments перед стартом продаж). По грубой прикидке из чата
# ($1.5-4 расход на Anthropic за сложный многофайловый проект) $3 — ближе к
# окупаемости, чем прежние $1, но всё ещё не гарантированный запас.
PAID_VERSION_PRICE_XTR = int(os.environ.get("PAID_VERSION_PRICE_XTR", "150"))

# --- Бесплатный тест бота: раздельные лимиты по ресурсам (25.09.2026) -----
# Владелец: идеи проекта — всегда бесплатно (не ресурс вообще, см.
# llm.generate_ideas, ничем не гейтится). Отдельно:
#   - скачивание ОФИЦИАЛЬНОГО шаблона донора — N раз бесплатно, дальше платно.
#     Списывается ТОЛЬКО после успешного скачивания (agent_engine.py) — чтобы
#     не брать деньги за то, что донор не отдал (антибот, 404 и т.п.).
#   - сборка итогового документа В ФАЙЛ шаблона донора (не текст в чате) —
#     платно с первого раза (FREE_FILE_EXPORTS=0 по умолчанию), независимо
#     от лимита на количество проектов выше: бесплатно — готовый ТЕКСТ
#     заявки в чате, файл по форме донора — отдельная платная услуга.
# Оба, как и ENFORCE_FULL_VERSION_LIMIT, по умолчанию НЕ блокируют — этап
# тестирования; включать явно через ENFORCE_*.
FREE_TEMPLATE_DOWNLOADS = int(os.environ.get("FREE_TEMPLATE_DOWNLOADS", "1"))
ENFORCE_TEMPLATE_DOWNLOAD_LIMIT = os.environ.get("ENFORCE_TEMPLATE_DOWNLOAD_LIMIT", "false").lower() == "true"
# Цена НИЖЕ, чем за проект/экспорт — скачивание шаблона на порядок дешевле в
# ресурсах бота, чем сборка документа. Не привязана к конкретной сумме в
# сомах владельцем — стартовое значение, свериться перед стартом продаж.
PAID_TEMPLATE_DOWNLOAD_PRICE_XTR = int(os.environ.get("PAID_TEMPLATE_DOWNLOAD_PRICE_XTR", "50"))

FREE_FILE_EXPORTS = int(os.environ.get("FREE_FILE_EXPORTS", "0"))
ENFORCE_FILE_EXPORT_PAYMENT = os.environ.get("ENFORCE_FILE_EXPORT_PAYMENT", "false").lower() == "true"
# Озвучено владельцем как "200 сом за проект". Курс сом/Stars не зафиксирован
# нигде в проекте — переведено по тому же ориентиру, что и PAID_VERSION_PRICE_XTR
# (~$0.02/Star): 200 сом ≈ $2.3 при ~87 сом/$ (ОРИЕНТИРОВОЧНО, свериться перед
# стартом продаж) ≈ 115 Stars.
PAID_FILE_EXPORT_PRICE_XTR = int(os.environ.get("PAID_FILE_EXPORT_PRICE_XTR", "115"))

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
