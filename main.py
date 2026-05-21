"""
Zetta Lead Bot — автономный AI-агент для поиска лидов 24/7.
Компания Zetta Group (Узбекистан), продукт: iiko (автоматизация ресторанов).

Запуск: python main.py
"""
import asyncio
import logging
import sys
from datetime import datetime, time

import database
from brain import decision as brain_decision
from brain import learning as brain_learning
from brain import memory as brain_memory
from notifier import telegram_bot as tg
from parsers import instagram, olx, tg_channels
from config import SCORE_HOT, SCORE_WARM, DIGEST_HOUR

# ──────────────────────────────────────────────
# Настройка логирования
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("zetta_bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("zetta_bot")

# Отключаем слишком шумные логи httpx
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ──────────────────────────────────────────────
# Главная функция обработки одного лида
# ──────────────────────────────────────────────
async def process_lead(raw_data: dict):
    """
    Полный цикл обработки одного найденного заведения:
    1. Анализ через Claude (brain/decision.py)
    2. Проверка дублей и сохранение (brain/memory.py)
    3. Отправка уведомления в Telegram (notifier/telegram_bot.py)
    4. Обновление статистики источника
    """
    source = raw_data.get("source", "unknown")
    name = raw_data.get("name", "Без названия")

    logger.info(f"[{source.upper()}] Обрабатываю: {name}")

    # Шаг 1 — анализ через Claude
    decision = await brain_decision.analyze_venue(raw_data)
    if not decision:
        logger.warning(f"Claude не смог проанализировать: {name}")
        database.update_source_stats(source, found=1, error=True)
        return

    decision_type = decision.get("decision", "skip")
    score = decision.get("lead_quality", {}).get("score", 0)

    logger.info(f"[{source.upper()}] Решение: {decision_type} | Скор: {score} | {name}")

    # Шаг 2 — проверка дублей и сохранение
    lead_id, is_dup = await brain_memory.check_and_save_lead(raw_data, decision)

    if is_dup:
        database.update_source_stats(source, found=1)
        return

    # Шаг 3 — отправка уведомления
    msg_id = None

    if decision_type == "send_now":
        # Горячий лид — отправляем сразу
        logger.info(f"🔥 ГОРЯЧИЙ ЛИД: {name} (score={score})")
        msg_id = await tg.notify_hot_lead(raw_data, decision)
        if lead_id and msg_id:
            database.mark_lead_sent(lead_id, msg_id)
        database.update_source_stats(source, found=1, sent=1, hot=1, score=score)

    elif decision_type == "send_digest":
        # Тёплый лид — добавлен в базу, отправится в дайджесте
        logger.info(f"📋 Тёплый лид в дайджест: {name} (score={score})")
        database.update_source_stats(source, found=1, score=score)

    elif decision_type == "watch":
        # На наблюдении — уведомляем и ждём 3 дня
        logger.info(f"👁 На наблюдении: {name}")
        msg_id = await tg.notify_watch_lead(raw_data, decision)
        if lead_id and msg_id:
            database.mark_lead_sent(lead_id, msg_id)
        database.update_source_stats(source, found=1)

    elif decision_type == "skip":
        # Пропускаем — не отправляем
        logger.info(f"⏭ Пропуск: {name}")
        database.update_source_stats(source, found=1)


# ──────────────────────────────────────────────
# Утренний дайджест
# ──────────────────────────────────────────────
async def run_daily_digest():
    """
    Каждый день в DIGEST_HOUR:00 отправляет дайджест тёплых лидов.
    """
    while True:
        now = datetime.now()
        # Вычисляем сколько секунд до следующей отправки
        target = now.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
        if now >= target:
            # Уже прошло сегодня — ждём до завтра
            from datetime import timedelta
            target = target + timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        logger.info(f"Дайджест: следующая отправка через {wait_seconds / 3600:.1f} часов")
        await asyncio.sleep(wait_seconds)

        logger.info("Отправляю утренний дайджест...")
        leads = database.get_digest_leads()
        if leads:
            await tg.send_daily_digest(leads)
            # Помечаем все как отправленные
            for lead in leads:
                database.mark_lead_sent(lead.id)
            logger.info(f"Дайджест отправлен: {len(leads)} тёплых лидов")
        else:
            logger.info("Нет тёплых лидов для дайджеста")


# ──────────────────────────────────────────────
# Перепроверка watch-лидов
# ──────────────────────────────────────────────
async def run_watch_checker():
    """
    Каждые 6 часов перепроверяет лиды в статусе watch.
    """
    while True:
        await asyncio.sleep(6 * 3600)
        logger.info("Перепроверяю watch-лиды...")
        count = await brain_memory.recheck_watch_leads(process_lead)
        if count > 0:
            logger.info(f"Перепроверено {count} watch-лидов")


# ──────────────────────────────────────────────
# Точка входа
# ──────────────────────────────────────────────
async def main():
    """
    Запускает все парсеры ПАРАЛЛЕЛЬНО — они работают одновременно.
    Никаких расписаний. Никаких пауз кроме антибан-задержек.
    """
    logger.info("=" * 60)
    logger.info("🤖 Zetta Lead Bot стартует...")
    logger.info("Компания: Zetta Group (Узбекистан) | Продукт: iiko")
    logger.info(f"Активные парсеры: Instagram, OLX, Telegram-каналы")
    logger.info(f"2GIS: отключён (подключим позже)")
    logger.info("=" * 60)

    # Инициализируем базу данных
    database.init_db()
    logger.info("База данных готова")

    # Отправляем стартовое сообщение в Telegram
    await tg.send_startup_message()

    # Запускаем все процессы параллельно
    await asyncio.gather(
        # Парсеры — работают бесконечно
        instagram.run_forever(process_lead),
        olx.run_forever(process_lead),
        tg_channels.run_forever(process_lead),
        # Утренний дайджест
        run_daily_digest(),
        # Перепроверка watch-лидов (каждые 6 часов)
        run_watch_checker(),
        # Самообучение (каждые 24 часа)
        brain_learning.run_forever(notify_func=tg.send_message),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен вручную (KeyboardInterrupt)")
    except Exception as e:
        logger.critical(f"Критическая ошибка: {e}", exc_info=True)
        sys.exit(1)
