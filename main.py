import os
import random
import asyncio
import logging
import sys
import signal
import time
import re as _re
import html as _html
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import anthropic
import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Conflict as TelegramConflict
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    MessageHandler, filters, ContextTypes,
)

# ── Logging: stdout only (Railway ephemeral FS) ──────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ── Graceful shutdown ─────────────────────────────────────────────────────────
def _handle_shutdown(signum, frame):
    logger.info(f"Signal {signum} received — shutting down gracefully.")
    sys.exit(0)

signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT,  _handle_shutdown)

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY      = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN     = os.environ["TELEGRAM_BOT_TOKEN"]
YOUR_PERSONAL_ID       = int(os.environ["YOUR_PERSONAL_ID"])
PEXELS_API_KEY         = os.environ.get("PEXELS_API_KEY", "")
CHANNEL_USERNAME       = os.environ.get("CHANNEL_USERNAME", "@zetta_uzbekistan")
APPROVAL_GROUP_ID      = int(os.environ.get("APPROVAL_GROUP_ID", "-5160536788"))
GROUP_APPROVALS_NEEDED = int(os.environ.get("GROUP_APPROVALS_NEEDED", "1"))

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# post_id -> {text, photo_url, stage, post_type, group_approvals, group_message_id, topic}
pending_posts: dict[str, dict] = {}

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
SCRAPE_TIMEOUT = 5


# ── Markdown helpers ──────────────────────────────────────────────────────────
def _md_to_html(text: str) -> str:
    """Convert **bold** → <b>bold</b> for Telegram HTML parse_mode."""
    safe = _html.escape(text)
    return _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", safe, flags=_re.DOTALL)

def _strip_bold(text: str) -> str:
    """Remove **bold** markers — for plain-text contexts."""
    return _re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=_re.DOTALL)

# ── Photo helpers ─────────────────────────────────────────────────────────────
TOPIC_PHOTO_KEYWORDS: list[tuple[str, str]] = [
    ("food cost",    "restaurant kitchen cost"),
    ("себестоимост", "restaurant kitchen cost"),
    ("техкарт",      "restaurant kitchen recipe"),
    ("ABC",          "restaurant analytics data"),
    ("аналитик",     "restaurant analytics data"),
    ("инвентариза",  "warehouse food inventory"),
    ("склад",        "warehouse food storage"),
    ("KDS",          "restaurant kitchen display screen"),
    ("кухн",         "restaurant kitchen chef"),
    ("официант",     "restaurant waiter service"),
    ("стоп-лист",    "restaurant menu tablet"),
    ("модификатор",  "restaurant pos tablet"),
    ("доставк",      "food delivery courier"),
    ("лояльност",    "restaurant loyalty customer"),
    ("смен",         "restaurant manager shift"),
    ("персонал",     "restaurant team staff"),
    ("меню",         "restaurant menu design"),
    ("стол",         "restaurant table dining"),
    ("банкет",       "restaurant banquet event"),
    ("автоматиза",   "restaurant automation technology"),
]
TYPE_DEFAULT_QUERY = {
    "news":     "restaurant industry news",
    "lifehack": "restaurant pos system tablet",
    "deepdive": "restaurant kitchen management",
}

def _topic_to_query(topic: str, post_type: str) -> str:
    tl = topic.lower()
    for kw, q in TOPIC_PHOTO_KEYWORDS:
        if kw.lower() in tl:
            return q
    return TYPE_DEFAULT_QUERY.get(post_type, "restaurant")

def _fetch_pexels_photo(query: str) -> str | None:
    if not PEXELS_API_KEY:
        return None
    try:
        resp = requests.get(
            "https://api.pexels.com/v1/search",
            params={"query": query, "orientation": "landscape", "per_page": 15},
            headers={"Authorization": PEXELS_API_KEY},
            timeout=5,
        )
        resp.raise_for_status()
        photos = resp.json().get("photos", [])
        if photos:
            return random.choice(photos)["src"]["large"]
    except Exception as e:
        logger.warning(f"Pexels failed for '{query}': {e}")
    return None

def pick_photo(post_type: str = "lifehack", topic: str = "") -> str | None:
    return _fetch_pexels_photo(_topic_to_query(topic, post_type))

