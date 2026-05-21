"""
Парсер OLX.uz — самый рабочий источник для Узбекистана.
Мониторит объявления об аренде помещений под кафе/рестораны
И объявления о продаже/аренде готового ресторанного бизнеса.
"""
import asyncio
import json
import logging
import re
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

import state
from config import PAUSE_OLX

logger = logging.getLogger(__name__)

BASE_URL = "https://www.olx.uz"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,uz;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": BASE_URL,
}

# ── Целевые URL-категории OLX.uz ──────────────────────────────────────────────
# 1. Коммерческая недвижимость (аренда под кафе/ресторан)
# 2. Готовый бизнес (продажа ресторанов/кафе)
CATEGORY_URLS = [
    # Аренда коммерческой недвижимости
    f"{BASE_URL}/uz/nedvizhimost/kommercheskaya-nedvizhimost/",
    # Готовый бизнес
    f"{BASE_URL}/uz/biznes-oborudovanie-i-materialy/gotovyy-biznes/",
]

# Поисковые запросы — максимально точечные для ресторанного бизнеса
SEARCH_QUERIES = [
    "под кафе",
    "под ресторан",
    "ошхона",
    "общепит",
    "фудкорт",
    "под пиццерию",
    "готовый ресторан",
    "готовое кафе",
    "продам кафе",
    "аренда кафе",
    "cafe business",
    "yangi oshxona",
    "kafe ijaraga",
]

# Ключевые слова для фильтрации релевантных объявлений
FOOD_KEYWORDS = [
    "кафе", "ресторан", "общепит", "ошхона", "фудкорт", "пиццерия",
    "суши", "столовая", "чойхона", "food court", "food", "cafe",
    "restaurant", "пекарня", "кондитерская", "фастфуд", "буфет",
    "янги", "kafe", "oshxona", "fastfood", "pizza",
]


