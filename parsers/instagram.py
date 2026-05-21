"""
Парсер Instagram — мониторит хэштеги в поисках новых заведений.
Использует httpx для получения публичных страниц хэштегов.
"""
import asyncio
import logging
import re
import random
from typing import AsyncIterator

import httpx

import state
from config import INSTAGRAM_HASHTAGS, PAUSE_INSTAGRAM, OPENING_KEYWORDS

logger = logging.getLogger(__name__)

# Заголовки браузера для обхода блокировок
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Ключевые слова для фильтрации постов
FOOD_KEYWORDS = [
    "ресторан", "кафе", "cafe", "restaurant", "food", "еда",
    "ошхона", "чойхона", "пиццерия", "суши", "доставка", "меню",
    "кухня", "повар", "chef", "открытие", "open", "новый",
]


def _extract_phone(text: str) -> str | None:
    """Извлекает телефонный номер из текста"""
    patterns = [
        r"\+998[\s\-]?\d{2}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}",
        r"998[\s\-]?\d{2}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}",
        r"\+7[\s\-]?\d{3}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}",
        r"8[\s\-]?\d{3}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}",
        r"\d{2}[\s\-]\d{3}[\s\-]\d{2}[\s\-]\d{2}",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group().strip()
    return None


def _has_opening_signal(text: str) -> bool:
    """Проверяет есть ли сигнал об открытии заведения"""
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in OPENING_KEYWORDS)


def _is_food_related(text: str) -> bool:
    """Проверяет относится ли пост к еде/ресторанному бизнесу"""
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in FOOD_KEYWORDS)


async def _fetch_hashtag_page(client: httpx.AsyncClient, hashtag: str) -> list[dict]:
    """
    Пытается получить посты по хэштегу через веб-интерфейс Instagram.
    Instagram сильно блокирует автоматический доступ, поэтому
    возвращаем пустой список при блокировке и логируем.
    """
    results = []
    url = f"https://www.instagram.com/explore/tags/{hashtag}/?__a=1&__d=dis"

    try:
        await asyncio.sleep(random.uniform(3, 7))  # имитация человека

        resp = await client.get(url, headers=HEADERS, timeout=15, follow_redirects=True)

        if resp.status_code == 200:
            try:
                data = resp.json()
                # Пробуем извлечь посты из JSON ответа
                edges = (
                    data.get("graphql", {})
                    .get("hashtag", {})
                    .get("edge_hashtag_to_media", {})
                    .get("edges", [])
                )
                for edge in edges[:10]:
                    node = edge.get("node", {})
                    caption_edges = node.get("edge_media_to_caption", {}).get("edges", [])
                    caption = ""
                    if caption_edges:
                        caption = caption_edges[0].get("node", {}).get("text", "")

                    if not _is_food_related(caption):
                        continue

                    shortcode = node.get("shortcode", "")
                    owner = node.get("owner", {})

                    result = {
                        "name": f"Instagram #{hashtag}",
                        "source": "instagram",
                        "hashtag": hashtag,
                        "caption": caption[:500],
                        "phone": _extract_phone(caption),
                        "instagram": f"https://www.instagram.com/p/{shortcode}/" if shortcode else None,
                        "url": f"https://www.instagram.com/p/{shortcode}/" if shortcode else None,
                        "opening_signal": _has_opening_signal(caption),
                        "owner_username": owner.get("username"),
                        "raw_text": caption,
                    }
                    # Имя заведения из первой строки caption
                    if caption:
                        first_line = caption.split("\n")[0].strip()
                        if 3 < len(first_line) < 100:
                            result["name"] = first_line

                    results.append(result)

            except Exception:
                pass  # JSON не распарсился — Instagram заблокировал

        elif resp.status_code == 401:
            logger.debug(f"Instagram требует авторизацию для #{hashtag}")
        elif resp.status_code == 429:
            logger.warning(f"Instagram: слишком много запросов (429) для #{hashtag}")
            await asyncio.sleep(120)
        else:
            logger.debug(f"Instagram #{hashtag}: статус {resp.status_code}")

    except httpx.TimeoutException:
        logger.debug(f"Instagram #{hashtag}: таймаут")
    except Exception as e:
        logger.error(f"Ошибка Instagram #{hashtag}: {e}")

    # Дополнительно пробуем через мобильный API
    if not results:
        results = await _fetch_hashtag_mobile(client, hashtag)

    return results


async def _fetch_hashtag_mobile(client: httpx.AsyncClient, hashtag: str) -> list[dict]:
    """
    Альтернативный способ — через Instagram мобильный API.
    """
    results = []
    try:
        url = f"https://i.instagram.com/api/v1/tags/{hashtag}/sections/"
        mobile_headers = {
            **HEADERS,
            "User-Agent": "Instagram 219.0.0.12.117 Android",
            "X-IG-App-ID": "936619743392459",
        }
        resp = await client.get(url, headers=mobile_headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            sections = data.get("sections", [])
            for section in sections:
                for item in section.get("layout_content", {}).get("medias", []):
                    media = item.get("media", {})
                    caption_text = ""
                    caption_obj = media.get("caption")
                    if caption_obj and isinstance(caption_obj, dict):
                        caption_text = caption_obj.get("text", "")

                    if not _is_food_related(caption_text):
                        continue

                    code = media.get("code", "")
                    user = media.get("user", {})

                    result = {
                        "name": user.get("full_name") or user.get("username", f"Instagram #{hashtag}"),
                        "source": "instagram",
                        "hashtag": hashtag,
                        "caption": caption_text[:500],
                        "phone": _extract_phone(caption_text),
                        "instagram": f"https://www.instagram.com/{user.get('username', '')}/",
                        "url": f"https://www.instagram.com/p/{code}/" if code else None,
                        "opening_signal": _has_opening_signal(caption_text),
                        "owner_username": user.get("username"),
                        "raw_text": caption_text,
                    }
                    results.append(result)
    except Exception as e:
        logger.debug(f"Instagram mobile API #{hashtag}: {e}")

    return results


async def fetch_new_items() -> list[dict]:
    """Собирает новые посты по всем хэштегам"""
    all_results = []
    async with httpx.AsyncClient() as client:
        for hashtag in INSTAGRAM_HASHTAGS:
            logger.info(f"Instagram: проверяю #{hashtag}")
            items = await _fetch_hashtag_page(client, hashtag)
            all_results.extend(items)
            logger.info(f"Instagram #{hashtag}: нашёл {len(items)} постов")
            await asyncio.sleep(random.uniform(5, 15))  # антибан

    return all_results


async def run_forever(process_func):
    """
    Бесконечный цикл мониторинга Instagram.
    process_func — async функция обработки одного лида.
    """
    logger.info("Парсер Instagram запущен")

    while True:
        if state.is_paused():
            await asyncio.sleep(60)
            continue

        try:
            items = await fetch_new_items()
            logger.info(f"Instagram: всего найдено {len(items)} постов")

            for item in items:
                try:
                    await process_func(item)
                except Exception as e:
                    logger.error(f"Ошибка обработки Instagram-поста: {e}")

        except Exception as e:
            logger.error(f"Ошибка парсера Instagram: {e}")
            await asyncio.sleep(30)

        logger.info(f"Instagram: следующий обход через {PAUSE_INSTAGRAM // 60} минут")
        await asyncio.sleep(PAUSE_INSTAGRAM)