# ── Topics ────────────────────────────────────────────────────────────────────
TOPICS_BY_FEATURE: dict[str, list[str]] = {
    "kds": [
        "KDS в iiko: как кухонный экран сокращает время отдачи блюд",
        "Маршрутизация заказов на KDS: какое блюдо на какой экран",
        "Цветовая индикация на KDS: как повара видят приоритет без слов",
        "Время приготовления в KDS: как iiko измеряет и зачем это менеджеру",
    ],
    "stoplist": [
        "Стоп-лист в iiko: три способа поставить позицию и чем они отличаются",
        "Автоматический стоп-лист: как склад сам блокирует продажи при нуле остатка",
        "Стоп-лист и официант: как это предотвращает конфликты с гостями",
        "Частичный стоп-лист в iiko: ограничить продажи, но не убирать из меню",
        "Синхронизация стоп-листа с агрегаторами доставки в iiko",
    ],
    "abc_analysis": [
        "ABC-анализ меню в iiko: какие блюда тянут прибыль вниз незаметно",
        "Матрица меню iiko: звёзды, рабочие лошадки, загадки и балласт",
        "ABC по выручке vs ABC по прибыли - почему результаты разные",
        "Как часто делать ABC-анализ и когда менять меню на основе данных",
        "Инжиниринг меню через iiko: убрать собак и усилить звёзд",
    ],
    "food_cost": [
        "Food cost в iiko: как система считает себестоимость блюда в реальном времени",
        "Плановый vs фактический food cost: что значит расхождение больше 3%",
        "Потери при обработке в iiko: почему 1 кг говядины даёт 650 г готового продукта",
        "Полуфабрикаты в iiko: как составной ингредиент снижает ошибки в техкартах",
        "Себестоимость с учётом модификаторов: iiko считает каждый вариант блюда",
    ],
    "modifiers": [
        "Модификаторы в iiko: разница между обязательными и опциональными группами",
        "Как настроить модификаторы так, чтобы официант не пропустил выбор соуса",
        "Модификаторы и себестоимость: почему каждый топпинг должен быть в техкарте",
        "Платные vs бесплатные модификаторы: как iiko считает финальную цену блюда",
        "Комбо и сеты в iiko: настройка и влияние на средний чек",
    ],
    "loyalty": [
        "Программа лояльности в iiko: бонусы, скидки и кешбэк - что выгоднее ресторану",
        "Гостевая база iiko: какие данные собирать и как использовать для возврата гостей",
        "Персональные акции в iiko: настройка скидки на день рождения без ручного труда",
        "RFM-анализ гостей в iiko: как найти засыпающих клиентов до ухода",
        "Бонусные баллы в iiko: срок сгорания, минимальное списание и психология удержания",
    ],
    "delivery": [
        "Доставка в iiko: как заказ с агрегатора попадает прямо на кухонный экран",
        "iiko и внешние агрегаторы: синхронизация стоп-листа в реальном времени",
        "Зоны доставки в iiko: привязка к адресу и расчёт стоимости",
        "Статусы доставки в iiko: как повар, упаковщик и курьер видят один заказ",
        "Интеграция iiko с Yandex Go и local-агрегаторами в Узбекистане",
    ],
    "inventory": [
        "Инвентаризация в iiko за 20 минут: правильный порядок зон и сотрудников",
        "Акт списания в iiko: когда списывать и что будет без документа",
        "Пересорт на складе iiko: почему возникает и как ручная коррекция ломает аналитику",
        "Минимальный остаток в iiko: автоуведомления до того, как кончился продукт",
        "Инвентаризация без остановки зала в iiko: лайфхак для занятых ресторанов",
    ],
    "staff": [
        "Рейтинг официантов в iiko: средний чек, количество гостей, скорость",
        "Контроль скидок в iiko: отчёт, который ловит злоупотребления персонала",
        "Права доступа в iiko: почему кассир не должен видеть склад",
        "Табель учёта рабочего времени в iiko: автоматический расчёт по открытию смены",
        "Нарушения кассовой дисциплины: какие действия сотрудника iiko логирует всегда",
        "График персонала в iiko: планирование смен и контроль выхода",
    ],
    "table_management": [
        "Схема зала в iiko: как правильно нарисовать и зачем это влияет на оборот стола",
        "Резервирование столов в iiko: привязка к гостевой базе и история визитов",
        "Перенос заказа между столами в iiko: 30 секунд вместо пересоздания",
        "Банкет в iiko: предзаказ, депозит и разбивка счёта на несколько гостей",
        "Время ожидания у стола в iiko: когда менеджер получает алерт",
    ],
    "financial_reports": [
        "P&L отчёт в iiko: как владелец видит прибыль ресторана за день в реальном времени",
        "Отчёт по выручке в iiko: разбивка по официантам, столам, категориям блюд",
        "Сравнение периодов в iiko: как найти причину падения выручки за неделю",
        "Финансовая аналитика iiko: какие 5 цифр должен смотреть владелец каждое утро",
        "Отчёт по скидкам и промо в iiko: считаем реальную стоимость акций",
    ],
    "waste_tracking": [
        "Учёт списаний в iiko: как фиксировать потери и не терять деньги дважды",
        "Списание по причинам в iiko: порча, проба, брак - и что каждая категория говорит о кухне",
        "Норма потерь в iiko: как установить лимит и получать алерт при превышении",
        "Waste tracking в iiko: связь между списаниями и реальным food cost",
        "Акт переработки в iiko: когда продукт меняет форму и как это учесть",
    ],
    "recipe_costing": [
        "Технологическая карта в iiko: пошаговое создание и привязка к складу",
        "Техкарта с несколькими единицами измерения в iiko: граммы, штуки, порции",
        "Версионность техкарт в iiko: как менять рецептуру без потери истории",
        "Себестоимость сезонного блюда в iiko: как менять цену ингредиента и пересчитывать",
        "Техкарта для заготовок в iiko: полуфабрикаты и многоуровневые рецепты",
    ],
    "shift_reports": [
        "Отчёт по закрытию смены в iiko: 7 показателей, которые менеджер обязан проверить",
        "Кассовые расхождения в iiko: как система фиксирует и почему нельзя игнорировать",
        "X-отчёт и Z-отчёт в iiko: в чём разница и когда использовать каждый",
        "Почасовой отчёт продаж в iiko: как найти провальные и пиковые часы",
        "Смена без закрытия в iiko: что происходит с данными и как исправить",
    ],
    "api_integrations": [
        "iiko и Payme/Click: как автоматизировать приём безналичных платежей в Ташкенте",
        "API iiko: какие интеграции уже доступны на рынке Узбекистана",
        "iiko и системы видеоаналитики: как камера считает гостей и передаёт данные в систему",
        "Интеграция iiko с 1С: что синхронизируется и что остаётся ручным",
        "iiko и телеграм-боты: автоматические отчёты владельцу без открытия системы",
    ],
}

