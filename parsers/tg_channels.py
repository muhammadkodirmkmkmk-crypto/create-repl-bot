"""
Парсер Telegram-каналов — мониторит публичные каналы через веб-интерфейс t.me.
Не требует Telegram API ключей — парсит публичные страницы.
"""
import asyncio
import logging
import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

import state
from config import TG_CHANNELS, OPENING_KEYWORDS, PAUSE_TG_CHANNELS

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# Ключевые слова ресторанной тематики
FOOD_KEYWORDS = [
    "ресторан", "кафе", "cafe", "restaurant", "food", "открытие",
    "открываемся", "grand opening", "ошхона", "yangi", "ochilish",
    "меню", "повар", "кухня", "доставка", "пиццерия", "суши",
    "кондитерская", "пекарня", "fastfood", "фастфуд",
]


def _extract_phone(text: str) -> str | None:
    patterns = [
        r"\+998[\s\-]?\d{2}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}",
        r"998\d{9}",
        r"\+7[\s\-]?\d{3}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group().strip()
    return None


def _is_relevant(text: str) -> bool:
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in FOOD_KEYWORDS)


def _has_opening_signal(text: str) -> bool:
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in OPENING_KEYWORDS)


def _extract_venue_name(text: str) -> str:
    """Пытается извлечь название заведения из текста поста"""
    # Паттерны: "Ресторан «Название»" или "Кафе "Название""
    patterns = [
        r'(?:ресторан|кафе|cafe|restaurant|ошхона)\s+[«"\'"]([^»"\'"\n]{3,60})[»"\'"]',
        r'(?:открылся|открылась|открылся|открываем)\s+[«"\'"]([^»"\'"\n]{3,60})[»"\'"]',
        r'[«"\'"]([A-Za-zА-Яа-яЁёÜüÖöÄä\s]{3,40})[»"\'"]',
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()

    # Берём первую строку если она похожа на название
    first_line = text.split("\n")[0].strip()
    if 3 < len(first_line) < 80 and not first_line.startswith("http"):
        return first_line

    return "Новое заведение (Telegram)"


async def _fetch_channel(client: httpx.AsyncClient, channel: str) -> list[dict]:
    """Получает последние посты из публичного Telegram-канала"""
    results = []
    url = f"https://t.me/s/{channel}"

    try:
        resp = await client.get(url, headers=HEADERS, timeout=20, follow_redirects=True)

        if resp.status_code != 200:
            logger.debug(f"TG канал @{channel}: статус {resp.status_code}")
            return results

        soup = BeautifulSoup(resp.text, "lxml")

        # Ищем сообщения канала
        messages = soup.select(".tgme_widget_message")

        if not messages:
            messages = soup.select("[data-post]")

        for msg in messages[-20:]:  # последние 20 сообщений
            try:
                # Текст поста
                text_el = msg.select_one(".tgme_widget_message_text")
                if not text_el:
                    continue
                text = text_el.get_text("\n", strip=True)

                if not text or not _is_relevant(text):
                    continue

                # Дата
                date_el = msg.select_one("time")
                post_date = date_el.get("datetime", "") if date_el else ""

                # Ссылка на пост
                link_el = msg.select_one(".tgme_widget_message_date")
                post_link = link_el.get("href", "") if link_el else ""
                if not post_link:
                    post_link_el = msg.select_one("a[href*='t.me']")
                    post_link = post_link_el.get("href", "") if post_link_el else f"https://t.me/{channel}"

                # Фото/медиа
                has_media = bool(msg.select(".tgme_widget_message_photo"))

                result = {
                    "name": _extract_venue_name(text),
                    "source": "tg_channels",
                    "channel": channel,
                    "raw_text": text[:800],
                    "phone": _extract_phone(text),
                    "instagram": None,
                    "url": post_link,
                    "post_date": post_date,
                    "opening_signal": _has_opening_signal(text),
                    "has_media": has_media,
                }

                # Ищем Instagram в тексте
                ig_match = re.search(r"@([a-zA-Z0-9._]+)", text)
                if ig_match:
                    result["instagram"] = f"https://www.instagram.com/{ig_match.group(1)}/"

                results.append(result)

            except Exception as e:
                logger.debug(f"TG @{channel}: ошибка парсинга поста: {e}")

    except httpx.TimeoutException:
        logger.warning(f"TG @{channel}: таймаут")
    except Exception as e:
        logger.error(f"TG @{channel}: ошибка: {e}")

    return results


async def fetch_new_items() -> list[dict]:
    """Собирает новые посты из всех Telegram-каналов"""
    all_results = []

    async with httpx.AsyncClient() as client:
        for channel in TG_CHANNELS:
            logger.info(f"Telegram: проверяю @{channel}")
            items = await _fetch_channel(client, channel)
            all_results.extend(items)
            logger.info(f"Telegram @{channel}: нашёл {len(items)} постов")
            await asyncio.sleep(5)

    return all_results


async def run_forever(process_func):
    """
    Бесконечный цикл мониторинга Telegram-каналов.
    process_func — async функция обработки одного лида.
    """
    logger.info("Парсер Telegram-каналов запущен")

    while True:
        if state.is_paused():
            await asyncio.sleep(60)
            continue

        try:
            items = await fetch_new_items()
            logger.info(f"Telegram: всего найдено {len(items)} постов")

            for item in items:
                try:
                    await process_func(item)
                except Exception as e:
                    logger.error(f"Ошибка обработки TG-поста: {e}")

        except Exception as e:
            logger.error(f"Ошибка парсера Telegram: {e}")
            await asyncio.sleep(30)

        logger.info(f"Telegram: следующий обход через {PAUSE_TG_CHANNELS} сек")
        await asyncio.sleep(PAUSE_TG_CHANNELS)
