"""
Парсер 2GIS — поиск новых ресторанов и кафе через публичный API.
Работает без официального API ключа через demo-доступ и веб-скрапинг.
"""
import asyncio
import json
import logging
import re
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

import state
from config import PAUSE_OLX, OPENING_KEYWORDS

logger = logging.getLogger(__name__)

PAUSE_2GIS = 1800  # 30 минут между прогонами

# Catalog API — работает с demo ключом для ограниченного числа запросов
CATALOG_API = "https://catalog.api.2gis.com/3.0/items"

# Suggest API — не требует ключа
SUGGEST_API = "https://suggest.api.2gis.com/v1/suggest"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,uz;q=0.8",
    "Origin": "https://2gis.uz",
    "Referer": "https://2gis.uz/",
}

# Запросы для поиска новых заведений
SEARCH_QUERIES = [
    "новый ресторан Ташкент",
    "новое кафе Ташкент",
    "yangi restoran Toshkent",
    "yangi kafe Toshkent",
    "grand opening кафе",
    "открытие ресторан",
    "oshxona yangi",
]

# Поиск по рубрикам (открытые недавно)
RUBRIC_QUERIES = [
    "ресторан",
    "кафе",
    "фаст-фуд",
    "пиццерия",
    "суши",
    "чойхона",
    "ошхона",
]

# Города для поиска
CITIES = [
    {"name": "tashkent", "lat": 41.2995, "lon": 69.2401},
    {"name": "samarkand", "lat": 39.6542, "lon": 66.9597},
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


def _has_opening_signal(text: str) -> bool:
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in OPENING_KEYWORDS)


def _parse_catalog_item(item: dict, city: str) -> dict | None:
    """Парсит один элемент из ответа Catalog API."""
    name = item.get("name", "").strip()
    if not name:
        return None

    firm_id = item.get("id", "")
    address = (
        item.get("address_name")
        or item.get("full_name", "")
        or item.get("address", {}).get("name", "")
    )

    # Телефоны
    phone = None
    for group in item.get("contact_groups", []):
        for contact in group.get("contacts", []):
            if contact.get("type") == "phone":
                phone = contact.get("value", "").strip()
                break
        if phone:
            break

    # Instagram/сайт
    instagram = None
    website = None
    for group in item.get("contact_groups", []):
        for contact in group.get("contacts", []):
            val = contact.get("value", "")
            if "instagram.com" in val:
                instagram = val
            elif contact.get("type") == "website" and not instagram:
                website = val

    url = f"https://2gis.uz/{city}/firm/{firm_id}" if firm_id else None

    return {
        "name": name,
        "source": "2gis",
        "address": address,
        "phone": phone,
        "instagram": instagram,
        "website": website,
        "url": url,
        "raw_text": f"{name} | {address}",
        "opening_signal": _has_opening_signal(name + " " + address),
    }


async def _search_catalog_api(
    client: httpx.AsyncClient, query: str, city: dict
) -> list[dict]:
    """
    Поиск через Catalog API 2GIS.
    Пробует demo ключ — работает для ~10 запросов/мин.
    """
    results = []
    try:
        params = {
            "q": query,
            "locale": "ru_UZ",
            "type": "branch",
            "sort": "score",
            "page": 1,
            "page_size": 20,
            "fields": "items.contact_groups,items.address,items.point",
            "key": "demo",
            "lat": city["lat"],
            "lon": city["lon"],
            "radius": 30000,
        }
        resp = await client.get(
            CATALOG_API, params=params, headers=HEADERS, timeout=15
        )

        if resp.status_code == 200:
            data = resp.json()
            items = data.get("result", {}).get("items", [])
            for item in items:
                parsed = _parse_catalog_item(item, city["name"])
                if parsed:
                    results.append(parsed)
            logger.info(
                f"2GIS Catalog '{query}' ({city['name']}): {len(results)} результатов"
            )
        elif resp.status_code == 403:
            logger.debug("2GIS Catalog API: demo ключ недоступен (403)")
        else:
            logger.debug(f"2GIS Catalog API: статус {resp.status_code}")

    except httpx.TimeoutException:
        logger.debug(f"2GIS Catalog API: таймаут для '{query}'")
    except Exception as e:
        logger.debug(f"2GIS Catalog API ошибка: {e}")

    return results