_feature_queue: list[str] = []
_last_feature: str | None = None
_feature_topic_indices: dict[str, int] = {f: 0 for f in TOPICS_BY_FEATURE}

def _refill_feature_queue() -> None:
    global _feature_queue
    keys = list(TOPICS_BY_FEATURE.keys())
    random.shuffle(keys)
    if keys and keys[0] == _last_feature:
        keys.append(keys.pop(0))
    _feature_queue = keys

def get_next_topic() -> str:
    global _last_feature, _feature_queue
    if not _feature_queue:
        _refill_feature_queue()
    feat = _feature_queue.pop(0)
    idx = _feature_topic_indices[feat]
    topics = TOPICS_BY_FEATURE[feat]
    topic = topics[idx % len(topics)]
    _feature_topic_indices[feat] = idx + 1
    _last_feature = feat
    logger.info(f"Topic [{feat}]: {topic[:60]}")
    return topic

# ── News scrapers ─────────────────────────────────────────────────────────────
def _scrape_headlines(url: str) -> list[str]:
    resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=SCRAPE_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    seen: set[str] = set()
    results: list[str] = []
    for tag in soup.find_all(["h1","h2","h3"]) + soup.find_all("a", href=True):
        text = tag.get_text(separator=" ", strip=True)
        if 15 < len(text) < 200 and text not in seen:
            seen.add(text)
            results.append(text)
        if len(results) >= 8:
            break
    return results

def _scrape_rss(url: str) -> list[str]:
    import xml.etree.ElementTree as ET
    resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=SCRAPE_TIMEOUT)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    results: list[str] = []
    for item in root.iter("item"):
        title = item.findtext("title","").strip()
        if title and 15 < len(title) < 200:
            results.append(title)
        if len(results) >= 10:
            break
    return results

def fetch_news_context() -> str | None:
    html_sources = [
        ("gazeta.uz", "https://www.gazeta.uz/ru/"),
        ("daryo.uz",  "https://daryo.uz/ru"),
    ]
    rss_sources = [
        ("Google: restoran UZ",
         "https://news.google.com/rss/search?q=%D1%80%D0%B5%D1%81%D1%82%D0%BE%D1%80%D0%B0%D0%BD+%D0%A3%D0%B7%D0%B1%D0%B5%D0%BA%D0%B8%D1%81%D1%82%D0%B0%D0%BD&hl=ru&gl=UZ&ceid=UZ:ru"),
        ("Google: cafe Tashkent",
         "https://news.google.com/rss/search?q=%D0%BA%D0%B0%D1%84%D0%B5+%D0%A2%D0%B0%D1%88%D0%BA%D0%B5%D0%BD%D1%82+%D0%BE%D0%B1%D1%89%D0%B5%D0%BF%D0%B8%D1%82&hl=ru&gl=UZ&ceid=UZ:ru"),
        ("Google: HoReCa",
         "https://news.google.com/rss/search?q=HoReCa+%D0%B0%D0%B2%D1%82%D0%BE%D0%BC%D0%B0%D1%82%D0%B8%D0%B7%D0%B0%D1%86%D0%B8%D1%8F+%D1%80%D0%B5%D1%81%D1%82%D0%BE%D1%80%D0%B0%D0%BD&hl=ru&gl=UZ&ceid=UZ:ru"),
        ("Google: biznes CA",
         "https://news.google.com/rss/search?q=%D1%80%D0%B5%D1%81%D1%82%D0%BE%D1%80%D0%B0%D0%BD%D0%BD%D1%8B%D0%B9+%D0%B1%D0%B8%D0%B7%D0%BD%D0%B5%D1%81+%D0%A6%D0%B5%D0%BD%D1%82%D1%80%D0%B0%D0%BB%D1%8C%D0%BD%D0%B0%D1%8F+%D0%90%D0%B7%D0%B8%D1%8F&hl=ru&gl=UZ&ceid=UZ:ru"),
    ]
    blocks: list[str] = []
    for name, url in html_sources:
        try:
            h = _scrape_headlines(url)
            if h:
                blocks.append(f"[{name}]\n" + "\n".join(f"- {x}" for x in h))
                logger.info(f"Scraped {len(h)} headlines from {name}")
        except Exception as e:
            logger.warning(f"Scraping failed for {name}: {e}")
    for name, url in rss_sources:
        try:
            h = _scrape_rss(url)
            if h:
                blocks.append(f"[{name}]\n" + "\n".join(f"- {x}" for x in h))
                logger.info(f"Fetched {len(h)} RSS items from {name}")
        except Exception as e:
            logger.warning(f"RSS fetch failed for {name}: {e}")
    return "\n\n".join(blocks) if blocks else None

