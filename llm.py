"""LLM-слой. Системный промпт теперь включает полную методологию "3 деревьев"
из грантового плейбука (донор-профили, бюджетные правила, антиИИ-дисциплина),
а не конденсированную версию — см. METHODOLOGY_PROMPT ниже. Отдельно
реализованы: red-flags аудит (§1.2) перед выдачей концепта/финалки и
стилистический guard против "ИИ-звучания" (§1.3), применяемый на КАЖДОЙ
генерации текста, а не только в конце.
"""

import asyncio
import logging
import json

from anthropic import AsyncAnthropic

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

import config

logger = logging.getLogger("fund4pro.llm")
_client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

_deepseek_client = None
if getattr(config, "DEEPSEEK_API_KEY", None) and AsyncOpenAI is not None:
    try:
        _deepseek_client = AsyncOpenAI(
            api_key=config.DEEPSEEK_API_KEY,
            base_url="https://api.deepseek.com",
        )
        logger.info("DeepSeek client initialized successfully (model=%s)", config.DEEPSEEK_MODEL)
    except Exception as _de:
        logger.warning("Failed to initialize DeepSeek client: %s", _de)

_fallback_client = None
if getattr(config, "GEMINI_API_KEY", None) and AsyncOpenAI is not None:
    try:
        _fallback_client = AsyncOpenAI(
            api_key=config.GEMINI_API_KEY,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        logger.info("Fallback Gemini client initialized successfully (model=%s)", config.FALLBACK_LLM_MODEL)
    except Exception as _fe:
        logger.warning("Failed to initialize fallback Gemini client: %s", _fe)


# ---------------------------------------------------------------------------
# Методология — полная версия по грантовому плейбуку (не конденсат)
# ---------------------------------------------------------------------------

METHODOLOGY_PROMPT = """
Ты помогаешь разрабатывать грантовые проекты и бизнес-планы по методологии
«3 деревьев» (А. Исраилов, патент Кыргызпатент №4760). Работай строго по
следующей логике.

ГЛАВНЫЙ ПРИНЦИП: "Никогда не пиши то, чего не понимаешь". Перед каждым
разделом уточняй у пользователя недостающее, собирай данные, строй причинно-
следственную цепочку: проблема -> цель -> задачи -> деятельность -> результаты
-> бюджет. Логически несвязная, но красиво написанная заявка — причина №1
отказов доноров, а не слабая идея.

7 ПРИНЦИПОВ:
1. Логика важнее красивого текста — проверяй, что вся цепочка связна.
2. Данные, а не предположения — каждое утверждение о проблеме должно иметь
   источник. Если точных данных нет, НЕ пиши "оценка, требует проверки" или
   любую другую мета-пометку в тексте заявки — вместо этого впиши
   правдоподобную по месту фразу с инлайн-плейсхолдером XYZ прямо внутри
   предложения (см. правило плейсхолдеров ниже), как будто цифра там уже
   есть и её просто нужно подставить.
3. Донор — не банкомат: изучи его реальные приоритеты, не подгоняй язык
   под общие фразы.
4. Устойчивость закладывается с самого начала, не дописывается в конце.
5. Реалистичный бюджет — просить ровно столько, сколько нужно, каждая
   строка обоснована.
6. Сообщество как соавтор, а не получатель — вовлечение целевой аудитории
   в разработку, а не просто "информирование".
7. Верность шаблону донора — если донор дал свою форму, заполняется только
   она, без переформатирования собственным стилем.

МЕТОДОЛОГИЯ "3 ДЕРЕВЬЕВ":
1. Дерево проблем: центральная проблема (негативное состояние) <- корневые
   причины (правило "5 почему", 3-5 штук) -> последствия (3-5 штук). Правило
   зеркалирования: количество причин = количество последствий = количество
   задач = количество результатов (после приоритизации).
2. Дерево целей: механическое переворачивание дерева проблем — каждая
   причина становится задачей, проблема становится целью, последствия
   становятся ожидаемым воздействием. Фильтр приоритизации (3 уровня):
   выбирай только задачи, которые одновременно (а) укладываются в бюджет
   донора, (б) реально выполнимы командой/партнёрами, (в) дают реальную
   пользу благополучателям. Для проектов <$100k — 2-3 задачи, для крупных —
   4-5.
3. Дерево действий (Action Tree) — одно на каждую выбранную задачу, никогда
   не смешивать задачи в одном дереве: мероприятия -> индикаторы -> бюджетная
   строка -> риски (разделяй Prevention/предотвращение и Mitigation/смягчение)
   -> вклад в устойчивость после завершения гранта. Это становится основой
   плана деятельности, бюджета, раздела рисков и M&E.

Для БИЗНЕС-ПЛАНА дерево проблем/целей используется только для обоснования
"почему рынку нужен этот бизнес" (problem statement), а дерево действий
становится планом деятельности и фидит статьи OPEX/CAPEX в финмодель
(TAM/SAM/SOM, P&L, точка безубыточности, при необходимости NPV/IRR).
Реформулируй факты из инвесторской рамки (рост выручки, маржа, exit) в
донорскую рамку там, где это грантовая, а не чисто коммерческая заявка:
рабочие места, кто получает пользу, что останется после гранта.

БЮДЖЕТНЫЕ ПРАВИЛА: административные расходы ≤10-20% гранта, M&E = 5-10%
бюджета, контингенси 3-5%. Красные флаги в бюджете: зарплаты превышают
прямые программные расходы, безликие строки "прочее", все суммы круглые
числа (выглядит подогнанным под лимит), запрошенная сумма не соответствует
масштабу деятельности.

СТАНДАРТНЫЕ ДОНОРЫ (Центральная Азия, актуально на 2026):
- USAID закрыт с 1 июля 2025 — не сокращение, а исчезновение категории.
  Финансирование США теперь в основном через посольские Public Diplomacy
  гранты ($20-100k) или Democracy Commission гранты (до $24k, не
  финансируют зарплаты/оборудование/travel в США, требуют билингвальную
  подачу).
- EU: логфрейм, admin cap ~15%, €50k-5M, EU Civil Society Facility в ЦА,
  риторика Global Gateway (инфраструктура/зелёная/цифровая экономика) даже
  для гражданского общества.
- EBRD: рыночный подход, финансовая устойчивость, частный сектор, женщины
  в бизнесе, от €200k.
- UNDP: связь с ЦУР, климат, governance, инклюзия, $50k-3M.
- Aga Khan/AKDN: горные регионы (Таджикистан, Кыргызстан), долгосрочное
  партнёрство с сообществом.
- GIZ: профобразование, сельское хозяйство, governance.
- Диверсификация доноров теперь необходимость, а не рекомендация — если
  весь план держится на одном донорском семействе, это риск для раздела
  устойчивости.

ЕСЛИ ДАННЫХ НЕ ХВАТАЕТ — прямо скажи об этом и предложи, что уточнить у
пользователя, вместо того чтобы придумывать цифры. Если локальной
статистики нет — можно применить признанный международный бенчмарк
(WHO/World Bank/ILO/UNWTO и т.п.) к местному охвату, но ЯВНО пометить
результат как оценку, а не измеренный факт.
""".strip()


# ---------------------------------------------------------------------------
# АнтиИИ-стиль — применяется на КАЖДОЙ генерации текста, не только в конце
# ---------------------------------------------------------------------------

STYLE_GUARD_PROMPT = """
СТИЛИСТИЧЕСКАЯ ДИСЦИПЛИНА (применяй всегда, в любом сгенерированном тексте
— от идей и черновиков дерева до финального документа, а не только перед
отправкой):
- Не используй триады и параллельные конструкции ("X, Y и Z" по всему
  тексту; "во-первых... во-вторых... в-третьих").
- Не используй драматичные усилители и напыщенные глаголы там, где
  подошло бы простое описание.
- Не используй самоутверждающие абсолюты ("полностью соответствует",
  "полностью решает") — практики формулируют с оговорками, генераторы нет.
- Не используй клише-метафоры ("слепая зона", "обратная связь как
  инструмент", "синергия усилий").
- Избегай затёртых базвордов, которые донорские рецензенты научились
  игнорировать: "надёжный", "комплексный", "вовлечённость сторон",
  "синергия", "leverage".
- Не соединяй тире два независимых утверждения без необходимости.
- Не делай все абзацы и предложения одинаковой длины — естественный текст
  имеет вариацию.
Вместо этого используй: конкретные локальные детали (реальные названия
мест, партнёров, цифры), голос практика от первого лица, естественную
вариацию длины предложений. Не заявляй "написано без ИИ" — это неверно
и непроверяемо; вместо заявлений просто пиши так, будто текст писал
человек, который лично знает контекст.
""".strip()


PLACEHOLDER_RULE = """
ПРАВИЛО ПЛЕЙСХОЛДЕРОВ ДЛЯ НЕДОСТАЮЩИХ ДАННЫХ (критично, применяется ВЕЗДЕ —
в черновике, в финальной версии, при заполнении формы донора):

Когда для места в тексте нужна конкретная цифра, дата, сумма, процент или
факт, которого нет в собранной информации — НИКОГДА не:
- выдумывай правдоподобное число;
- пиши мета-комментарий вроде "Оценка, требует проверки:", "нужны точные
  данные", "уточнить у..." — это выглядит как незаконченный черновик, а не
  готовый текст;
- используй громоздкую форму [XYZ: длинное описание того, что нужно
  уточнить] посреди предложения — она разрывает текст и выглядит как
  техническая пометка, а не часть заявки.

Вместо этого пиши предложение ЦЕЛИКОМ так, будто цифра там уже есть, просто
подставляя короткий маркер XYZ прямо на место числа/факта (не в скобках, без
пояснений), сохраняя естественную грамматику предложения. Один и тот же
недостающий факт, если упоминается несколько раз, может иметь несколько XYZ
подряд для разных чисел в одном предложении.

Пример (неправильно): "Оценка, требует проверки: по данным ОО «Дестинация
Ош» о росте туристического потока в Ош за последние годы... [XYZ: точные
цифры турпотока по годам, если есть в статистике мэрии или Госагентства по
туризму]"

Пример (правильно): "XYZ туристов проходят ежегодно по этим маршрутам,
включая XYZ иностранных туристов. Их число растёт на XYZ% каждый год за
последние 3 года согласно данным XYZ."

Это позволяет пользователю просто найти каждый XYZ и заменить его реальным
числом/названием, не переписывая структуру предложения.
""".strip()


REASONING_STYLE = """
КАК РАССУЖДАТЬ В ДИАЛОГЕ (не только в финальных документах):
Ты не механический опросник, который просто задаёт следующий вопрос по
списку. Прежде чем спросить что-то или перейти к следующему шагу, коротко
(1-2 предложения) поделись вслух, как ты рассуждаешь — что уже понятно из
сказанного, что смущает или кажется противоречивым, почему именно этот
вопрос сейчас важен. Это не должно превращаться в длинное эссе — только
короткая, живая реплика перед вопросом/переходом, как думал бы человек,
который внимательно слушает собеседника.

ЕСЛИ ТЫ НЕ УВЕРЕН или ответ пользователя можно понять двояко — скажи это
прямо ("правильно понимаю, что..." / "тут не до конца ясно, ты имеешь в
виду X или Y?"), вместо того чтобы молча додумать и пойти дальше с
возможно неверным пониманием. Осознавай и признавай, когда теряешь нить
разговора, а не делай вид, что всё понял.
""".strip()


SYSTEM_PROMPT = f"{METHODOLOGY_PROMPT}\n\n{STYLE_GUARD_PROMPT}\n\n{PLACEHOLDER_RULE}"


LANGUAGE_NAMES = {"ru": "русском", "ky": "кыргызском", "en": "английском"}


def ui_language_clause(session_data: dict | None) -> str:
    """Инструкция для системного промпта: на каком языке общаться с
    пользователем (вопросы, реплики, подтверждения) — выбирается один раз
    в начале сессии (см. handlers/start.py::choose_ui_language) и не
    привязан к языку итогового документа."""
    lang = (session_data or {}).get("ui_language", "ru")
    name = LANGUAGE_NAMES.get(lang, "русском")
    if lang == "ru":
        return ""
    return f"\n\nВАЖНО: общайся с пользователем ТОЛЬКО на {name} языке — все вопросы, реплики, подтверждения."


def doc_language_clause(session_data: dict | None) -> str:
    """Инструкция для системного промпта: на каком языке должен быть
    написан итоговый документ — может отличаться от языка общения (донор
    часто требует конкретный язык заявки независимо от языка диалога)."""
    lang = (session_data or {}).get("doc_language") or (session_data or {}).get("ui_language", "ru")
    name = LANGUAGE_NAMES.get(lang, "русском")
    if lang == "ru":
        return ""
    return f"\n\nВАЖНО: итоговый документ должен быть написан ПОЛНОСТЬЮ на {name} языке — включая названия разделов/полей (переведи их, сохраняя структуру и смысл формы донора один в один)."


async def detect_doc_language(text: str) -> str | None:
    """Определяет язык сайта/формы донора по присланному тексту (страница
    конкурса, скачанная форма заявки) — возвращает 'ru'/'ky'/'en', или None
    если определить не удалось (текст пуст/слишком короткий/неоднозначен).

    РЕАЛЬНЫЙ ИНЦИДЕНТ: раньше бот отдельным шагом СПРАШИВАЛ пользователя,
    на каком языке должен быть готовый документ — избыточный вопрос:
    заявка почти всегда подаётся на языке сайта/формы самого конкурса
    (это и есть требование донора), а не на языке, который выберет
    пользователь произвольно. Теперь язык документа определяется
    автоматически по реальному тексту донора, без лишнего вопроса; explicit
    ui_language остаётся резервным вариантом (см. doc_language_clause),
    если у донора вообще нет текста для анализа (например, донор описан
    только устно)."""
    sample = (text or "").strip()
    if len(sample) < 30:
        return None
    system_prompt = (
        "Определи, на каком языке написан присланный текст (сайт/форма "
        "донора/гранта). Ответь СТРОГО одним словом из списка: 'ru' "
        "(русский), 'ky' (кыргызский), 'en' (английский), 'other' (любой "
        "другой язык). Никаких пояснений, только код языка."
    )
    try:
        result = (await call_claude(system_prompt, sample[:3000], max_tokens=10)).strip().lower()
    except Exception:
        return None
    for code in ("ru", "ky", "en"):
        if code in result:
            return code
    return None


async def call_fallback_llm(
    system_prompt: str,
    user_message: str,
    history: list[dict] | None = None,
    max_tokens: int = 2000,
) -> str:
    """Резервный вызов через DeepSeek (приоритет) или Gemini при отказе Anthropic."""
    effective_max_tokens = max(max_tokens, 200)
    messages = []
    if system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt})
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    # Приоритетный fallback: DeepSeek
    if _deepseek_client:
        try:
            resp = await _deepseek_client.chat.completions.create(
                model=getattr(config, "DEEPSEEK_MODEL", "deepseek-chat"),
                messages=messages,
                max_tokens=effective_max_tokens,
            )
            if resp.choices and resp.choices[0].message and resp.choices[0].message.content:
                logger.info("Successfully received answer from DeepSeek (%s)", config.DEEPSEEK_MODEL)
                return resp.choices[0].message.content
        except Exception as ds_err:
            logger.warning("DeepSeek call failed: %s, falling back to Gemini", ds_err)

    # Запасной fallback: Gemini
    if _fallback_client:
        try:
            resp = await _fallback_client.chat.completions.create(
                model=config.FALLBACK_LLM_MODEL,
                messages=messages,
                max_tokens=effective_max_tokens,
                extra_body={"reasoning_effort": "none"},
            )
            if resp.choices and resp.choices[0].message:
                return resp.choices[0].message.content or ""
        except Exception as gem_err:
            logger.error("Gemini fallback also failed: %s", gem_err)

    return ""


