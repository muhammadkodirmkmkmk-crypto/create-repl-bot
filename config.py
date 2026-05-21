"""
Конфигурация агента — читает переменные окружения из .env
"""
import os
from dotenv import load_dotenv

load_dotenv()

# Anthropic Claude
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"

# Telegram — все уведомления только на TELEGRAM_CHAT_ID
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# 2GIS — работает без API ключа через demo + веб-скрапинг
TWOGIS_API_KEY = os.getenv("TWOGIS_API_KEY", "")

# Пороги для принятия решений
SCORE_HOT = int(os.getenv("SCORE_HOT", "60"))
SCORE_WARM = int(os.getenv("SCORE_WARM", "30"))
WATCH_DAYS = int(os.getenv("WATCH_DAYS", "3"))

# Паузы между обходами (в секундах)
PAUSE_INSTAGRAM = 600    # 10 минут
PAUSE_OLX = 900          # 15 минут
PAUSE_TG_CHANNELS = 60   # 1 минута
PAUSE_2GIS = 1800        # 30 минут
PAUSE_LEARNING = 86400   # 24 часа

# Утренний дайджест
DIGEST_HOUR = 9  # 09:00

# Telegram каналы для мониторинга
TG_CHANNELS = [
    "tashkent_food",
    "uzrestoran",
    "novosti_tashkenta",
    "tashkentcafe",
    "restoran_tashkent",
    "food_uz",
    "uzfood",
    "tashkentrestoran",
]

# Ключевые слова для поиска открытий
OPENING_KEYWORDS = [
    "открытие", "открываемся", "открылись", "grand opening",
    "скоро открытие", "скоро", "yangi", "ochilish", "ochildi",
    "новое кафе", "новый ресторан", "новое заведение",
    "ищем команду", "набор персонала", "открываем",
    "soft open", "grand open", "ochilmoqda", "yangilanmoqda",
]

# Instagram хэштеги
INSTAGRAM_HASHTAGS = [
    "ресторанташкент",
    "openingsoon",
    "yangirestoran",
    "cafetashkent",
    "новоекафе",
    "открытиересторана",
    "ресторанузбекистан",
    "cafetoshkent",
    "yangioshxona",
    "tashkentfood",
    "uzbekistanfood",
]

# OLX ключевые слова (теперь расширенный список в parsers/olx.py)
OLX_KEYWORDS = [
    "под кафе",
    "под ресторан",
    "общепит",
    "ошхона",
    "фудкорт",
    "готовый ресторан",
    "продам кафе",
    "kafe ijaraga",
]

# Регионы для поиска
REGIONS = ["Ташкент", "Самарканд", "Бухара", "Фергана", "Наманган"]

# Путь к базе данных
DATABASE_URL = "sqlite:///zetta_bot.db"

# Путь к файлу логов
LOG_FILE = "zetta_bot.log"