# ── Post generation ───────────────────────────────────────────────────────────
TYPE_LABELS = {
    "news":     "Svejaya novost",
    "lifehack": "Laifhak iiko",
    "deepdive": "Poleznyj razbor",
}

FORMAT_WHEEL: list[str] = [
    (
        "FORMAT - LOVUSHKA:\n"
        "Pervye 2 stroki - lozhnoe ubezhdenie, kotoroe kazhetsya pravilnym bolshinstvu restoratorov Tashkenta. "
        "Sformuliruyte ego uverenno, kak budto eto obshcheprinytaya istina.\n"
        "Potom rezkiy razvorot: Na samom dele... - i razbe eto ubezhdenie konkretnymi tsiframi.\n"
        "Ob'yasni mekhaniku: pochemu oshibka takaya rasprostranennaya i skolko ona stoit v sumakh.\n"
        "Final: kak iiko pokazyvaet pravdu - konkretnaya funktsiya + rezultat anonimnogo zavedeniya."
    ),
    (
        "FORMAT - ZHIVAYA STSENA:\n"
        "Pervye 3 stroki - kinematografichnaya stsena iz tashkentskogo restorana. "
        "Konkretnoe vremya (pyatnitsa 20:00), mesto (kukhnya, kassa, zal), chelovek (shef, kassir, upravlyayushchiy). "
        "Bez imen zavedeniy. Chitatel dolzhen uvidet kartinku.\n"
        "Sleduyushchiy abzats: pochemu eta stsena - simptom sistemnoy problemy, tsifry poter.\n"
        "Zatem: chto v iiko zakryvaet eto - konkretnaya funktsiya, kak rabotaet.\n"
        "Final: izmerimy rezultat, anonimnoe zavedenie."
    ),
    (
        "FORMAT - DO/POSLE:\n"
        "Opishi odnu konkretnuyu operatsiyu DO iiko: ruchnoy protsess, poteri v sumakh ili chasakh, khaos. "
        "Bez vody, tolko bol i tsifry.\n"
        "Potom to zhe samoe POSLE iiko: konkretnaya funktsiya, skolko vremeni ili deneg ekonomit kazhdyy mesyats.\n"
        "Final: konkretnaya raznitsa v sumakh ili protsentakh, anonimnoe zavedenie Tashkenta."
    ),
    (
        "FORMAT - PROVOKATSIYA:\n"
        "Nachni s rezkogo voprosa ili spornogo tezisa kotoryy zadvayet restoratora. "
        "Primer urovnya: Vy teryaete X sum kazhdyy den - i dazhe ne znaete ob etom.\n"
        "Sleduyushchiy abzats: dokazhite tezis - konkretnaya mekhanika poter, tsifry.\n"
        "Zatem: reshenie cherez iiko - tochnoe nazvanie funktsii, kak ispolzovat.\n"
        "Final: rezultat, anonimnyy keys."
    ),
    (
        "FORMAT - MINI-ISTORIYA:\n"
        "Korotkaya istoriya s nachalom, problemoy i razvyazkoy. "
        "Geroy - anonimnyy restorator iz Tashkenta (bez imen i nazvaniy). "
        "Nachalo: vsyo shlo kak obychno. Problema: chto-to poshlo ne tak, tsifry poter. "
        "Razvyazka: nashli v iiko, nastroili konkretnuyu funktsiyu, poluchili rezultat.\n"
        "Emotsionalnaya duga: ot trevogi k oblegcheniyu. Realistichnye tsifry."
    ),
    (
        "FORMAT - TSIFRA V LOB:\n"
        "Pervaya stroka - odno shocking chislo krupno i bez predisloviy. "
        "Primer: 3 400 000 sum. Stolko teryaet sredniy tashkentskiy restoran za god na nemarkirovannykh spisaniyakh.\n"
        "Sleduyushchiy abzats: otkuda eta tsifra beretsya, operatsionnaya mekhanika, kto v zone riska.\n"
        "Zatem: kak iiko eto vidit i kontroliruet - konkretnaya funktsiya.\n"
        "Final: chto izmenilos posle nastroyki, anonimnyy primer."
    ),
    (
        "FORMAT - DIALOG:\n"
        "Nachni s korotkogo dialoga (3-4 repliki) mezhdu upravlyayushchim i oficiantom, povarom ili kassirom. "
        "Dialog pokazyvaet problemu - tolko zhivaya rech. Bez imen, bez nazvaniy zavedeniy.\n"
        "Posle dialoga: ob'yasni pochemu eto sistemnaya problema, tsifry poter.\n"
        "Zatem: kak iiko ubiraet etu problemu - konkretnaya funktsiya, konkretnye shagi.\n"
        "Final: rezultat v chislakh."
    ),
    (
        "FORMAT - ANTISOVET:\n"
        "Nachni s Ne delajte X - tipichnaya oshibka pri nastroyki ili ispolzovanii iiko "
        "kotoruyu dopuskayut bolshinstvo restoranov Tashkenta.\n"
        "Ob'yasni chto lomaetsya ot etoy oshibki: konkretnaya tsepochka posledstviy, poteri v sumakh.\n"
        "Zatem: kak delat pravilno - konkretnye shagi, tochnoe nazvanie funktsii iiko.\n"
        "Final: rezultat posle ispravleniya, anonimnyy keys."
    ),
]

