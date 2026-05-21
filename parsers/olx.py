"""
Парсер OLX.uz — мониторит объявления об аренде коммерческой недвижимости
(помещения под кафе, рестораны, общепит).
"""
import asyncio
import logging
import re
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from config import OLX_KEYWORDS, PAUSE_OLX

logger = logging.getLogger(__name__)

BASE_URL = "https://www.olx.uz"
SEARCH_URL = f"{BASE_URL}/uz/nedvizhimost/kommercheskaya-nedvizhimost/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": BASE_URL,
}

# Ключевые слова которые указывают на новое заведение
POSITIVE_KEYWORDS = [
    "под кафе", "под ресторан", "под общепит", "ошхона", "фудкорт",
    "кафе", "ресторан", "общепит", "food", "кухня", "столовая",
    "под кондитерскую", "под пекарню", "fastfood", "фастфуд",
]


def _extract_phone(text: str) -> str | None:
    """Извлекает телефон из текста объявления"""
    patterns = [
        r"\+998[\s\-]?\d{2}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}",
        r"998[\s\-]?\d{2}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}",
        r"\d{2}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group().strip()
    return None


def _is_relevant(title: str, description: str) -> bool:
    """Проверяет, подходит ли объявление под поиск ресторанного бизнеса"""
    text = (title + " " + description).lower()
    return any(kw.lower() in text for kw in POSITIVE_KEYWORDS)


async def _parse_olx_page(client: httpx.AsyncClient, keyword: str, page: int = 1) -> list[dict]:
    """Парсит одну страницу результатов OLX по ключевому слову"""
    results = []

    try:
        params = {
            "search[q]": keyword,
            "page": page,
        }
        url = f"{SEARCH_URL}?search[q]={quote(keyword)}&page={page}"

        resp = await client.get(url, headers=HEADERS, timeout=20, follow_redirects=True)

        if resp.status_code != 200:
            logger.debug(f"OLX: статус {resp.status_code} для '{keyword}'")
            return results

        soup = BeautifulSoup(resp.text, "lxml")

        # Ищем карточки объявлений
        listings = soup.select("[data-cy='l-card']") or soup.select(".offer-wrapper") or []

        if not listings:
            # Попробуем другой селектор
            listings = soup.select("li[data-aut-id='itemBox']") or []

        for listing in listings[:20]:
            try:
                # Название объявления
                title_el = (
                    listing.select_one("h6") or
                    listing.select_one("[data-cy='ad-card-title']") or
                    listing.select_one(".title-cell strong")
                )
                title = title_el.get_text(strip=True) if title_el else ""

                # Ссылка
                link_el = listing.select_one("a[href]")
                link = link_el["href"] if link_el else ""
                if link and not link.startswith("http"):
                    link = BASE_URL + link

                # Цена
                price_el = listing.select_one("[data-testid='ad-price']") or listing.select_one(".price")
                price = price_el.get_text(strip=True) if price_el else ""

                # Местоположение
                location_el = listing.select_one("[data-testid='location-date']") or listing.select_one(".location-price")
                location = location_el.get_text(strip=True) if location_el else ""

                if not title or not _is_relevant(title, location):
                    continue

                result = {
                    "name": title,
                    "source": "olx",
                    "url": link,
                    "address": location,
                    "price": price,
                    "keyword": keyword,
                    "phone": None,  # телефон виден только в объявлении
                    "opening_signal": True,  # аренда под кафе = сигнал об открытии
                    "raw_text": f"{title} | {location} | {price}",
                }
                results.append(result)

            except Exception as e:
                logger.debug(f"OLX: ошибка парсинга карточки: {e}")

    except httpx.TimeoutException:
        logger.warning(f"OLX: таймаут для '{keyword}'")
    except Exception as e:
        logger.error(f"OLX: ошибка для '{keyword}': {e}")

    return results


async def _get_listing_phone(client: httpx.AsyncClient, url: str) -> str | None:
    """
    Пробует получить телефон из страницы объявления.
    OLX прячет телефоны — получаем что можем из описания.
    """
    if not url:
        return None
    try:
        resp = await client.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "lxml")
            # Пробуем найти телефон в описании
            desc = soup.get_text()
            return _extract_phone(desc)
    except Exception:
        pass
    return None


async def fetch_new_items() -> list[dict]:
    """Собирает новые объявления по всем ключевым словам"""
    all_results = []
    seen_urls = set()

    async with httpx.AsyncClient() as client:
        for keyword in OLX_KEYWORDS:
            logger.info(f"OLX: ищу по '{keyword}'")
            items = await _parse_olx_page(client, keyword)

            for item in items:
                url = item.get("url", "")
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)

                # Пробуем достать телефон
                if url and not item.get("phone"):
                    item["phone"] = await _get_listing_phone(client, url)
                    await asyncio.sleep(2)

                all_results.append(item)

            logger.info(f"OLX '{keyword}': нашёл {len(items)} объявлений")
            await asyncio.sleep(10)  # пауза между ключевыми словами

    return all_results


async def run_forever(process_func):
    """
    Бесконечный цикл мониторинга OLX.
    process_func — async функция обработки одного лида.
    """
    logger.info("Парсер OLX запущен")

    while True:
        try:
            items = await fetch_new_items()
            logger.info(f"OLX: всего найдено {len(items)} объявлений")

            for item in items:
                try:
                    await process_func(item)
                except Exception as e:
                    logger.error(f"Ошибка обработки OLX-объявления: {e}")

        except Exception as e:
            logger.error(f"Ошибка парсера OLX: {e}")
            await asyncio.sleep(30)

        logger.info(f"OLX: следующий обход через {PAUSE_OLX // 60} минут")
        await asyncio.sleep(PAUSE_OLX)