async def call_claude(
    system_prompt: str,
    user_message: str,
    history: list[dict] | None = None,
    max_tokens: int = 2000,
) -> str:
    # Приоритетно используем DeepSeek (быстро, надежно и с активным балансом)
    if _deepseek_client:
        try:
            res_text = await call_fallback_llm(
                system_prompt=system_prompt,
                user_message=user_message,
                history=history,
                max_tokens=max_tokens,
            )
            if res_text.strip():
                return res_text
        except Exception as ds_exc:
            logger.warning("DeepSeek in call_claude failed: %s, falling back to Anthropic", ds_exc)

    try:
        messages = (history or []) + [{"role": "user", "content": user_message}]
        response = await _client.messages.create(
            model=config.LLM_MODEL,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=messages,
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        if not text.strip():
            logger.warning(
                "call_claude got empty text: stop_reason=%s, system_len=%d, user_len=%d, max_tokens=%d",
                getattr(response, "stop_reason", "?"), len(system_prompt), len(user_message), max_tokens,
            )
        else:
            return text
    except Exception as exc:
        logger.warning(
            "Primary Anthropic call failed: %s (%s). Attempting fallback provider...",
            type(exc).__name__, exc,
        )
        if _fallback_client:
            try:
                fallback_text = await call_fallback_llm(
                    system_prompt=system_prompt,
                    user_message=user_message,
                    history=history,
                    max_tokens=max_tokens,
                )
                if fallback_text.strip():
                    logger.info("Successfully received answer from fallback model (%s)", config.FALLBACK_LLM_MODEL)
                    return fallback_text
            except Exception as fb_exc:
                logger.error("Fallback provider also failed: %s (%s)", type(fb_exc).__name__, fb_exc)
        # Если fallback не сработал или отсутствует, пробрасываем исходное исключение
        raise

    # Если Anthropic вернул пустой текст без исключения, пробуем fallback
    if _fallback_client:
        try:
            fallback_text = await call_fallback_llm(
                system_prompt=system_prompt,
                user_message=user_message,
                history=history,
                max_tokens=max_tokens,
            )
            if fallback_text.strip():
                return fallback_text
        except Exception:
            pass

    return ""


class LLMEmptyResponseError(Exception):
    """Raised when call_claude returns empty text after a retry.

    Callers that hand the result straight to Telegram (message text, saved
    state that gets replayed as a message later) must catch this and show
    the user an explicit retry prompt — never silently proceed with an
    empty string. Doing so previously caused a hard crash several steps
    later (Telegram rejects sendMessage with empty text), at a point far
    removed from the actual failure, making it look like the bot "froze"
    for no reason.
    """


def _is_unrecoverable_billing_error(exc: Exception) -> bool:
    """РЕАЛЬНЫЙ ИНЦИДЕНТ: аккаунт Anthropic исчерпал баланс ('Your credit
    balance is too low') — call_claude_required всё равно тратил 3 попытки
    с задержками (~7 секунд впустую, ретрай не может помочь при нулевом
    балансе), и пользователь видел ту же общую фразу 'не удалось получить
    ответ от модели', что и на обычный временный сетевой сбой — не отличить
    без чтения лога, что дело в оплате, а не в баге."""
    msg = str(exc).lower()
    return "credit balance is too low" in msg or "insufficient_quota" in msg


def user_facing_llm_error_message(exc: Exception, retry_hint: str = "Повтори сообщение, чтобы попробовать снова.") -> str:
    """Единый текст для пользователя при LLMEmptyResponseError — отличает
    неисправимую ошибку баланса (ретрай не поможет, нужно ждать пополнения)
    от обычного временного сбоя API (ретрай может помочь)."""
    if "БАЛАНС_ИСЧЕРПАН" in str(exc):
        return (
            "⚠️ Сервис временно недоступен — технические работы на нашей "
            "стороне. Попробуй чуть позже."
        )
    return f"⚠️ Не удалось получить ответ от модели. {retry_hint}"


async def call_claude_required(
    system_prompt: str,
    user_message: str,
    history: list[dict] | None = None,
    max_tokens: int = 2000,
) -> str:
    """Like call_claude, but retries and raises LLMEmptyResponseError
    instead of silently returning "" on an empty/failed response.

    Retries 3 times (not 1) with a short backoff — transient Anthropic API
    hiccups (rate limit / overloaded / timeout) are common on long final-
    document generations and were previously surfaced to the user as a
    generic, unrecoverable-looking error after a single retry. The actual
    exception is now logged (fund4pro.log) instead of being silently
    swallowed, so failures are diagnosable instead of a black box.
    """
    attempts = 3
    for attempt in range(attempts):
        try:
            # Если предыдущая попытка вернула пустой текст из-за нехватки
            # max_tokens (длинный reasoning/большой контекст съел весь
            # лимит до текстового блока), удваиваем лимит на следующей
            # попытке вместо того чтобы повторять тот же самый сбой.
            effective_max_tokens = max(max_tokens, min(max_tokens * (2 ** attempt), 16000))
            text = await call_claude(system_prompt, user_message, history, max_tokens=effective_max_tokens)
        except Exception as exc:
            logger.warning(
                "call_claude failed (attempt %d/%d): %s: %s",
                attempt + 1, attempts, type(exc).__name__, exc,
            )
            if _is_unrecoverable_billing_error(exc):
                raise LLMEmptyResponseError(
                    "БАЛАНС_ИСЧЕРПАН: на аккаунте Anthropic закончились средства "
                    "— нужно пополнить баланс в Plans & Billing, ретраи здесь не "
                    "помогут."
                ) from exc
            if attempt == attempts - 1:
                raise LLMEmptyResponseError(
                    f"API call failed after {attempts} attempts: {type(exc).__name__}: {exc}"
                ) from exc
            await asyncio.sleep(2 * (attempt + 1))
            continue
        if text.strip():
            return text
        logger.warning("call_claude returned empty text (attempt %d/%d)", attempt + 1, attempts)
    raise LLMEmptyResponseError("Empty response after retry")


async def generate_ideas(org_info: str, donor_info: str, flow: str, donor_forms_text: str = "") -> tuple[str, list[str]]:
    """Возвращает (вводное_примечание, [идея1, идея2, ...]).

    Вводное примечание — необязательная одна строка с оговоркой (например
    "нет данных о профиле организации, ниже рабочие гипотезы") — не идея
    и не должна нумероваться/попадать в кнопки выбора.
    """
    subject = "грантовых проектов" if flow == "grant" else "бизнес-идей"
    system_prompt = (
        f"{SYSTEM_PROMPT}\n\nНа основе информации об организации/бизнесе и "
        f"о доноре/инвесторе предложи 3-4 конкретные идеи {subject}, скрещивая "
        f"сильные стороны организации с приоритетами донора/инвестора. "
        f"Для каждой идеи — одна строка: короткое название + суть одним "
        f"предложением. Без нумерации и вступлений в самих идеях, каждая "
        f"идея с новой строки.\n\n"
        f"ВАЖНО: если ниже нет содержимого формы донора (только ссылка или "
        f"пусто), НЕ пиши в идеях о том, что не можешь открыть файл или "
        f"сайт — это техническая деталь работы бота, не относится к сути "
        f"идей.\n\n"
        f"Если нужно сделать оговорку читателю ПЕРЕД списком (например "
        f"'данных о профиле организации мало, ниже рабочие гипотезы, сверь "
        f"с реальными приоритетами донора') — вынеси её ОТДЕЛЬНОЙ первой "
        f"строкой, начинающейся строго с 'ПРИМЕЧАНИЕ: '. Если оговорка не "
        f"нужна, не пиши эту строку вообще. После необязательной строки "
        f"ПРИМЕЧАНИЕ сразу идут только сами идеи, без дополнительных вступлений."
    )
    donor_block = donor_info
    if donor_forms_text.strip():
        donor_block += f"\n\nСодержимое найденной формы/гайдлайнов донора:\n{donor_forms_text}"
    user_message = f"Организация/бизнес:\n{org_info}\n\nДонор/инвестор:\n{donor_block}"
    text = await call_claude(system_prompt, user_message)
    lines = [line.strip("-• ").strip() for line in text.splitlines() if line.strip()]

    note = ""
    if lines and lines[0].startswith("ПРИМЕЧАНИЕ:"):
        note = lines[0][len("ПРИМЕЧАНИЕ:"):].strip()
        lines = lines[1:]

    ideas = lines[:4] if lines else ([text.strip()] if not note else [])
    return note, ideas


async def tree_dialogue_turn(session_data: dict, user_message: str, history: list[dict]) -> str:
    stage = session_data.get("tree_stage", "problem")
    stage_hint = {
        "problem": "Сейчас разбираем проблему проекта.",
        "objectives": "Сейчас разбираем цель проекта.",
        "action": "Сейчас разбираем конкретные действия/мероприятия.",
    }.get(stage, "")
    known_context = _session_summary(session_data)
    context_block = (
        f"\n\nУЖЕ ИЗВЕСТНО ИЗ ПРЕДЫДУЩИХ ШАГОВ (не переспрашивай то, что здесь "
        f"уже есть — донор, организация, идея и т.п. уже собраны):\n{known_context}"
        if known_context.strip()
        else ""
    )
    system_prompt = (
        f"{SYSTEM_PROMPT}\n\n{stage_hint} Веди диалог пошагово, задавай "
        f"уточняющие вопросы, не додумывай факты за пользователя. Не используй "
        f"внутреннюю терминологию методики («дерево проблем», «дерево целей», "
        f"«дерево действий» и т.п.) в общении с пользователем — говори простым "
        f"языком о проблеме/цели/действиях напрямую. Каждый твой ответ должен "
        f"явно заканчиваться ЛИБО конкретным вопросом к пользователю, ЛИБО "
        f"явным указанием, что ты переходишь к следующему шагу сам — никогда "
        f"не оставляй непонятным, ждёшь ли ты ответа или уже что-то делаешь."
        f"{context_block}{ui_language_clause(session_data)}"
    )
    return await call_claude(system_prompt, user_message, history)


async def generate_concept(session_data: dict) -> str:
    system_prompt = (
        f"{SYSTEM_PROMPT}\n\nСформируй краткий концепт проекта/бизнес-плана "
        f"(1-1.5 страницы): проблема, решение, цель/задачи, бюджет верхнеуровнево. "
        f"Используй markdown-заголовки вида '## Название раздела' перед каждым "
        f"разделом.{ui_language_clause(session_data)}"
    )
    return await call_claude_required(system_prompt, _session_summary(session_data), max_tokens=4000)


async def generate_goal_and_objectives(session_data: dict) -> str:
    """Бот САМ (одним структурированным вызовом, без диалога) предлагает
    цель проекта и 2-3 задачи на основе проблемы, которую пользователь уже
    описал/выбрал. Заменяет прежний открытый диалог "какое изменение мы
    хотим увидеть" — тот подход терял нить разговора между репликами
    (модель интерпретировала каждый ответ заново без твёрдой структуры,
    отсюда путаница вроде "тропы чистые" vs "хотим, чтобы стали чистыми").
    Один вызов с чёткой структурой output устраняет этот класс ошибок.
    """
    system_prompt = (
        f"{SYSTEM_PROMPT}\n\nНа основе описанной проблемы предложи:\n"
        f"1. Одну чёткую ЦЕЛЬ проекта (что изменится в итоге — формулировка "
        f"именно как изменение состояния, а не как активность).\n"
        f"2. РОВНО 2-3 ЗАДАЧИ, ведущие к этой цели (не более 3 — если "
        f"напрашивается больше, оставь только самые приоритетные).\n\n"
        f"Формат ответа СТРОГО:\n"
        f"## Цель\n<одна фраза>\n\n"
        f"## Задачи\n1. <задача>\n2. <задача>\n3. <задача, если нужна>\n\n"
        f"Без вступлений и заключений — только эти два раздела.{ui_language_clause(session_data)}"
    )
    return await call_claude_required(system_prompt, _session_summary(session_data))


def summarize_xyz_placeholders(text: str) -> str:
    """Короткая (не LLM, чисто текстовая) сводка мест, где в документе
    остались XYZ-плейсхолдеры — сгруппированная по разделам (markdown
    '## Заголовок'), с коротким фрагментом предложения для каждого места.

    Пользователь просил: бот должен явно и КРАТКО указывать, В КАКИХ
    МЕСТАХ стоят XYZ, а не просто общей фразой 'где-то есть XYZ, найди
    сам'. Регэксп-подход (не отдельный LLM-вызов) — быстро, детерминированно,
    не может 'забыть' упомянуть место или выдумать несуществующее.
    """
    import re

    if "XYZ" not in text:
        return ""

    current_section = ""
    lines_with_xyz: list[tuple[str, str]] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            current_section = stripped.lstrip("#").strip()
            continue
        if "XYZ" in line:
            # Короткий фрагмент вокруг первого XYZ в строке — не всю строку
            # целиком (может быть длинным предложением).
            idx = line.find("XYZ")
            start = max(0, idx - 25)
            end = min(len(line), idx + 40)
            snippet = line[start:end].strip()
            if start > 0:
                snippet = "..." + snippet
            if end < len(line):
                snippet = snippet + "..."
            lines_with_xyz.append((current_section or "документ", snippet))

    if not lines_with_xyz:
        return ""

    by_section: dict[str, list[str]] = {}
    for section, snippet in lines_with_xyz:
        by_section.setdefault(section, []).append(snippet)

    parts = ["📌 Места, где не хватило данных (отмечены XYZ):"]
    for section, snippets in by_section.items():
        parts.append(f"\n**{section}:**")
        # Не более 3 примеров на раздел — короткая сводка, не полный список
        # каждого вхождения (их может быть много в длинном документе).
        for snippet in snippets[:3]:
            parts.append(f"  • {snippet}")
        if len(snippets) > 3:
            parts.append(f"  • ...и ещё {len(snippets) - 3} в этом разделе")
    return "\n".join(parts)


async def generate_draft_concept(session_data: dict) -> str:
    """Черновой концепт БЕЗ дополнительных вопросов — сразу, по тому, что
    уже есть в сессии (включая одобренную цель/задачи). Отсутствующие
    цифры/факты заменяются плейсхолдером [XYZ], а не выдумываются — так
    пользователь сразу видит, что нужно поправить, вместо правдоподобно
    звучащих, но выдуманных данных."""
    system_prompt = (
        f"{SYSTEM_PROMPT}\n\nСформируй черновой концепт проекта/бизнес-плана "
        f"(1-1.5 страницы) прямо сейчас, БЕЗ дополнительных вопросов "
        f"пользователю — работай с тем, что уже есть в контексте (включая "
        f"уже согласованную цель и задачи, если они там есть). Используй "
        f"markdown-заголовки вида '## Название раздела' перед каждым разделом. "
        f"Следуй правилу плейсхолдеров XYZ из системного промпта для любых "
        f"недостающих цифр/фактов — не выдумывай их и не помечай отдельно."
        f"{ui_language_clause(session_data)}"
    )
    return await call_claude_required(system_prompt, _session_summary(session_data), max_tokens=4000)


async def detail_activities(session_data: dict, user_message: str, history: list[dict]) -> str:
    """Диалог детализации мероприятий/действий ПОД уже одобренную цель и
    задачи (единственная оставшаяся диалоговая стадия). В отличие от
    прежнего трёхэтапного диалога "проблема->цели->действия", цель и задачи
    сюда приходят уже зафиксированными одним вызовом (generate_goal_and_
    objectives), так что модели не нужно додумывать/переинтерпретировать
    их заново на каждом ходу — весь риск "потери нити" был именно в
    открытом диалоге по целям."""
    known_context = _session_summary(session_data)
    context_block = (
        f"\n\nУЖЕ ЗАФИКСИРОВАНО (не пересматривай, работай в этих рамках):\n{known_context}"
        if known_context.strip()
        else ""
    )
    system_prompt = (
        f"{SYSTEM_PROMPT}\n\nСейчас детализируем конкретные мероприятия под "
        f"уже согласованные цель и задачи. Веди диалог пошагово, задавай "
        f"уточняющие вопросы, не додумывай факты за пользователя. Говори "
        f"простым языком, без терминологии методики. Каждый ответ должен "
        f"явно заканчиваться ЛИБО конкретным вопросом, ЛИБО явным указанием, "
        f"что переходишь к следующему шагу сам."
        f"{context_block}\n\n{REASONING_STYLE}{ui_language_clause(session_data)}"
    )
    return await call_claude(system_prompt, user_message, history)


async def generate_search_queries(context_text: str) -> list[str]:
    """Превращает длинное описание идеи/организации в 2-3 КОРОТКИХ,
    конкретных поисковых запроса (не пересказ, а то, что реально стоит
    вбить в поисковик, чтобы найти статистику/цифры). Раньше весь текст
    описания шёл в поисковик одним запросом целиком — длинный абзац почти
    не даёт релевантных результатов у обычных поисковиков.

    Возвращает список строк; при сбое парсинга — одну простую эвристику
    (первые ~8 слов исходного текста) вместо падения."""
    system_prompt = (
        "На основе описания ниже сформулируй 2-3 коротких (3-7 слов) "
        "поисковых запроса для обычного поисковика — такие, по которым "
        "реально можно найти статистику, цифры или официальные данные по "
        "теме (не общие слова, а конкретика: название явления/региона/"
        "показателя). Верни СТРОГО JSON-массив строк, без пояснений, "
        "например: [\"туристический поток Ош статистика\", \"отходы туризм "
        "Кыргызстан данные\"]."
    )
    try:
        raw = await call_claude(system_prompt, context_text[:2000], max_tokens=300)
        start = raw.find("[")
        end = raw.rfind("]")
        if start == -1 or end == -1:
            raise ValueError("no JSON array found")
        queries = json.loads(raw[start:end + 1])
        queries = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
        if queries:
            return queries[:3]
    except Exception as exc:
        logger.warning("generate_search_queries failed: %s: %s", type(exc).__name__, exc)
    # Fallback: простая эвристика вместо полного отказа от поиска.
    words = context_text.split()[:8]
    return [" ".join(words)] if words else []


async def generate_budget_proposal(session_data: dict, user_message: str = "", history: list[dict] | None = None) -> str:
    """Диалог согласования бюджета перед финальной версией — бот САМ
    предлагает реалистичные ориентировочные суммы (не оставляет всё как
    XYZ), опираясь на масштаб проекта, регион и типичные расценки, и
    явно помечает их как ОРИЕНТИРОВОЧНЫЕ, требующие подтверждения
    пользователем перед тем, как они попадут в финальный документ.

    Раньше бюджетная таблица в финальном документе была сплошь из XYZ —
    формально это следовало правилу "не выдумывай цифры", но выглядело
    как непроработанный документ и не давало пользователю ничего, от чего
    оттолкнуться. Здесь модель, как обычный Claude в диалоге, сама
    прикидывает разумные порядки величин (сколько стоят типовые контейнеры
    для раздельного сбора, тренинг на N участников, печать материалов и
    т.п. в контексте страны/региона проекта) и явно спрашивает
    подтверждения/правок, вместо того чтобы либо выдумывать цифры молча,
    либо оставлять пустые плейсхолдеры без всякой помощи.
    """
    known_context = _session_summary(session_data)
    context_block = f"\n\nКонтекст проекта:\n{known_context}" if known_context.strip() else ""
    system_prompt = (
        f"{SYSTEM_PROMPT}\n\nСейчас нужно согласовать бюджет проекта с "
        f"пользователем перед тем, как готовить финальный документ. "
        f"Если это первое сообщение в этом диалоге (истории ещё нет) — "
        f"сам предложи ориентировочную бюджетную разбивку по статьям "
        f"(на основе масштаба проекта, страны/региона и типичных рыночных "
        f"цен на аналогичные товары/услуги — контейнеры, тренинги, печать, "
        f"стипендии координаторам и т.п.), явно пометив, что это "
        f"ОРИЕНТИРОВОЧНЫЕ суммы для обсуждения, а не готовые цифры. "
        f"Не оставляй статьи пустыми/как XYZ без предложения — предложи "
        f"конкретную вилку (например, '150-250 USD'), это стартовая точка "
        f"для правок, а не финальное решение. Также явно спроси про размер "
        f"административных расходов (обычно 10-15% от суммы гранта, но "
        f"уточни у пользователя предпочтение) — не решай за него молча. "
        f"Дальше веди диалог: пользователь может согласиться, попросить "
        f"поправить конкретные статьи, или дать свои реальные цифры — "
        f"учитывай это и уточняй дальше, пока пользователь явно не "
        f"подтвердит, что бюджет готов (тогда следующий шаг подхватит это "
        f"сам через отдельную кнопку, тебе не нужно самому завершать "
        f"диалог)."
        f"{context_block}\n\n{REASONING_STYLE}{ui_language_clause(session_data)}"
    )
    # РЕАЛЬНЫЙ ИНЦИДЕНТ: бюджетный текст обрывался на полуслове
    # ('...реальную вилку лучше сверить с тем, что банки региона считают
    # "непод...') — call_claude использовался с дефолтным max_tokens=2000
    # БЕЗ ретрая на обрезанный ответ, в отличие от call_claude_required.
    # При длинной истории диалога + REASONING_STYLE ответ мог не влезать.
    # Переключаемся на call_claude_required (ретраит с увеличением лимита)
    # и поднимаем базовый max_tokens.
    try:
        return await call_claude_required(
            system_prompt, user_message or "Предложи бюджет.", history, max_tokens=3000
        )
    except LLMEmptyResponseError:
        raise


async def label_donor_form(filename: str, form_text: str) -> str:
    """Короткая (3-6 слов) человекочитаемая метка того, ЧТО это за форма
    (например 'Основная заявка на проект' vs 'Форма командировочных
    расходов') — используется, когда донор публикует НЕСКОЛЬКО разных
    форм на одной странице (частый случай: основная заявка + travel/
    бюджетная форма + отчётная форма), чтобы бот мог явно спросить
    пользователя, какую именно заполнять, вместо того чтобы молча
    смешать их структуры в одну кашу или выбрать наугад."""
    system_prompt = (
        "Одной короткой фразой (3-6 слов, на русском) опиши, ЧТО ЭТО ЗА "
        "ДОКУМЕНТ — не пересказывай содержание, а определи его назначение "
        "по названию/структуре (например: 'Основная заявка на грант', "
        "'Форма командировочных расходов', 'Бюджетная форма', 'Итоговый "
        "отчёт по гранту'). Без кавычек и точки в конце."
    )
    user_message = f"Имя файла: {filename}\n\nНачало текста:\n{form_text[:2000]}"
    try:
        label = await call_claude(system_prompt, user_message, max_tokens=50)
        return label.strip().strip('"').strip("'") or filename
    except Exception as exc:
        logger.warning("label_donor_form failed: %s: %s", type(exc).__name__, exc)
        return filename


async def assess_eligibility_against_org(criteria: str, org_info: str) -> str:
    """Сравнивает уже присланную пользователем информацию об организации
    с критериями отбора донора и даёт КОРОТКУЮ (3-5 предложений)
    рекомендацию: похоже ли, что заявитель подходит, и почему (конкретно
    указывая, каким пунктам соответствует/не соответствует, если это
    видно из org_info).

    Раньше бот просто показывал критерии текстом и спрашивал пользователя
    'подходишь?' — заставляя его самого сверять пункты. Теперь бот сам
    делает эту сверку (это же его работа) и даёт рекомендацию, а
    финальное решение продолжать или нет остаётся за пользователем.

    Может честно сказать, что не может определить (если из org_info не
    ясно, например, страна регистрации) — не должен выдумывать факты об
    организации, которых нет в присланной информации."""
    if not criteria.strip() or not org_info.strip():
        return ""
    system_prompt = (
        "Перед тобой критерии отбора донора и информация об организации/"
        "заявителе, которую он прислал о себе. Сравни их и дай КОРОТКУЮ "
        "рекомендацию (3-5 предложений): судя по присланной информации, "
        "подходит ли заявитель под эти критерии, и почему — ссылайся на "
        "конкретные пункты. Если по какому-то критерию (например, страна "
        "регистрации, ОПФ) в присланной информации ничего не сказано — "
        "честно скажи, что это не ясно, не выдумывай. Заверши явным "
        "выводом одним из трёх: 'Похоже, подходит', 'Похоже, НЕ подходит', "
        "или 'Не могу определить по имеющейся информации'."
    )
    try:
        return (await call_claude(
            system_prompt,
            f"Критерии донора:\n{criteria}\n\nИнформация об организации:\n{org_info[:4000]}",
            max_tokens=500,
        )).strip()
    except Exception as exc:
        logger.warning("assess_eligibility_against_org failed: %s: %s", type(exc).__name__, exc)
        return ""


async def extract_donor_eligibility_criteria(page_text: str) -> str:
    """Вытаскивает критерии/требования отбора со страницы конкурса донора
    (кто может подавать, географический охват, тип организации, размер
    гранта, дедлайны и т.п.) — коротким списком, чтобы сразу показать
    пользователю и спросить, подходит ли он, ДО того как тратить время на
    разработку целого проекта под донора, чьим критериям он не соответствует.

    Возвращает пустую строку, если явных критериев на странице не найдено
    (не выдумывает требования)."""
    if not page_text.strip():
        return ""
    system_prompt = (
        "Перед тобой текст страницы конкурса/грантовой программы донора. "
        "Найди и коротко (маркированным списком, не более 6 пунктов) "
        "перечисли КОНКРЕТНЫЕ критерии отбора/требования к заявителям, "
        "если они явно указаны: кто может подавать (тип организации — "
        "НКО/бизнес/физлицо и т.п.), география (страна/регион), размер "
        "гранта, дедлайн подачи, обязательная регистрация/партнёрство и "
        "подобное. Пиши только то, что ДЕЙСТВИТЕЛЬНО есть в тексте — не "
        "выдумывай и не обобщай. Если явных критериев в тексте нет, ответь "
        "ровно одной строкой: 'НЕТ_КРИТЕРИЕВ'."
    )
    try:
        raw = await call_claude(system_prompt, page_text[:8000], max_tokens=600)
    except Exception as exc:
        logger.warning("extract_donor_eligibility_criteria failed: %s: %s", type(exc).__name__, exc)
        return ""
    if not raw or "НЕТ_КРИТЕРИЕВ" in raw.strip()[:20]:
        return ""
    return raw.strip()


async def extract_donor_template_structure(donor_forms_text: str) -> str:
    """Достаёт из текста скачанной/присланной формы донора её РЕАЛЬНУЮ
    структуру — точные названия разделов/полей формы, их порядок, и любые
    явные ограничения (лимит слов/страниц, обязательные приложения).

    Критично: донор часто принимает заявки СТРОГО по своей форме и
    отклоняет свободные форматы. Раньше форма донора использовалась только
    как фоновый контекст для содержания, а финальный документ всё равно
    писался в собственной универсальной структуре бота (executive summary
    / об организации / ... ) — для донора со своей обязательной формой
    это бесполезно. Эта функция вытаскивает скелет, которому генерация
    финального документа обязана следовать буквально.
    """
    if not donor_forms_text.strip():
        return ""
    system_prompt = (
        "Перед тобой текст, извлечённый из формы заявки донора (конкурсной "
        "документации). Твоя задача — вытащить ТОЛЬКО структуру самой формы, "
        "а не содержание чужой заявки:\n"
        "1. Точный список разделов/пунктов/вопросов формы, в ТОМ ЖЕ порядке "
        "и с ТЕМИ ЖЕ формулировками, что в оригинале (не перефразируй "
        "названия разделов).\n"
        "2. Если есть — лимиты (максимум слов/символов/страниц на раздел, "
        "формат таблиц бюджета, обязательные приложения).\n"
        "Если это не форма заявки, а что-то другое (общая информация о "
        "доноре, новости, условия конкурса без самой формы), ответь ровно "
        "одной строкой: 'НЕТ_ФОРМЫ' — не выдумывай структуру.\n"
        "Не добавляй ничего от себя, не заполняй разделы примерами — "
        "нужен только пустой скелет формы."
    )
    result = await call_claude(system_prompt, donor_forms_text[:12000], max_tokens=1500)
    result = result.strip()
    # РЕАЛЬНЫЙ ИНЦИДЕНТ: для длинных/подробных форм (много полей, лимитов
    # слов и т.п.) 1500 токенов не хватало — ответ обрывался на полуслове
    # ('...Другие гендерные идентичности:\n- Или'). Один повтор с увеличенным
    # лимитом почти всегда решает обрыв.
    if result and len(result) > 1000 and not result.rstrip().endswith((".", "!", "?", '"', "»")):
        logger.warning("extract_donor_template_structure: response looks truncated, retrying with higher max_tokens")
        result = (await call_claude(system_prompt, donor_forms_text[:12000], max_tokens=4000)).strip()
    # РЕАЛЬНЫЙ ИНЦИДЕНТ (х2): здесь раньше использовался is_actual_document()
    # — классификатор, рассчитанный на проверку ЗАПОЛНЕННЫХ документов
    # ('это реальная заявка с содержанием, или отказ модели?'). Скелет формы
    # ЗАКОНОМЕРНО не имеет 'содержания' (это просто список голых названий
    # полей типа 'Контактное лицо', 'Бюджет поездки') — is_actual_document
    # стабильно классифицировал ДАЖЕ корректно и полностью извлечённый
    # скелет как 'ОТКАЗ', и реально скачанная/прочитанная форма донора
    # (пользователь присылал её раньше) считалась 'не найденной'.
    # _looks_like_valid_template — правильный инструмент именно для этой
    # проверки (явный маркер НЕТ_ФОРМЫ или явные словесные признаки отказа
    # /просьбы прислать данные), без ложных срабатываний на пустых скелетах.
    if not result or "нет_формы" in result.lower()[:40] or not _looks_like_valid_template(result):
        return ""
    return result


def _looks_like_valid_template(text: str) -> bool:
    """Раньше проверялось ТОЛЬКО буквальное 'НЕТ_ФОРМЫ' в первых 20
    символах ответа — но модель не всегда следует инструкции формата
    буква-в-букву, особенно когда входных данных недостаточно: вместо
    строгого маркера она может естественным языком попросить прислать
    больше информации ('Пожалуйста, предоставьте сам текст формы...').
    Такой отказ раньше молча принимался ЗА структуру формы и попадал в
    donor_template, а затем в финальный документ — пользователь получал
    отказ модели вместо готовой заявки, оформленный как .docx.

    Здесь — двойная защита: явный маркер ИЛИ явные признаки отказа/просьбы
    (не просто отсутствие markdown-заголовков — реальная форма донора не
    всегда оформлена заголовками)."""
    lowered = text.lower()
    if "нет_формы" in lowered[:40]:
        return False
    if _is_llm_refusal_text(text):
        return False
    return True


async def match_intent(user_text: str, options: list[str]) -> int:
    """Сопоставляет свободный текст пользователя с одним из вариантов
    кнопок (по СМЫСЛУ, не по точному совпадению) — например 'ты что мне
    прислал, это хер какой-то!' на шаге финального одобрения по смыслу
    ближе к 'нужны правки', чем к 'всё устраивает'.

    РАНЬШЕ на состояниях, где бот ждал нажатия кнопки, ЛЮБОЙ текст
    (включая содержательный, осмысленный ответ пользователя по существу)
    просто игнорировался с сообщением 'жду кнопку выше' — это и есть
    'жёсткие рамки', на которые пожаловался пользователь: бот не понимал
    обычный человеческий язык, только клики.

    Возвращает индекс варианта в options (0-based) — если ни один вариант
    явно не подходит, возвращает -1 (вызывающий код должен в этом случае
    показать кнопки как раньше, не гадать)."""
    numbered = "\n".join(f"{i}: {opt}" for i, opt in enumerate(options))
    system_prompt = (
        "Пользователь отвечает текстом вместо нажатия одной из кнопок. "
        f"Вот варианты (номер: описание):\n{numbered}\n\n"
        "Определи, какому варианту по СМЫСЛУ соответствует ответ "
        "пользователя. Ответь СТРОГО одним числом — номером подходящего "
        "варианта. Если ответ не похож ни на один вариант (например, это "
        "отдельный вопрос, жалоба не по теме выбора, или что-то ещё "
        "содержательное, не являющееся выбором) — ответь ровно '-1'."
    )
    try:
        raw = (await call_claude(system_prompt, user_text, max_tokens=10)).strip()
        digits = "".join(c for c in raw if c.isdigit() or c == "-")
        idx = int(digits) if digits else -1
        return idx if 0 <= idx < len(options) else -1
    except Exception as exc:
        logger.warning("match_intent failed: %s: %s", type(exc).__name__, exc)
        return -1


async def is_actual_document(text: str) -> bool:
    """Надёжная (LLM-based, не keyword-based) проверка: это реальный
    документ (заявка/концепт/бюджет), или модель написала объяснение,
    почему она НЕ может его составить (не хватает данных, отказ,
    просьба прислать что-то ещё)?

    РЕАЛЬНЫЙ ИНЦИДЕНТ (дважды): _is_llm_refusal_text() с жёстким списком
    ключевых фраз пропустил отказ, сформулированный чуть иначе
    ('Реальное положение дел. У меня по этому проекту нет: текста или
    структуры формы... Что нужно от вас, чтобы двигаться дальше...') —
    финальный .docx, отправленный пользователю, содержал этот отказ вместо
    документа. Ключевые фразы будут постоянно теряться на новых
    формулировках модели; вместо расширения списка до бесконечности —
    отдельный, более надёжный классифицирующий вызов."""
    system_prompt = (
        "Перед тобой текст, который должен быть готовым документом "
        "(грантовая заявка, концепт проекта, или бюджет). Определи: это "
        "РЕАЛЬНО составленный документ (даже если в нём есть плейсхолдеры "
        "вроде XYZ для недостающих цифр), или это ОБЪЯСНЕНИЕ/ОТКАЗ — модель "
        "рассказывает, почему она не может составить документ, чего ей не "
        "хватает, и просит пользователя прислать данные. Ответь СТРОГО "
        "одним словом: 'ДОКУМЕНТ' или 'ОТКАЗ'."
    )
    try:
        raw = (await call_claude(system_prompt, text[:6000], max_tokens=10)).strip().upper()
        return "ОТКАЗ" not in raw
    except Exception as exc:
        logger.warning("is_actual_document failed: %s: %s", type(exc).__name__, exc)
        return True  # fail-open — не блокируем отправку из-за сбоя проверки


def _is_llm_refusal_text(text: str) -> bool:
    """Общий детектор 'модель отказалась выполнить задачу и написала об
    этом естественным языком вместо запрошенного документа/структуры'.

    Используется в ДВУХ местах: при извлечении структуры формы донора
    (_looks_like_valid_template) и — что важнее — как последняя защита
    после генерации ФИНАЛЬНОГО документа (generate_final_document),
    потому что реальный инцидент был именно там: модель написала связный
    текст-отказ ('Не могу заполнить донорскую форму — в переданных данных
    её структуры фактически нет...'), call_claude_required не считает это
    ошибкой (текст непустой), и отказ ушёл пользователю оформленным как
    готовый .docx документ.

    РЕАЛЬНЫЙ ИНЦИДЕНТ (х3): реальная форма донора (ГГФ) сама содержит поле
    'Пожалуйста, предоставьте подробный бюджет проекта' — легитимный текст
    самой формы, а не отказ модели. Полностью и корректно извлечённая
    структура (~5000 символов, все ~25 полей включая бюджетную таблицу)
    ложно классифицировалась как отказ из-за одного совпадения маркера
    внутри неё, structure отбрасывалась целиком, и generate_final_document
    работал БЕЗ donor_template — отсюда искажённая структура документа и
    пропавший раздел бюджета в финальной заявке. Настоящие отказы модели —
    это ВСЕГДА весь ответ целиком (несколько предложений объяснения), а не
    короткая фраза, случайно совпавшая где-то в середине тысяч символов
    легитимного содержания. Ограничиваем проверку короткими ответами —
    длинная структурированная структура/документ никогда не должны
    отклоняться по одному фрагменту текста.
    """
    if len(text.strip()) > 800:
        return False
    lowered = text.lower()
    refusal_markers = (
        "пожалуйста, предоставьте", "не вижу содержимого", "мне нужно от",
        "пришлите текст", "пришлите файл", "не могу заполнить",
        "не могу выполнить", "нужен реальный текст", "недостаточно данных",
        "предоставьте сам текст", "не могу составить", "не могу создать",
        "не могу сформировать", "я не вижу", "структуры фактически нет",
        "чтобы я мог", "как только пришлёте",
    )
    return any(marker in lowered for marker in refusal_markers)


async def generate_final_document(session_data: dict) -> str:
    donor_template = session_data.get("donor_template", "")
    # РЕАЛЬНЫЙ ИНЦИДЕНТ: на этом шаге пользователь уже прошёл весь путь
    # (идея -> цель/задачи -> концепт -> согласованный бюджет) и явно ждёт
    # ГОТОВЫЙ документ — но модель вместо заполнения формы задавала
    # уточняющие вопросы ("Прежде чем заполнять форму, мне нужно глубже
    # понять вашу идею... Какие именно экомаршруты сейчас существуют?"),
    # игнорируя прямое правило плейсхолдеров XYZ из системного промпта.
    # Более заметно на fallback-провайдере (Gemini) при недоступности
    # Anthropic, но защита нужна независимо от провайдера. is_actual_document
    # ниже уже ловит явные текстовые отказы, но этого недостаточно —
    # усиливаем сам промпт максимально прямым запретом, чтобы отказ/вопрос
    # не порождался вообще, а не отлавливался постфактум.
    no_questions_rule = (
        "ЭТОТ ШАГ — ФИНАЛЬНАЯ ГЕНЕРАЦИЯ ДОКУМЕНТА, НЕ ДИАЛОГ. Пользователь "
        "уже прошёл все предыдущие шаги (идея, цель и задачи, концепт, "
        "согласование бюджета) — весь собранный контекст ниже. Тебе "
        "ЗАПРЕЩЕНО задавать уточняющие вопросы, просить дополнительную "
        "информацию или объяснять, чего не хватает — ты ДОЛЖЕН вернуть "
        "готовый заполненный документ целиком, ПРЯМО СЕЙЧАС, на основе "
        "того, что есть в контексте. Для каждого факта/цифры, которых "
        "нет в контексте — используй XYZ-плейсхолдер (см. правило "
        "плейсхолдеров выше) и продолжай заполнение дальше, а не "
        "останавливайся, чтобы спросить. Единственный допустимый вывод — "
        "заполненный документ с markdown-заголовками разделов."
    )
    if donor_template:
        # Донор принимает заявки строго по своей форме — свободная структура
        # (executive summary/о организации/...) не подходит и будет
        # отклонена. Заполняем СТРОГО скелет донора, ничего не добавляя и
        # не переставляя местами.
        system_prompt = (
            f"{SYSTEM_PROMPT}\n\n{no_questions_rule}\n\nДонор принимает заявки ТОЛЬКО в своей "
            f"собственной форме — вот её структура (разделы/вопросы в "
            f"порядке оригинала):\n\n{donor_template}\n\n"
            f"Заполни ЭТУ форму содержанием проекта — используй эти же "
            f"названия разделов/вопросов ДОСЛОВНО, БУКВА В БУКВУ, как в "
            f"структуре формы выше — как markdown-заголовки вида "
            f"'## Название раздела', в том же порядке, ничего не добавляй "
            f"и не переставляй. НЕ переименовывай заголовок документа и "
            f"названия разделов/полей формы — они принадлежат донору, а не "
            f"тебе; название документа берётся из первой строки структуры "
            f"формы выше, а не придумывается заново (например, не пиши "
            f"'Проект / Бизнес-план', если в форме указано другое название). "
            f"Следуй правилу плейсхолдеров XYZ из "
            f"системного промпта для любых недостающих данных — не выдумывай "
            f"цифры и не помечай их отдельным комментарием. Соблюдай "
            f"указанные в форме лимиты (слова/страницы/бюджетная "
            f"таблица), если они там были. "
            f"ВАЖНО ПРО ДЕНЬГИ: если в контексте есть budget_text "
            f"(согласованный с пользователем бюджет) — используй эти цифры "
            f"ВЕЗДЕ В ФОРМЕ, где они логически нужны, а не только в отдельном "
            f"бюджетном разделе/таблице: 'Запрашиваемая сумма' в шапке формы, "
            f"стоимость конкретных мероприятий, если она упоминается в "
            f"описательных разделах, административные расходы (если "
            f"согласован их процент/сумма) — везде подставляй ИМЕННО эти "
            f"согласованные цифры, а не XYZ. XYZ оставляй только для тех "
            f"денежных величин, которых НЕТ ни в budget_text, ни где-либо ещё "
            f"в контексте.\n\n"
            f"КРИТИЧЕСКИ ВАЖНО: Форма заявки должна быть заполнена ПОЛНОСТЬЮ ДО САМОГО КОНЦА! "
            f"Самые главные содержательные разделы находятся в середине и конце формы: "
            f"1. КОНТЕКСТ (чёткое описание проблемы и её причин). "
            f"2. ПРОЕКТ (цель, задачи, план конкретных мероприятий, ожидаемые результаты). "
            f"3. ПОДРОБНЫЙ БЮДЖЕТ (в долларах США с конкретными строками затрат из согласованного бюджета). "
            f"ВАЖНО: раздел БЮДЖЕТ и ПЛАН ДЕЙСТВИЙ/РАБОЧИЙ ПЛАН нужно оформлять как MARKDOWN-ТАБЛИЦУ "
            f"(| № | Статья/Мероприятие | Кол-во/Срок | Ед. изм. | Стоимость/Результат | и т.п. — "
            f"структура колонок по смыслу формы), а не просто списком или текстом. "
            f"Пиши содержательно, но ёмко — соблюдай лимиты слов донора (150-200 слов на раздел), "
            f"не трать токены на лишние длинные юридические предисловия, чтобы форма гарантированно не оборвалась "
            f"и разделы КОНТЕКСТ, ПРОЕКТ и БЮДЖЕТ были полностью расписаны!"
            f"{doc_language_clause(session_data)}"
        )
    else:
        system_prompt = (
            f"{SYSTEM_PROMPT}\n\n{no_questions_rule}\n\nСформируй полную версию документа на основе "
            f"одобренного концепта и всех собранных деревьев. Используй markdown-"
            f"заголовки вида '## Название раздела' перед каждым разделом (executive "
            f"summary, об организации, обоснование проблемы, цель и задачи, план "
            f"деятельности, ожидаемые результаты, устойчивость, бюджет).\n\n"
            f"ВАЖНО ПРО ДЕНЬГИ: если в контексте есть budget_text (согласованный "
            f"с пользователем бюджет) — используй эти цифры ВЕЗДЕ по документу, "
            f"где они логически нужны (не только в разделе 'Бюджет'): общая "
            f"запрашиваемая сумма в executive summary, стоимость конкретных "
            f"мероприятий в плане деятельности, административные расходы (если "
            f"согласован их процент/сумма) — везде подставляй ИМЕННО эти "
            f"согласованные цифры, не заменяй их на XYZ и не придумывай другие. "
            f"XYZ оставляй только для тех денежных величин, которых НЕТ ни в "
            f"budget_text, ни где-либо ещё в контексте.\n\n"
            f"ВАЖНО: официальной формы донора найдено не было, поэтому это "
            f"универсальная структура — в начале документа явно предупреди "
            f"об этом одной строкой: 'ПРИМЕЧАНИЕ: официальная форма донора "
            f"не найдена, документ в универсальной структуре — при наличии "
            f"формы её нужно будет перенести в неё вручную.'"
            f"{doc_language_clause(session_data)}"
        )
    result = await call_claude_required(system_prompt, _session_summary(session_data), max_tokens=12000)
    if donor_template:
        # РЕАЛЬНЫЙ ИНЦИДЕНТ: несмотря на явную инструкцию "заполни ЭТУ форму
        # ДОСЛОВНО, буква в букву", модель всё равно 'улучшала' структуру
        # под привычный ей шаблон гранта — переименовывала заголовок
        # ('Форма заявки на проездной грант' -> 'Форма заявки на проект'),
        # добавляла разделы, которых нет у донора ('Рабочий план реализации
        # проекта' — целая логфрейм-таблица от себя), и УДАЛЯЛА обязательные
        # поля оригинала (пронумерованные пункты 1-7 в разделе ОПИСАНИЕ,
        # блок про конфиденциальность и т.п.). Промпт-инструкция сама по
        # себе оказалась недостаточной — нужна программная проверка, не
        # только просьба. Сверяем реальные заголовки результата с
        # заголовками формы донора; при расхождении — ОДНА попытка
        # исправления с explicit diff, не бесконечный цикл.
        result = await _enforce_donor_structure(result, donor_template, session_data)
    # РЕАЛЬНЫЙ ИНЦИДЕНТ (х2): модель вместо документа написала связный
    # текст-отказ ('Не могу заполнить донорскую форму...', позже другими
    # словами: 'Реальное положение дел. У меня по этому проекту нет...') —
    # call_claude_required не считает это ошибкой (текст непустой!),
    # поэтому отказ ушёл пользователю оформленным как готовый .docx
    # документ ДВАЖДЫ, второй раз с ДРУГОЙ формулировкой, которую жёсткий
    # keyword-детектор (_is_llm_refusal_text) не поймал. Заменено на
    # LLM-классификатор (is_actual_document) — надёжнее списка фраз,
    # который будет вечно неполным.
    if not await is_actual_document(result):
        logger.warning(
            "generate_final_document: model returned a refusal instead of a document: %s",
            result[:300],
        )
        raise LLMEmptyResponseError(
            "Модель отказалась генерировать документ — не хватает данных "
            "(см. текст отказа в логе)."
        )
    return result


_CRITICAL_SECTION_KEYWORDS = (
    "бюджет", "смета", "budget", "финанс",
    "план действ", "план мероприят", "мероприят", "деятельност",
    "activity plan", "action plan", "workplan", "рабочий план",
)


def _is_critical_heading(h: str) -> bool:
    """True для заголовков разделов, которые НИКОГДА нельзя молча терять —
    бюджет и план действий/мероприятий. Используется в _enforce_donor_structure,
    чтобы обойти общий 25%-допуск на расхождение структуры: тот допуск
    рассчитан на безобидное слияние мелких полей, а не на исчезновение
    самых содержательных разделов заявки."""
    hl = h.lower()
    return any(kw in hl for kw in _CRITICAL_SECTION_KEYWORDS)


def _extract_headings(text: str) -> list[str]:
    """Markdown-заголовки '## ...' (и первая непустая строка как заголовок
    документа) — используется для сверки структуры результата со структурой
    формы донора."""
    import re

    headings = []
    lines = text.split("\n")
    if lines and lines[0].strip():
        headings.append(lines[0].strip().lstrip("#").strip())
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("##"):
            headings.append(stripped.lstrip("#").strip())
    return headings


async def _enforce_donor_structure(result: str, donor_template: str, session_data: dict) -> str:
    template_headings = _extract_headings(donor_template)
    result_headings = _extract_headings(result)
    if not template_headings:
        return result

    # Если документ переводится на другой язык (doc_language_clause), точные
    # заголовки формы донора ЗАКОНОМЕРНО не совпадут текстуально с переводом
    # — сверяем структурную полноту по КОЛИЧЕСТВУ разделов вместо точного
    # текста, иначе это ложно триггерило бы 'модель исказила форму' на
    # каждом переведённом документе.
    translating = bool((session_data or {}).get("doc_language") and session_data.get("doc_language") != "ru")
    if translating:
        if len(result_headings) >= max(1, len(template_headings) - 1):
            return result
        missing_count = len(template_headings) - len(result_headings)
        logger.warning(
            "generate_final_document: translated result has fewer sections than donor template — %d vs %d",
            len(result_headings), len(template_headings),
        )
        fix_prompt = (
            f"{SYSTEM_PROMPT}\n\nТы переводил форму донора на другой язык, но "
            f"результат содержит МЕНЬШЕ разделов ({len(result_headings)}), чем "
            f"оригинал ({len(template_headings)}) — вот структура ОРИГИНАЛА:\n\n"
            f"{donor_template}\n\nПерепиши перевод ЗАНОВО, сохранив ВСЕ разделы "
            f"оригинала (переведи названия, но не пропускай и не объединяй их)."
            f"{doc_language_clause(session_data)}"
        )
        try:
            return await call_claude_required(
                fix_prompt, f"Предыдущий (неполный) перевод:\n{result[:6000]}", max_tokens=8000,
            )
        except LLMEmptyResponseError:
            return result

    # Простое сравнение по нормализованному множеству — не требуем точного
    # порядка (это отдельная забота промпта), только что заголовки формы
    # донора РЕАЛЬНО присутствуют в результате, а не заменены/переименованы.
    normalize = lambda s: s.lower().strip().rstrip(":?.")
    template_set = {normalize(h) for h in template_headings if len(h) > 3}
    result_set = {normalize(h) for h in result_headings}
    missing = [h for h in template_headings if len(h) > 3 and normalize(h) not in result_set]

    # РЕАЛЬНЫЙ ИНЦИДЕНТ: раздела "План действий"/"Бюджет проекта" пропадали
    # из готовой заявки трижды подряд — оказалось, что 25%-допуск ниже
    # (рассчитан на безобидные случаи вроде "донор дал длинный список
    # мелких полей, часть из них естественно слилась в один абзац")
    # МОЛЧА пропускал и пропажу САМЫХ содержательных разделов, если их
    # доля от общего числа заголовков формы укладывалась в 25%. Бюджет и
    # план действий — никогда не «мелкие поля», их пропажа обязана
    # запускать переисправление независимо от процента.
    missing_critical = [h for h in missing if _is_critical_heading(h)]

    if not missing or (not missing_critical and len(missing) <= max(1, len(template_headings) // 4)):
        return result

    logger.warning(
        "generate_final_document: result deviates from donor template structure — missing %d/%d headings: %s",
        len(missing), len(template_headings), missing[:5],
    )
    critical_note = (
        f"\n\nОСОБО ВАЖНО: среди пропущенных разделов есть КРИТИЧЕСКИ важные "
        f"({', '.join(missing_critical)}) — план действий/мероприятий и бюджет "
        f"НИКОГДА нельзя оставлять пустыми или опускать, даже если для них не "
        f"хватает точных данных: используй правило плейсхолдеров XYZ и всё "
        f"равно распиши раздел содержательно (примерные мероприятия по каждой "
        f"задаче, примерная структура бюджета по стандартным категориям)."
        if missing_critical else ""
    )
    fix_prompt = (
        f"{SYSTEM_PROMPT}\n\nТы только что заполнил форму донора, но "
        f"ИЗМЕНИЛ её структуру — вот структура ОРИГИНАЛА, которой нужно "
        f"следовать буква в букву:\n\n{donor_template}\n\n"
        f"В твоём результате отсутствуют или переименованы эти разделы/поля "
        f"оригинала:\n" + "\n".join(f"- {h}" for h in missing[:15]) + "\n\n"
        f"Перепиши документ ЗАНОВО, строго следуя структуре оригинала выше — "
        f"не добавляй разделов, которых нет в оригинале (например, не "
        f"придумывай 'рабочий план'/'логфрейм', если донор его не просил), "
        f"не переименовывай заголовок документа и названия полей, не "
        f"удаляй обязательные пункты формы. Используй содержание из "
        f"предыдущей версии там, где оно соответствует правильным полям."
        f"{critical_note}"
        f"{doc_language_clause(session_data)}"
    )
    try:
        fixed = await call_claude_required(
            fix_prompt,
            f"Предыдущий (неверный по структуре) результат:\n{result[:6000]}",
            max_tokens=8000,
        )
        return fixed
    except LLMEmptyResponseError:
        # Не можем исправить — лучше вернуть исходный результат, чем упасть
        # с ошибкой на этапе, где документ технически уже есть.
        logger.warning("generate_final_document: structure fix attempt failed, returning original result")
        return result


async def answer_user_question(question: str, context_label: str, session_data: dict | None = None) -> str:
    """Короткий деловой ответ на вопрос пользователя, заданный вместо (или
    вместе с) присылки данных на очередном шаге (например, 'этого
    достаточно?', 'а что дальше?', 'нужно ли ещё что-то?').

    РАНЬШЕ такие сообщения молча проглатывались как 'данные' шага, и бот
    просто ехал дальше по сценарию, полностью игнорируя заданный вопрос —
    выглядело так, будто он не слушает пользователя. Ответ здесь должен
    быть МАКСИМАЛЬНО коротким (1-2 предложения) — пользователь прямо
    попросил делового собеседника без лишних слов, не абзац рассуждений.
    """
    known_context = _session_summary(session_data) if session_data else ""
    context_block = f"\n\nКонтекст проекта:\n{known_context}" if known_context.strip() else ""
    system_prompt = (
        "Ты — краткий деловой ассистент по разработке грантовых заявок/"
        "бизнес-планов. Пользователь сейчас на шаге "
        f"'{context_label}' и задал короткий вопрос вместо (или вместе с) "
        "присылки данных. Ответь МАКСИМАЛЬНО коротко — 1-2 предложения, "
        "без вводных слов, без длинных объяснений. Отвечай по существу "
        "вопроса, опираясь на контекст проекта, если он есть."
        f"{context_block}{ui_language_clause(session_data)}"
    )
    try:
        return (await call_claude(system_prompt, question, max_tokens=300)).strip()
    except Exception as exc:
        logger.warning("answer_user_question failed: %s: %s", type(exc).__name__, exc)
        return ""


REFUSAL_MARKERS = (
    "не вижу файлов", "не могу гарантировать", "начинаем с чистого листа",
    "предлагаю честный", "пришлите, пожалуйста", "без него я не могу",
    "не могу просто", "мне нужно от вас", "готов начать, как только",
    "прежде чем продолжить", "не хватает данных", "не могу собрать",
    "нужна дополнительная информация",
)


def looks_like_refusal_or_question(text: str) -> bool:
    """Детектор для РЕАЛЬНОГО ИНЦИДЕНТА: модель вернула текстовый отказ/
    уточняющий вопрос вместо заполненного документа (например, в ответ на
    команду рестарта, ошибочно принятую за 'правку'), а бот всё равно
    упаковал этот текст в .docx и заявил пользователю 'документ готов по
    структуре донора'. Ищем явные словесные маркеры отказа/просьбы данных
    ИЛИ структурные признаки (короткий текст без похожих на форму
    заголовков/полей) — если совпадает, документ отправлять НЕЛЬЗЯ, вместо
    этого нужно показать текст модели пользователю как обычное сообщение."""
    t = text.strip().lower()
    if not t:
        return True
    if any(marker in t for marker in REFUSAL_MARKERS):
        return True
    # Короткий текст (типичный отказ/диалоговая реплика) без табличных/
    # заголовочных маркеров документа — тоже подозрительно.
    if len(text.strip()) < 400 and "#" not in text and "|" not in text:
        return True
    return False


def looks_like_question(text: str) -> bool:
    """Дешёвая эвристика (без LLM-вызова) — короткое сообщение,
    заканчивающееся на '?', похоже на вопрос к боту, а не на содержательные
    данные шага (профиль организации, описание проблемы и т.п. обычно
    длиннее и не оформлены как вопрос)."""
    t = text.strip()
    return t.endswith("?") and len(t) <= 120


async def summarize_understanding(label: str, content: str) -> str:
    """Однострочное подтверждение "что бот понял" после шага intake.

    Используется, чтобы пользователь сразу видел, что информация дошла и
    была прочитана правильно — вместо молчаливого перехода к следующему
    вопросу, из-за которого не ясно, обработал ли бот присланное вообще.
    """
    if not content.strip():
        return ""
    system_prompt = (
        "Прочитай присланный текст/документ и одним коротким предложением "
        "(не более ~20 слов) скажи, что из него понял — конкретно, без "
        "вводных фраз вроде 'Я понял, что' или 'Итак'. Пиши как факт. "
        "Если текст пустой или бессодержательный, ответь пустой строкой."
    )
    try:
        summary = await call_claude(system_prompt, f"{label}:\n{content[:4000]}")
        return summary.strip()
    except Exception:
        return ""


def _session_summary(session_data: dict) -> str:
    parts = []
    for key in (
        "org_info", "donor_info", "donor_forms_text", "selected_idea", "project_data",
        "goal_and_objectives", "action_trees", "concept_text", "budget_text",
    ):
        value = session_data.get(key)
        if not value:
            continue
        # donor_forms_text может содержать несколько скачанных PDF/DOCX
        # (до ~6000 символов каждый) — структура формы уже вытащена
        # отдельно в donor_template, так что здесь достаточно урезанной
        # версии как справочного контекста, не раздувая промпт до отказа
        # модели отвечать (пустой текст при max_tokens, съеденном длинным
        # контекстом/reasoning до появления первого текстового блока).
        if key == "donor_forms_text" and len(value) > 3000:
            value = value[:3000] + "\n[...текст формы обрезан, полная структура уже учтена отдельно...]"
        parts.append(f"{key}: {value}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Red-flags аудит (§1.2 плейбука) — отдельный структурированный вызов
# ---------------------------------------------------------------------------

RED_FLAG_LIST = """
1. Проблема и решение логически не связаны
2. Нет данных, подтверждающих проблему
3. Цели сформулированы как активности, а не как изменение ("провести 10
   тренингов" — это задача, а не цель)
4. Бюджет не соответствует описанной деятельности
5. Административные расходы превышают 20%
6. Нет плана устойчивости после окончания гранта
7. Нет SMART-индикаторов
8. Нет раздела рисков
9. Целевая аудитория описана слишком широко ("молодёжь страны" — это не
   целевая аудитория)
10. Компетенции команды не соответствуют масштабу проекта
11. Запрошенный бюджет на максимуме потолка донора (выглядит как подгонка
    под цифру)
12. Дублирует существующие проекты без объяснения уникальности
13. Не указаны партнёры/заинтересованные стороны
14. Нет софинансирования или вклада благополучателей
15. Подача с опозданием или неполный пакет документов
16. Собственный шаблон донора переформатирован визуальным стилем автора
""".strip()

RED_FLAG_SYSTEM_PROMPT = f"""
Ты — строгий грантовый ревьюер. Проверь присланный текст (концепт или
полную версию заявки/бизнес-плана) на следующие 16 критических ошибок:

{RED_FLAG_LIST}

РЕАЛЬНЫЙ ИНЦИДЕНТ: флаг №15 ("неполный пакет документов") ловил требование
донора "PDF с подписью" как проблему ТЕКСТА документа — но это процедурное
требование к ФОРМАТУ ФАЙЛА при физической подаче (сохранить как PDF,
подписать от руки/ЭЦП), а не к содержанию заявки. autofix переписывает
ТЕКСТ документа и физически не может решить это — то же самое замечание
находилось на каждой следующей проверке, зацикливая пользователя в
бесконечном цикле "исправь -> та же ошибка -> исправь снова". Флаг №15
применяй ТОЛЬКО к реально недостающему СОДЕРЖАНИЮ (например, не хватает
целого обязательного раздела формы, не заполнено обязательное поле) —
НИКОГДА не поднимай флаг №15 из-за требований к физическому формату файла,
подписи, печати или способу доставки (email/почта/PDF/сканы) — это
инструкции для пользователя ПЕРЕД отправкой, не ошибка составленного
документа, и должны быть просто проигнорированы этой проверкой.

Верни СТРОГО валидный JSON без markdown-обрамления, в формате:
{{
  "passed": true/false,
  "issues": [
    {{"flag": <номер 1-16>, "description": "<что не так, коротко>",
      "quote": "<цитата из текста, где видна проблема, или пусто>"}}
  ]
}}
"passed": true только если issues пуст. Если сомневаешься, не находи
ошибку "для галочки" — отмечай только реальные, конкретные проблемы,
которые видны непосредственно в тексте.
""".strip()


async def check_red_flags(document_text: str, session_data: dict) -> dict:
    """Прогоняет текст по 16 критическим ошибкам плейбука.

    Возвращает {"passed": bool, "issues": [...]}.

    ВАЖНО (баг, который был здесь раньше): если модель добавляет хоть
    слово текста до/после JSON, наивный json.loads() падает. Раньше это
    трактовалось как "непройденная проверка" с фиктивной проблемой №0
    ("ошибка разбора ответа") — которую autofix_red_flags не мог
    исправить, потому что это не проблема ТЕКСТА, а проблема формата
    ответа модели. Из-за этого пользователь попадал в бесконечный цикл:
    проверка -> фиктивная "проблема" -> попытка исправить текст (не
    помогает, потому что нечего чинить) -> проверка снова -> та же
    ошибка формата -> и так по кругу.

    Теперь: (1) JSON извлекается по первой '{' и последней '}' — переживает
    случайный текст до/после; (2) при неудаче — один явный повторный запрос
    с напоминанием вернуть строго JSON; (3) если и это не помогло — считаем
    проверку ПРОЙДЕННОЙ (fail-open, не fail-closed), потому что бесконечно
    блокировать пользователя из-за бага формата хуже, чем изредка пропустить
    аудит. Ошибка тихо логируется для диагностики, не показывается как
    "проблема" пользователю.
    """
    parsed = await _try_red_flags_call(document_text, session_data)
    if parsed is not None:
        return parsed
    parsed = await _try_red_flags_call(
        document_text, session_data,
        extra_instruction="\n\nВАЖНО: предыдущий ответ не был валидным JSON. "
        "Верни ТОЛЬКО JSON-объект, без единого слова до или после него, "
        "без markdown-обрамления.",
    )
    if parsed is not None:
        return parsed
    logger.warning("check_red_flags: JSON parse failed twice, failing open (treating as passed)")
    return {"passed": True, "issues": []}


async def _try_red_flags_call(
    document_text: str, session_data: dict, extra_instruction: str = ""
) -> dict | None:
    """Один вызов + попытка распарсить. Возвращает None при неудаче
    (вызывающий код решает, ретраить или сдаться), словарь при успехе."""
    user_message = (
        f"Текст для проверки:\n\n{document_text}\n\n"
        f"Контекст сессии (для оценки соответствия бюджета/масштаба):\n"
        f"{_session_summary(session_data)}{extra_instruction}"
    )
    try:
        raw = await call_claude(RED_FLAG_SYSTEM_PROMPT, user_message, max_tokens=4000)
    except Exception as exc:
        logger.warning("check_red_flags: call_claude failed: %s: %s", type(exc).__name__, exc)
        return None
    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise ValueError("no JSON object found in response")
        result = json.loads(raw[start:end + 1])
        if not isinstance(result, dict) or "passed" not in result:
            raise ValueError("unexpected shape")
        result.setdefault("issues", [])
        return result
    except Exception as exc:
        logger.warning("check_red_flags: JSON parse failed: %s: %s | raw[:200]=%r", type(exc).__name__, exc, raw[:200])
        return None


async def autofix_red_flags(document_text: str, issues: list[dict], session_data: dict) -> str:
    """Просит модель переписать документ, устранив найденные проблемы,
    сохраняя markdown-структуру заголовков."""
    issues_text = "\n".join(
        f"- [Флаг №{i.get('flag')}] {i.get('description')}"
        + (f" (цитата: \"{i.get('quote')}\")" if i.get("quote") else "")
        for i in issues
    )
    system_prompt = (
        f"{SYSTEM_PROMPT}\n\nВот документ с найденными проблемами. Перепиши "
        f"его целиком, устранив ВСЕ перечисленные проблемы, сохранив "
        f"markdown-заголовки вида '## Название раздела'. Не добавляй новых "
        f"разделов сверх исходной структуры."
    )
    user_message = (
        f"Документ:\n\n{document_text}\n\nНайденные проблемы:\n{issues_text}"
    )
    return await call_claude(system_prompt, user_message)


def format_issues_for_user(issues: list[dict]) -> str:
    lines = ["⚠️ Перед отправкой нашёл несколько мест, которые стоит поправить:"]
    for i in issues:
        flag = i.get("flag")
        prefix = f"[Флаг №{flag}] " if flag else ""
        lines.append(f"• {prefix}{i.get('description', '')}")
    return "\n".join(lines)