PERSONAS = {
    "news": (
        "Ty - starshy analitik HoReCa-rynka Uzbekistana, byvshiy operatsionnyy direktor seti iz 12 "
        "restoranov v Tashkente. Pishesh kak zhurnalist-rassledovatel: kazhdyy tezis - tsifra, "
        "kazhdyy vyvod - mekhanika, ni odnogo slova-parazita."
    ),
    "lifehack": (
        "Ty - sertifikovannyy spetsialist po vnedreniyu iiko. Za 8 let nastroil sistemu v 160+ "
        "restoranakh Tashkenta i Uzbekistana. Znaesh kazhduyu knopku, kazhdyy otchet, kazhduyu lovushku. "
        "Govorish kak praktik: tochno, konkretno, bez akademicheskoy vody."
    ),
    "deepdive": (
        "Ty - upravlyayushchiy partner restorannoy gruppy v Tashkente (4 zavedeniya, vyruchka 800 mln sum "
        "v god), sertifikovannyy ekspert iiko. Pishesh analitiku dlya vladeltsev HoReCa Uzbekistana."
    ),
}

PERSONAS_RU = {
    "news": (
        "Ты — старший аналитик HoReCa-рынка Узбекистана, бывший операционный директор сети из 12 "
        "ресторанов в Ташкенте. Пишешь как журналист-расследователь: каждый тезис — цифра, "
        "каждый вывод — механика, ни одного слова-паразита."
    ),
    "lifehack": (
        "Ты — сертифицированный специалист по внедрению iiko. За 8 лет настроил систему в 160+ "
        "ресторанах Ташкента и Узбекистана. Знаешь каждую кнопку, каждый отчёт, каждую ловушку. "
        "Говоришь как практик: точно, конкретно, без академической воды."
    ),
    "deepdive": (
        "Ты — управляющий партнёр ресторанной группы в Ташкенте (4 заведения, выручка 800 млн сум "
        "в год), сертифицированный эксперт iiko. Пишешь аналитику для владельцев HoReCa Узбекистана."
    ),
}

FORMAT_WHEEL_RU: list[str] = [
    (
        "ФОРМАТ — ЛОВУШКА:\n"
        "Первые 2 строки — ложное убеждение, которое кажется правильным большинству рестораторов Ташкента. "
        "Сформулируй его уверенно, как общепринятую истину.\n"
        "Потом резкий разворот: «На самом деле...» — разбей убеждение конкретными цифрами.\n"
        "Объясни механику: почему ошибка такая распространённая и сколько она стоит в сумах.\n"
        "Финал: как iiko показывает правду — конкретная функция + результат анонимного заведения."
    ),
    (
        "ФОРМАТ — ЖИВАЯ СЦЕНА:\n"
        "Первые 3 строки — кинематографичная сцена из ташкентского ресторана. "
        "Конкретное время (пятница 20:00), место (кухня, касса, зал), человек (шеф, кассир, управляющий). "
        "Без имён заведений. Читатель должен увидеть картинку.\n"
        "Следующий абзац: почему эта сцена — симптом системной проблемы, цифры потерь.\n"
        "Затем: что в iiko закрывает это — конкретная функция, как работает.\n"
        "Финал: измеримый результат, анонимное заведение."
    ),
    (
        "ФОРМАТ — ДО/ПОСЛЕ:\n"
        "Опиши одну конкретную операцию ДО iiko: ручной процесс, потери в сумах или часах, хаос. "
        "Без воды, только боль и цифры.\n"
        "Потом то же самое ПОСЛЕ iiko: конкретная функция, сколько времени или денег экономит каждый месяц.\n"
        "Финал: конкретная разница в сумах или процентах, анонимное заведение Ташкента."
    ),
    (
        "ФОРМАТ — ПРОВОКАЦИЯ:\n"
        "Начни с резкого вопроса или спорного тезиса который задевает ресторатора. "
        "Пример: «Вы теряете X сум каждый день — и даже не знаете об этом».\n"
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
        "Диалог показывает проблему — только живая речь. Без имён, без названий заведений.\n"
        "После диалога: объясни почему это системная проблема, цифры потерь.\n"
        "Затем: как iiko убирает эту проблему — конкретная функция, конкретные шаги.\n"
        "Финал: результат в числах."
    ),
    (
        "ФОРМАТ — АНТИСОВЕТ:\n"
        "Начни с «Не делайте X» — типичная ошибка при настройке или использовании iiko "
        "которую допускают большинство ресторанов Ташкента.\n"
        "Объясни что ломается от этой ошибки: конкретная цепочка последствий, потери в сумах.\n"
        "Затем: как делать правильно — конкретные шаги, точное название функции iiko.\n"
        "Финал: результат после исправления, анонимный кейс."
    ),
]

