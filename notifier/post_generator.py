"""
Генерация iiko-постов для Telegram-канала на основе найденных лидов.
Максимум MAX_POSTS_PER_DAY постов в день, минимум MIN_HOURS_BETWEEN_POSTS между постами.
"""
import asyncio
import logging
import random
from datetime import datetime

import anthropic
import httpx

from config import (
    ANTHROPIC_API_KEY, CLAUDE_MODEL,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID,
    MAX_POSTS_PER_DAY, MIN_HOURS_BETWEEN_POSTS,
)
import state

logger = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# ── Format wheel: 8 distinct formats, chosen randomly each time ───────────────
FORMAT_WHEEL = [
    (
        "ФОРМАТ — ЛОВУШКА:\n"
        "Первые 2 строки — ложное убеждение, которое кажется правильным большинству рестораторов Ташкента. "
        "Сформулируй его уверенно, как общепринятую истину.\n"
        "Потом резкий разворот: «На самом деле...» — разбей убеждение конкретными цифрами.\n"
        "Объясни механику: почему ошибка распространённая и сколько она стоит в сумах.\n"
        "Финал: как iiko показывает правду — конкретная функция + результат анонимного заведения."
    ),
    (
        "ФОРМАТ — ЖИВАЯ СЦЕНА:\n"
        "Первые 3 строки — кинематографичная сцена из ташкентского ресторана. "
        "Конкретное время (пятница 20:00), место (кухня, касса, зал), человек (шеф, кассир, управляющий), действие. "
        "Без имён заведений. Читатель должен увидеть картинку.\n"
        "Следующий абзац: почему эта сцена — симптом системной проблемы, цифры потерь.\n"
        "Затем: что в iiko закрывает это — конкретная функция, как работает.\n"
        "Финал: измеримый результат, анонимное заведение."
    ),
    (
        "ФОРМАТ — ДО/ПОСЛЕ:\n"
        "Опиши одну конкретную операцию ДО iiko: ручной процесс, потери в сумах или часах, хаос. "
        "Без воды, только боль и цифры.\n"
        "Потом то же самое ПОСЛЕ iiko: конкретная функция, сколько времени или денег экономит каждый месяц. "
        "Только контраст, никаких рекламных слов.\n"
        "Финал: конкретная разница в сумах или процентах, анонимное заведение Ташкента."
    ),
    (
        "ФОРМАТ — ПРОВОКАЦИЯ:\n"
        "Начни с резкого вопроса или спорного тезиса который задевает ресторатора. "
        "Пример уровня: «Вы теряете X сум каждый день — и даже не знаете об этом» или "
        "«Почему 80% ресторанов Ташкента считают деньги в Excel и удивляются убыткам».\n"
        "Следующий абзац: докажи тезис — конкретная механика потерь, цифры.\n"
        "Затем: решение через iiko — точное название функции, как использовать.\n"
        "Финал: результат, анонимный кейс."
    ),
    (
        "ФОРМАТ — МИНИ-ИСТОРИЯ:\n"
        "Короткая история с началом, проблемой и развязкой. "
        "Герой — анонимный ресторатор из Ташкента (без имён и названий). "
        "Начало: всё шло как обычно. Проблема: что-то пошло не так, цифры потерь. "
        "Развязка: нашли в iiko, настроили конкретную функцию, получили результат.\n"
        "Эмоциональная дуга: от тревоги к облегчению. Реалистичные цифры."
    ),
    (
        "ФОРМАТ — ЦИФРА В ЛОБ:\n"
        "Первая строка — одно shocking число крупно и без предисловий. "
        "Пример: «3 400 000 сум. Столько теряет средний ташкентский ресторан за год на немаркированных списаниях».\n"
        "Следующий абзац: откуда эта цифра берётся, операционная механика, кто в зоне риска.\n"
        "Затем: как iiko это видит и контролирует — конкретная функция.\n"
        "Финал: что изменилось после настройки, анонимный пример."
    ),
    (
        "ФОРМАТ — ДИАЛОГ:\n"
        "Начни с короткого диалога (3-4 реплики) между управляющим и официантом, поваром или кассиром. "
        "Диалог показывает проблему — живая речь, без лишних слов. Без имён и названий заведений.\n"
        "После диалога: объясни почему это системная проблема, цифры потерь.\n"
        "Затем: как iiko убирает эту проблему — конкретная функция, шаги.\n"
        "Финал: результат в числах."
    ),
    (
        "ФОРМАТ — АНТИСОВЕТ:\n"
        "Начни с «Не делайте X» — типичная ошибка при настройке или использовании iiko, "
        "которую допускают большинство ресторанов Ташкента.\n"
        "Объясни что ломается от этой ошибки: конкретная цепочка последствий, потери в сумах.\n"
        "Затем: как делать правильно — конкретные шаги, точное название функции iiko.\n"
        "Финал: результат после исправления, анонимный кейс."
    ),
]