async def _search_via_web(
    client: httpx.AsyncClient, query: str, city: dict
) -> list[dict]:
    """
    Резервный метод — парсинг веб-страницы 2GIS.
    Извлекает JSON из тега <script> с initialData.
    """
    results = []
    url = f"https://2gis.uz/{city['name']}/search/{quote(query)}"

    try:
        resp = await client.get(url, headers={
            **HEADERS,
            "Accept": "text/html,application/xhtml+xml",
        }, timeout=20, follow_redirects=True)

        if resp.status_code != 200:
            return results

        soup = BeautifulSoup(resp.text, "lxml")

        # 2GIS встраивает данные в script с window.__initialData__
        for script in soup.find_all("script"):
            script_text = script.string or ""
            if "__initialData__" in script_text or "catalog" in script_text.lower():
                # Ищем JSON-массив с результатами
                json_matches = re.findall(
                    r'"name"\s*:\s*"([^"]{3,80})".*?"address_name"\s*:\s*"([^"]{3,100})"',
                    script_text,
                )
                for name, address in json_matches[:20]:
                    if any(kw in name.lower() for kw in ["ресторан", "кафе", "cafe", "ошхона", "food", "pizza"]):
                        results.append({
                            "name": name,
                            "source": "2gis",
                            "address": address,
                            "phone": None,
                            "url": url,
                            "raw_text": f"{name} | {address}",
                            "opening_signal": _has_opening_signal(name + address),
                        })
                break

    except httpx.TimeoutException:
        logger.debug(f"2GIS web: таймаут для '{query}'")
    except Exception as e:
        logger.debug(f"2GIS web ошибка: {e}")

    return results


async def _search_new_venues(client: httpx.AsyncClient) -> list[dict]:
    """
    Ищет новые заведения — сортировка по дате добавления через rubric search.
    """
    results = []
    for city in CITIES:
        for query in RUBRIC_QUERIES:
            try:
                # Ищем с sort=created — свежие добавления в 2GIS
                params = {
                    "q": query,
                    "locale": "ru_UZ",
                    "type": "branch",
                    "sort": "created",
                    "page": 1,
                    "page_size": 15,
                    "fields": "items.contact_groups,items.address",
                    "key": "demo",
                    "lat": city["lat"],
                    "lon": city["lon"],
                    "radius": 30000,
                }
                resp = await client.get(
                    CATALOG_API, params=params, headers=HEADERS, timeout=12
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for item in data.get("result", {}).get("items", []):
                        parsed = _parse_catalog_item(item, city["name"])
                        if parsed:
                            results.append(parsed)
                await asyncio.sleep(1)
            except Exception:
                pass

    logger.info(f"2GIS новые заведения: {len(results)} результатов")
    return results


async def fetch_new_items() -> list[dict]:
    """Собирает новые заведения из 2GIS через несколько стратегий."""
    all_results = []
    seen_names = set()

    async with httpx.AsyncClient() as client:
        # Стратегия 1: Прямой поиск по ключевым запросам
        for city in CITIES:
            for query in SEARCH_QUERIES[:4]:  # Первые 4 запроса
                items = await _search_catalog_api(client, query, city)

                if not items:
                    # Резервный метод — веб-скрапинг
                    items = await _search_via_web(client, query, city)

                for item in items:
                    key = item.get("name", "").lower().strip()
                    if key and key not in seen_names:
                        seen_names.add(key)
                        all_results.append(item)

                await asyncio.sleep(2)

        # Стратегия 2: Поиск по рубрикам (свежие добавления)
        new_items = await _search_new_venues(client)
        for item in new_items:
            key = item.get("name", "").lower().strip()
            if key and key not in seen_names:
                seen_names.add(key)
                all_results.append(item)

    logger.info(f"2GIS: итого уникальных заведений: {len(all_results)}")
    return all_results


async def run_forever(process_func):
    """Бесконечный цикл мониторинга 2GIS."""
    logger.info("Парсер 2GIS запущен (demo API + web scraping)")

    while True:
        if state.is_paused():
            await asyncio.sleep(60)
            continue

        try:
            items = await fetch_new_items()
            logger.info(f"2GIS: обрабатываю {len(items)} заведений")

            for item in items:
                try:
                    await process_func(item)
                except Exception as e:
                    logger.error(f"Ошибка обработки 2GIS-заведения: {e}")

        except Exception as e:
            logger.error(f"Ошибка парсера 2GIS: {e}")
            await asyncio.sleep(30)

        logger.info(f"2GIS: следующий обход через {PAUSE_2GIS // 60} минут")
        await asyncio.sleep(PAUSE_2GIS)