def generate_post(post_type: str, topic: str | None = None) -> str:
    if topic is None:
        topic = get_next_topic()
    persona = PERSONAS_RU[post_type]
    fmt = random.choice(FORMAT_WHEEL_RU)
    news_context = fetch_news_context()
    if news_context and post_type == "news":
        news_block = (
            f"\n\nСВЕЖИЕ ЗАГОЛОВКИ из отраслевых изданий (используй как источник вдохновения):\n\n"
            f"{news_context}\n\n"
        )
    elif news_context:
        news_block = f"\n\nКОНТЕКСТ из отраслевых новостей:\n\n{news_context}\n\n"
    else:
        news_block = ""
    content = (
        f"{persona}\n\n"
        f"ТЕМА ПОСТА: {topic}\n\n"
        f"{fmt}\n"
        f"{news_block}"
        f"ЖЁСТКИЕ ПРАВИЛА:\n"
        f"— Контекст ТОЛЬКО Узбекистан/Ташкент: цифры, районы, реалии местного рынка\n"
        f"— Фокус поста — ТОЛЬКО та функция iiko, которая указана в теме\n"
        f"— Цифры обязательны: суммы в сумах, проценты, временные показатели\n"
        f"— Анонимные примеры только: один ташкентский ресторан, сеть кафе в Узбекистане и т.п.\n"
        f"— НЕ реклама: никогда купи iiko, Zetta Group, обратитесь к нам\n"
        f"— 180-220 слов, только русский язык\n"
        f"— КРИТИЧНО: текст должен быть завершён полностью — никогда не обрывай на полуслове\n"
        f"— Эмодзи: 2-4 штуки, только по смыслу — не декоративные\n"
        f"— ЗАПРЕЩЕНО начинать пост словами: Лайфхак, Совет, Внимание, Представьте\n"
        f"— Каждый пост должен начинаться УНИКАЛЬНО — первые 5 слов не должны быть шаблонными\n"
        f"— Последняя строка без изменений: Связаться: @iikoman\n"
        f"— Только текст поста, без заголовков типа Пост: и без пояснений"
    )
    logger.info(f"Generating post — type={post_type}, format={fmt[:25].strip()!r}")
    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1400,
        messages=[{"role": "user", "content": content}],
    )
    post = msg.content[0].text.strip()
    if "Связаться: @iikoman" not in post:
        post = f"{post}\n\nСвязаться: @iikoman"
    return post

# ── Keyboards ─────────────────────────────────────────────────────────────────
def build_type_keyboard(post_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Свежая новость",  callback_data=f"type_news:{post_id}")],
        [InlineKeyboardButton("💡 Лайфхак iiko",    callback_data=f"type_lifehack:{post_id}")],
        [InlineKeyboardButton("📊 Полезный разбор", callback_data=f"type_deepdive:{post_id}")],
        [InlineKeyboardButton("🎲 Случайный тип",   callback_data=f"type_random:{post_id}")],
    ])

def build_owner_keyboard(post_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Одобрить → группа", callback_data=f"owner_approve:{post_id}"),
            InlineKeyboardButton("❌ Отклонить",          callback_data=f"reject:{post_id}"),
        ],
        [
            InlineKeyboardButton("🔄 Переделать",  callback_data=f"regen:{post_id}"),
            InlineKeyboardButton("📸 Другое фото", callback_data=f"change_photo:{post_id}"),
        ],
        [InlineKeyboardButton("📝 Другой текст",   callback_data=f"change_text:{post_id}")],
    ])

def build_group_keyboard(post_id: str, count: int) -> InlineKeyboardMarkup:
    label = f"✅ Опубликовать ({count}/{GROUP_APPROVALS_NEEDED})"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(label,          callback_data=f"group_approve:{post_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"group_reject:{post_id}"),
        ],
    ])

# ── Core helpers ──────────────────────────────────────────────────────────────
def _new_pending(stage: str = "type_select") -> dict:
    return {
        "text": None, "photo_url": None, "stage": stage,
        "post_type": None, "group_approvals": set(), "group_message_id": None,
    }

async def send_type_selection(app: Application) -> None:
    import uuid
    post_id = str(uuid.uuid4())[:8]
    pending_posts[post_id] = _new_pending("type_select")
    await app.bot.send_message(
        chat_id=YOUR_PERSONAL_ID,
        text="📝 Выберите тип поста:",
        reply_markup=build_type_keyboard(post_id),
    )
    logger.info(f"Type-selection menu sent for post {post_id}.")

async def _generate_and_send_to_owner(bot, post_id: str, post_type: str) -> None:
    topic = get_next_topic()
    post = await asyncio.get_event_loop().run_in_executor(
        None, lambda: generate_post(post_type, topic)
    )
    photo_url = pick_photo(post_type, topic)
    entry = pending_posts.get(post_id)
    if entry is None:
        return
    entry.update({"text": post, "photo_url": photo_url, "stage": "owner",
                  "post_type": post_type, "topic": topic})
    keyboard = build_owner_keyboard(post_id)
    label = {"news": "🔥 Свежая новость", "lifehack": "💡 Лайфхак iiko", "deepdive": "📊 Полезный разбор"}.get(post_type, post_type)
    if photo_url:
        try:
            await bot.send_photo(chat_id=YOUR_PERSONAL_ID, photo=photo_url)
        except Exception as e:
            logger.warning(f"Owner photo send failed ({e}), skipping photo.")
    await bot.send_message(
        chat_id=YOUR_PERSONAL_ID,
        text=_md_to_html(f"[{label}]\n\n{post}"), parse_mode="HTML",
        reply_markup=keyboard,
    )
    logger.info(f"Post {post_id} ({post_type}) sent to owner.")