PERSONA = (
    "Ты — сертифицированный специалист по внедрению iiko. "
    "8 лет опыта, 160+ ресторанов в Узбекистане. "
    "Говоришь как практик: точно, конкретно, без академической воды."
)


def _build_topic(raw_data: dict, decision: dict) -> str:
    """Строим тему поста из данных лида."""
    name = raw_data.get("name", "")
    source = raw_data.get("source", "")
    reasoning = decision.get("reasoning", "")
    opening_soon = decision.get("lead_quality", {}).get("opening_soon", False)

    parts = []
    if opening_soon:
        parts.append(f"Новое заведение открывается в Ташкенте")
    if name:
        parts.append(f"заведение типа «{name}»")
    if reasoning:
        parts.append(reasoning[:120])

    return " — ".join(parts) if parts else "Автоматизация ресторана в Ташкенте через iiko"


def generate_channel_post(raw_data: dict, decision: dict) -> str:
    """Генерирует пост для Telegram-канала через Claude."""
    fmt = random.choice(FORMAT_WHEEL)
    topic = _build_topic(raw_data, decision)

    prompt = (
        f"{PERSONA}\n\n"
        f"ТЕМА: {topic}\n\n"
        f"{fmt}\n\n"
        f"ЖЁСТКИЕ ПРАВИЛА:\n"
        f"— Контекст ТОЛЬКО Узбекистан/Ташкент: цифры, районы, реалии местного рынка\n"
        f"— Цифры обязательны: суммы в сумах, проценты, временные показатели\n"
        f"— Анонимные примеры только: «один ташкентский ресторан», «сеть кафе в Узбекистане» и т.п.\n"
        f"— НЕ реклама: никогда «купи iiko», «Zetta Group», «обратитесь к нам»\n"
        f"— 180-220 слов, только русский язык\n"
        f"— Текст должен быть завершён полностью — никогда не обрывай на полуслове\n"
        f"— Эмодзи: 2-4 штуки, только по смыслу — не декоративные\n"
        f"— ЗАПРЕЩЕНО начинать пост словами: «Лайфхак», «Совет», «Внимание», «Представьте»\n"
        f"— Последняя строка без изменений: «Связаться: @iikoman»\n"
        f"— Только текст поста, без заголовков типа «Пост:» и без пояснений"
    )

    logger.info(f"Генерирую пост для канала | формат: {fmt[:25].strip()!r}")

    msg = _client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1400,
        messages=[{"role": "user", "content": prompt}],
    )
    post = msg.content[0].text.strip()

    if "Связаться: @iikoman" not in post:
        post += "\n\nСвязаться: @iikoman"

    return post


async def send_to_channel(text: str) -> bool:
    """Отправляет пост в Telegram-канал. Возвращает True при успехе."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        logger.warning("TELEGRAM_BOT_TOKEN или TELEGRAM_CHANNEL_ID не заданы — пропускаю")
        return False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{TG_API}/sendMessage",
                json={
                    "chat_id": TELEGRAM_CHANNEL_ID,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            data = resp.json()
            if data.get("ok"):
                logger.info(f"Пост опубликован в канале (msg_id={data['result']['message_id']})")
                return True
            else:
                logger.error(f"Telegram API ошибка: {data.get('description')}")
                # Retry without markdown
                resp2 = await client.post(
                    f"{TG_API}/sendMessage",
                    json={"chat_id": TELEGRAM_CHANNEL_ID, "text": text,
                          "disable_web_page_preview": True},
                    timeout=15,
                )
                data2 = resp2.json()
                if data2.get("ok"):
                    logger.info("Пост опубликован без markdown")
                    return True
                logger.error(f"Повторная ошибка: {data2.get('description')}")
                return False
    except Exception as e:
        logger.error(f"Ошибка отправки в канал: {e}")
        return False


async def try_post_to_channel(raw_data: dict, decision: dict) -> bool:
    """
    Проверяет лимиты, генерирует пост и публикует в канал.
    Возвращает True если пост был опубликован.
    """
    if not TELEGRAM_CHANNEL_ID:
        logger.warning("TELEGRAM_CHANNEL_ID не задан — публикация в канал отключена")
        return False

    if not state.can_post():
        logger.info(
            f"Лимит постов: {state.posts_today()}/{MAX_POSTS_PER_DAY} сегодня, "
            f"последний пост {state.hours_since_last_post():.1f}ч назад"
        )
        return False

    try:
        loop = asyncio.get_event_loop()
        post_text = await loop.run_in_executor(
            None, lambda: generate_channel_post(raw_data, decision)
        )
        success = await send_to_channel(post_text)
        if success:
            state.record_post()
            logger.info(f"✅ Канал: пост опубликован ({state.posts_today()}/{MAX_POSTS_PER_DAY} сегодня)")
        return success
    except Exception as e:
        logger.error(f"Ошибка генерации/публикации поста: {e}")
        return False