def _extract_phone(text: str) -> str | None:
    """Извлекает узбекский/российский номер телефона."""
    patterns = [
        r"\+998[\s\-]?\d{2}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}",
        r"998[\s\-]?\d{2}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}",
        r"8[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}",
        r"\d{2}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            phone = m.group().strip()
            digits = re.sub(r"\D", "", phone)
            if len(digits) >= 9:
                return phone
    return None


def _is_food_relevant(title: str, description: str = "") -> bool:
    """Проверяет относится ли объявление к ресторанному бизнесу."""
    text = (title + " " + description).lower()
    return any(kw.lower() in text for kw in FOOD_KEYWORDS)


def _extract_listing_data(listing, base_url: str) -> dict | None:
    """Извлекает данные из карточки объявления OLX."""
    try:
        # Заголовок — пробуем несколько селекторов (OLX меняет разметку)
        title_el = (
            listing.select_one("h6[data-testid='ad-card-title']") or
            listing.select_one("h6") or
            listing.select_one("[data-cy='ad-card-title']") or
            listing.select_one(".title-cell strong") or
            listing.select_one("h3")
        )
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            return None

        # Ссылка
        link_el = listing.select_one("a[href*='/d/']") or listing.select_one("a[href]")
        link = ""
        if link_el:
            link = link_el.get("href", "")
            if link and not link.startswith("http"):
                link = base_url + link

        # Цена
        price_el = (
            listing.select_one("[data-testid='ad-price']") or
            listing.select_one(".price strong") or
            listing.select_one("[class*='price']")
        )
        price = price_el.get_text(strip=True) if price_el else ""

        # Местоположение и дата
        location_el = (
            listing.select_one("[data-testid='location-date']") or
            listing.select_one(".tdnone + td") or
            listing.select_one("[class*='location']")
        )
        location = location_el.get_text(strip=True) if location_el else ""
        # Чистим дату из локации
        location = re.sub(r"\d{1,2}\s+\w+\s+\d{4}.*", "", location).strip()

        # Описание (краткое из карточки)
        desc_el = listing.select_one("[class*='description']") or listing.select_one("p")
        description = desc_el.get_text(strip=True)[:300] if desc_el else ""

        return {
            "title": title,
            "link": link,
            "price": price,
            "location": location,
            "description": description,
        }
    except Exception as e:
        logger.debug(f"OLX: ошибка парсинга карточки: {e}")
        return None


async def _parse_olx_search(
    client: httpx.AsyncClient, keyword: str, page: int = 1
) -> list[dict]:
    """Парсит страницу поиска OLX по ключевому слову."""
    results = []
    url = f"{BASE_URL}/uz/list/?search[q]={quote(keyword)}&page={page}"

    try:
        resp = await client.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        if resp.status_code != 200:
            logger.debug(f"OLX search: статус {resp.status_code} для '{keyword}'")
            return results

        soup = BeautifulSoup(resp.text, "lxml")

        # Пробуем извлечь JSON из __NEXT_DATA__ (OLX использует Next.js)
        next_data_el = soup.find("script", {"id": "__NEXT_DATA__"})
        if next_data_el:
            try:
                next_data = json.loads(next_data_el.string)
                ads = (
                    next_data.get("props", {})
                    .get("pageProps", {})
                    .get("ads", [])
                )
                if not ads:
                    # Попробуем другой путь
                    ads = (
                        next_data.get("props", {})
                        .get("pageProps", {})
                        .get("data", {})
                        .get("ads", [])
                    )
                for ad in ads[:30]:
                    title = ad.get("title", "")
                    if not title or not _is_food_relevant(title):
                        continue

                    location_data = ad.get("location", {})
                    city = location_data.get("cityName", "")
                    district = location_data.get("districtName", "")
                    location_str = f"{city}, {district}".strip(", ")

                    price_data = ad.get("price", {})
                    price = f"{price_data.get('value', '')} {price_data.get('currency', '')}".strip()

                    ad_url = ad.get("url", "")
                    if ad_url and not ad_url.startswith("http"):
                        ad_url = BASE_URL + ad_url

                    # Телефон из параметров объявления
                    phone = None
                    for param in ad.get("params", []):
                        if param.get("key") == "phone":
                            phone = param.get("value", {}).get("label", "")
                            break

                    results.append({
                        "name": title,
                        "source": "olx",
                        "url": ad_url,
                        "address": location_str,
                        "price": price,
                        "phone": phone,
                        "keyword": keyword,
                        "opening_signal": True,
                        "raw_text": f"{title} | {location_str} | {price}",
                    })
                logger.info(f"OLX JSON '{keyword}': {len(results)} объявлений")
                return results
            except (json.JSONDecodeError, KeyError):
                pass  # Fallback к HTML-парсингу

        # Fallback: HTML-парсинг карточек
        listings = (
            soup.select("[data-cy='l-card']") or
            soup.select("li[data-aut-id='itemBox']") or
            soup.select(".offer-wrapper") or
            soup.select("[class*='listing-grid-item']") or []
        )

        for listing in listings[:30]:
            data = _extract_listing_data(listing, BASE_URL)
            if not data:
                continue
            if not _is_food_relevant(data["title"], data["description"]):
                continue
            results.append({
                "name": data["title"],
                "source": "olx",
                "url": data["link"],
                "address": data["location"],
                "price": data["price"],
                "phone": None,
                "keyword": keyword,
                "opening_signal": True,
                "raw_text": f"{data['title']} | {data['location']} | {data['price']}",
            })

        logger.info(f"OLX HTML '{keyword}': {len(results)} объявлений")

    except httpx.TimeoutException:
        logger.warning(f"OLX: таймаут для '{keyword}'")
    except Exception as e:
        logger.error(f"OLX: ошибка для '{keyword}': {e}")

    return results


async def _parse_olx_category(
    client: httpx.AsyncClient, category_url: str
) -> list[dict]:
    """Парсит категорию OLX напрямую (без поиска)."""
    results = []
    try:
        resp = await client.get(
            category_url, headers=HEADERS, timeout=20, follow_redirects=True
        )
        if resp.status_code != 200:
            return results

        soup = BeautifulSoup(resp.text, "lxml")

        # JSON data
        next_data_el = soup.find("script", {"id": "__NEXT_DATA__"})
        if next_data_el:
            try:
                data = json.loads(next_data_el.string)
                ads = (
                    data.get("props", {})
                    .get("pageProps", {})
                    .get("ads", [])
                )
                for ad in ads[:20]:
                    title = ad.get("title", "")
                    if not title or not _is_food_relevant(title):
                        continue
                    location_data = ad.get("location", {})
                    location_str = f"{location_data.get('cityName', '')}".strip()
                    ad_url = ad.get("url", "")
                    if ad_url and not ad_url.startswith("http"):
                        ad_url = BASE_URL + ad_url
                    results.append({
                        "name": title,
                        "source": "olx",
                        "url": ad_url,
                        "address": location_str,
                        "price": "",
                        "phone": None,
                        "keyword": "category",
                        "opening_signal": True,
                        "raw_text": f"{title} | {location_str}",
                    })
            except Exception:
                pass

    except Exception as e:
        logger.debug(f"OLX category error: {e}")

    return results


async def _get_listing_details(
    client: httpx.AsyncClient, url: str
) -> dict:
    """Получает детали объявления: телефон + полное описание."""
    details = {"phone": None, "description": ""}
    if not url:
        return details
    try:
        resp = await client.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
        if resp.status_code != 200:
            return details

        soup = BeautifulSoup(resp.text, "lxml")

        # Пробуем из JSON
        next_data_el = soup.find("script", {"id": "__NEXT_DATA__"})
        if next_data_el:
            try:
                data = json.loads(next_data_el.string)
                ad = (
                    data.get("props", {})
                    .get("pageProps", {})
                    .get("ad", {})
                )
                # Телефон
                for contact in ad.get("contact", {}).get("phones", []):
                    phone_val = contact.get("number", "")
                    if phone_val:
                        details["phone"] = phone_val
                        break
                # Описание
                details["description"] = ad.get("description", "")[:500]
                return details
            except Exception:
                pass

        # Fallback: поиск телефона в тексте страницы
        full_text = soup.get_text()
        details["phone"] = _extract_phone(full_text)

        desc_el = soup.select_one("[data-cy='ad_description']") or soup.select_one(".description")
        if desc_el:
            details["description"] = desc_el.get_text(strip=True)[:500]

    except Exception as e:
        logger.debug(f"OLX details error для {url}: {e}")

    return details


async def fetch_new_items() -> list[dict]:
    """Собирает новые объявления по всем запросам и категориям."""
    all_results = []
    seen_urls: set[str] = set()

    async with httpx.AsyncClient() as client:
        # 1. Поиск по ключевым словам
        for keyword in SEARCH_QUERIES:
            items = await _parse_olx_search(client, keyword)
            for item in items:
                url = item.get("url", "")
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                all_results.append(item)
            await asyncio.sleep(8)

        # 2. Парсинг категорий (готовый бизнес)
        for cat_url in CATEGORY_URLS:
            items = await _parse_olx_category(client, cat_url)
            for item in items:
                url = item.get("url", "")
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                all_results.append(item)
            await asyncio.sleep(5)

        # 3. Достаём детали (телефон + описание) для топ объявлений
        logger.info(f"OLX: получаю детали для {min(len(all_results), 30)} объявлений")
        for item in all_results[:30]:
            if not item.get("phone") and item.get("url"):
                details = await _get_listing_details(client, item["url"])
                if details["phone"]:
                    item["phone"] = details["phone"]
                if details["description"]:
                    item["raw_text"] = (
                        f"{item['raw_text']} | {details['description'][:200]}"
                    )
                await asyncio.sleep(3)

    logger.info(f"OLX: итого уникальных объявлений: {len(all_results)}")
    return all_results


async def run_forever(process_func):
    """Бесконечный цикл мониторинга OLX."""
    logger.info("Парсер OLX запущен (улучшенная версия: JSON + HTML + детали)")

    while True:
        if state.is_paused():
            await asyncio.sleep(60)
            continue

        try:
            items = await fetch_new_items()
            logger.info(f"OLX: обрабатываю {len(items)} объявлений")

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