async def _forward_to_group(bot, post_id: str) -> None:
    entry = pending_posts[post_id]
    post = entry["text"]
    photo_url = entry["photo_url"]
    label = {"news": "🔥 Свежая новость", "lifehack": "💡 Лайфхак iiko", "deepdive": "📊 Полезный разбор"}.get(entry.get("post_type","lifehack"), "Пост")
    count = len(entry["group_approvals"])
    keyboard = build_group_keyboard(post_id, count)
    if photo_url:
        try:
            await bot.send_photo(chat_id=APPROVAL_GROUP_ID, photo=photo_url)
        except Exception as e:
            logger.warning(f"Group photo send failed ({e}), continuing.")
    msg = await bot.send_message(
        chat_id=APPROVAL_GROUP_ID,
        text=_strip_bold(f"📋 [{label}] На проверку:\n\n{post}"),
        reply_markup=keyboard,
    )
    entry["stage"] = "group"
    entry["group_message_id"] = msg.message_id
    logger.info(f"Post {post_id} forwarded to group for vote.")

async def _safe_edit(query, text: str) -> None:
    try:
        await query.edit_message_caption(text)
    except Exception:
        try:
            await query.edit_message_text(text)
        except Exception:
            pass

# ── Handlers ──────────────────────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    # Ignore callbacks from other bots / other projects in the same group
    data = query.data or ""
    KNOWN = ("type_news:","type_lifehack:","type_deepdive:","type_random:",
             "owner_approve:","reject:","regen:","change_photo:","change_text:",
             "send_now:","group_approve:","group_reject:")
    if not any(data.startswith(p) for p in KNOWN):
        return
    await query.answer()
    action, post_id = data.split(":", 1)
    entry = pending_posts.get(post_id)
    if entry is None:
        await _safe_edit(query, "⚠️ Пост уже обработан или не найден.")
        return
    stage = entry["stage"]

    if action.startswith("type_") and stage == "type_select":
        chosen = action[5:]
        if chosen == "random":
            chosen = random.choice(["news", "lifehack", "deepdive"])
        try:
            labels = {"news": "🔥 Свежая новость", "lifehack": "💡 Лайфхак iiko", "deepdive": "📊 Полезный разбор"}
            await query.edit_message_text(f"⏳ Генерирую: {labels.get(chosen, chosen)}...")
        except Exception:
            pass
        await _generate_and_send_to_owner(context.bot, post_id, chosen)

    elif action == "owner_approve" and stage == "owner":
        await _safe_edit(query, f"✅ Одобрено. Отправлено в группу.\n\n{entry['text']}")
        await _forward_to_group(context.bot, post_id)

    elif action == "group_approve" and stage == "group":
        user_id = update.effective_user.id
        user_name = update.effective_user.first_name or "Участник"
        approvals: set = entry["group_approvals"]
        if user_id in approvals:
            await query.answer("Вы уже проголосовали ✅", show_alert=True)
            return
        approvals.add(user_id)
        count = len(approvals)
        logger.info(f"Post {post_id}: {count}/{GROUP_APPROVALS_NEEDED} approvals ({user_name})")
        if count >= GROUP_APPROVALS_NEEDED:
            post = entry["text"]
            photo_url = entry["photo_url"]
            if photo_url:
                try:
                    await context.bot.send_photo(chat_id=CHANNEL_USERNAME, photo=photo_url)
                except Exception as e:
                    logger.warning(f"Channel photo failed ({e}), skipping.")
            await context.bot.send_message(
                chat_id=CHANNEL_USERNAME, text=_md_to_html(post), parse_mode="HTML"
            )
            del pending_posts[post_id]
            await _safe_edit(query, _strip_bold(f"✅ Опубликовано ({count}/{GROUP_APPROVALS_NEEDED})!\n\n{post}"))
            await context.bot.send_message(
                chat_id=YOUR_PERSONAL_ID,
                text=f"✅ Пост опубликован в {CHANNEL_USERNAME}.",
            )
            logger.info(f"Post {post_id} published to channel.")
        else:
            try:
                await query.edit_message_reply_markup(
                    reply_markup=build_group_keyboard(post_id, count)
                )
            except Exception:
                pass
            await query.answer(f"Голос принят ({count}/{GROUP_APPROVALS_NEEDED})", show_alert=True)

    elif action == "group_reject" and stage == "group":
        rejecter = update.effective_user.first_name or "Участник группы"
        post = entry["text"]
        del pending_posts[post_id]
        await _safe_edit(query, f"❌ Пост отклонён ({rejecter}).")
        await context.bot.send_message(
            chat_id=YOUR_PERSONAL_ID,
            text=f"❌ Пост отклонён группой ({rejecter}):\n\n{post}",
        )
        logger.info(f"Post {post_id} rejected by group ({rejecter}).")

    elif action == "reject" and stage == "owner":
        del pending_posts[post_id]
        await _safe_edit(query, "❌ Пост отклонён.")
        logger.info(f"Post {post_id} rejected by owner.")

    elif action == "change_photo" and stage == "owner":
        post_type = entry.get("post_type", "lifehack")
        new_photo = pick_photo(post_type, entry.get("topic", ""))
        entry["photo_url"] = new_photo
        keyboard = build_owner_keyboard(post_id)
        try:
            await context.bot.send_photo(
                chat_id=YOUR_PERSONAL_ID, photo=new_photo,
                caption=entry["text"], reply_markup=keyboard,
            )
            await _safe_edit(query, "📸 Новое фото — смотрите сообщение выше.")
        except Exception as e:
            logger.warning(f"change_photo failed: {e}")
            await _safe_edit(query, "Ошибка при смене фото.")

    elif action == "change_text" and stage == "owner":
        post_type = entry.get("post_type", "lifehack")
        photo_url = entry["photo_url"]
        await _safe_edit(query, "📝 Генерирую новый текст, фото остаётся...")
        new_post = await asyncio.get_event_loop().run_in_executor(
            None, lambda: generate_post(post_type)
        )
        entry["text"] = new_post
        keyboard = build_owner_keyboard(post_id)
        try:
            await context.bot.send_photo(
                chat_id=YOUR_PERSONAL_ID, photo=photo_url,
                caption=new_post, reply_markup=keyboard,
            )
        except Exception as e:
            logger.warning(f"change_text photo failed: {e}")
            await context.bot.send_message(
                chat_id=YOUR_PERSONAL_ID, text=new_post, reply_markup=keyboard
            )

    elif action == "regen" and stage == "owner":
        import uuid
        post_type = entry.get("post_type", "lifehack")
        del pending_posts[post_id]
        await _safe_edit(query, "🔄 Генерирую новый пост...")
        new_id = str(uuid.uuid4())[:8]
        pending_posts[new_id] = _new_pending("owner")
        pending_posts[new_id]["post_type"] = post_type
        await _generate_and_send_to_owner(context.bot, new_id, post_type)

    elif action == "send_now" and stage == "owner":
        post = entry["text"]
        photo_url = entry["photo_url"]
        if photo_url:
            try:
                await context.bot.send_photo(chat_id=CHANNEL_USERNAME, photo=photo_url)
            except Exception as e:
                logger.warning(f"send_now photo failed ({e}), skipping.")
        await context.bot.send_message(chat_id=CHANNEL_USERNAME, text=post)
        del pending_posts[post_id]
        await _safe_edit(query, f"✅ Опубликовано напрямую!\n\n{post}")
        logger.info(f"Post {post_id} published directly (send_now).")


async def handle_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != YOUR_PERSONAL_ID:
        return
    logger.info("Type selection sent via /test.")
    await send_type_selection(context.application)


async def handle_schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != YOUR_PERSONAL_ID:
        return
    import datetime
    tz = pytz.timezone("Asia/Tashkent")
    now_str = datetime.datetime.now(tz).strftime("%H:%M")
    await update.message.reply_text(
        f"Текущее время (Ташкент): {now_str}\n"
        f"Расписание: 09:00 / 13:00 / 19:00\n"
        f"Канал: {CHANNEL_USERNAME}"
    )


async def handle_edited_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != YOUR_PERSONAL_ID:
        return
    text = update.message.text
    owner_posts = [(pid, e) for pid, e in pending_posts.items() if e["stage"] == "owner"]
    if not owner_posts:
        await update.message.reply_text("Нет постов на стадии проверки.")
        return
    post_id, entry = owner_posts[-1]
    entry["text"] = text
    await update.message.reply_text("✏️ Текст обновлён.", reply_markup=build_owner_keyboard(post_id))
    logger.info(f"Post {post_id} text edited by owner.")


async def handle_telegram_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, TelegramConflict):
        logger.warning("409 Conflict detected — another instance may be running. Retrying...")
        await asyncio.sleep(5)
        return
    logger.error(f"Unhandled bot error: {context.error}", exc_info=context.error)

# ── Scheduler ─────────────────────────────────────────────────────────────────
_scheduler = BackgroundScheduler(timezone=pytz.timezone("Asia/Tashkent"))
_main_loop: asyncio.AbstractEventLoop | None = None

def _schedule_post(app: Application) -> None:
    if _main_loop is None:
        logger.warning("Event loop not ready — skipping scheduled post.")
        return
    asyncio.run_coroutine_threadsafe(send_type_selection(app), _main_loop)

def apply_schedule(app: Application) -> None:
    tz = pytz.timezone("Asia/Tashkent")
    for hour in [9, 13, 19]:
        _scheduler.add_job(
            _schedule_post,
            trigger=CronTrigger(hour=hour, minute=0, timezone=tz),
            args=[app],
            id=f"post_{hour}",
            replace_existing=True,
        )
    logger.info("Scheduler configured: 09:00 / 13:00 / 19:00 Tashkent")

# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    global _main_loop

    _main_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_main_loop)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("test",     handle_test_command))
    app.add_handler(CommandHandler("schedule", handle_schedule_command))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^(type_news:|type_lifehack:|type_deepdive:|type_random:|owner_approve:|reject:|regen:|change_photo:|change_text:|send_now:|group_approve:|group_reject:)"))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.User(YOUR_PERSONAL_ID),
            handle_edited_post,
        )
    )
    app.add_error_handler(handle_telegram_error)

    apply_schedule(app)
    _scheduler.start()

    logger.info("iiko channel bot starting — 09:00 / 13:00 / 19:00 Tashkent")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
