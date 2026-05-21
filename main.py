"""
Zetta Lead Bot — автономный AI-агент для поиска лидов 24/7.
Компания Zetta Group (Узбекистан), продукт: iiko (автоматизация ресторанов).

Запуск: python main.py
"""
import asyncio
import logging
import sys
from datetime import datetime, timedelta

import database
import state
from brain import decision as brain_decision
from brain import learning as brain_learning
from brain import memory as brain_memory
from notifier import telegram_bot as tg
from notifier import command_bot
from parsers import instagram, olx, tg_channels, twogis
from config import SCORE_HOT, SCORE_WARM, DIGEST_HOUR

# ──────────────────────────────────────────────
# Настройка логирования — только stdout (Railway не поддерживает файлы)
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("zetta_bot")

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ──────────────────────────────────────────────
# Обработка одного лида
# ──────────────────────────────────────────────
async def process_lead(raw_data: dict):
    """
    Полный цикл: Claude анализирует → проверяем дубль → отправляем в Telegram.
    """
    source = raw_data.get("source", "unknown")
    name = raw_data.get("name", "Без названия")

    logger.info(f"[{source.upper()}] Обрабатываю: {name}")

    decision = await brain_decision.analyze_venue(raw_data)
    if not decision:
        logger.warning(f"Claude не смог проанализировать: {name}")
        database.update_source_stats(source, found=1, error=True)
        return

    decision_type = decision.get("decision", "skip")
    score = decision.get("lead_quality", {}).get("score", 0)
    logger.info(f"[{source.upper()}] Решение: {decision_type} | Скор: {score} | {name}")

    lead_id, is_dup = await brain_memory.check_and_save_lead(raw_data, decision)
    if is_dup:
        database.update_source_stats(source, found=1)
        return

    msg_id = None

    if decision_type == "send_now":
        logger.info(f"🔥 ГОРЯЧИЙ ЛИД: {name} (score={score})")
        msg_id = await tg.notify_hot_lead(raw_data, decision)
        if lead_id and msg_id:
            database.mark_lead_sent(lead_id, msg_id)
        database.update_source_stats(source, found=1, sent=1, hot=1, score=score)

    elif decision_type == "send_digest":
        logger.info(f"📋 Тёплый лид в дайджест: {name} (score={score})")
        database.update_source_stats(source, found=1, score=score)

    elif decision_type == "watch":
        logger.info(f"👁 На наблюдении: {name}")
        msg_id = await tg.notify_watch_lead(raw_data, decision)
        if lead_id and msg_id:
            database.mark_lead_sent(lead_id, msg_id)
        database.update_source_stats(source, found=1)

    elif decision_type == "skip":
        logger.info(f"⏭ Пропуск: {name}")
        database.update_source_stats(source, found=1)


# ──────────────────────────────────────────────
# Тестовый прогон (вызывается командой /test)
# ──────────────────────────────────────────────
async def run_one_test_cycle():
    """
    Немедленно запускает один прогон всех парсеров.
    Используется командой /test.
    """
    logger.info("Тестовый прогон запущен...")
    results = await asyncio.gather(
        olx.fetch_new_items(),
        tg_channels.fetch_new_items(),
        twogis.fetch_new_items(),
        instagram.fetch_new_items(),
        return_exceptions=True,
    )
    total = 0
    for batch in results:
        if isinstance(batch, list):
            for item in batch[:3]:
                try:
                    await process_lead(item)
                    total += 1
                except Exception as e:
                    logger.error(f"Ошибка в тестовом прогоне: {e}")
    logger.info(f"Тестовый прогон завершён: обработано {total} заведений")


# ──────────────────────────────────────────────
# Утренний дайджест
# ──────────────────────────────────────────────
async def run_daily_digest():
    """Каждый день в DIGEST_HOUR:00 отправляет дайджест тёплых лидов."""
    while True:
        now = datetime.now()
        target = now.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        logger.info(f"Дайджест: следующая отправка через {wait_seconds / 3600:.1f} часов")
        await asyncio.sleep(wait_seconds)

        leads = database.get_digest_leads()
        if leads:
            await tg.send_daily_digest(leads)
            for lead in leads:
                database.mark_lead_sent(lead.id)
            logger.info(f"Дайджест отправлен: {len(leads)} тёплых лидов")
        else:
            logger.info("Нет тёплых лидов для дайджеста")


# ──────────────────────────────────────────────
# Перепроверка watch-лидов
# ──────────────────────────────────────────────
async def run_watch_checker():
    """Каждые 6 часов перепроверяет лиды в статусе watch."""
    while True:
        await asyncio.sleep(6 * 3600)
        if state.is_paused():
            continue
        count = await brain_memory.recheck_watch_leads(process_lead)
        if count > 0:
            logger.info(f"Перепроверено {count} watch-лидов")


# ──────────────────────────────────────────────
# Точка входа
# ──────────────────────────────────────────────
async def main():
    logger.info("=" * 60)
    logger.info("🤖 Zetta Lead Bot стартует...")
    logger.info("Компания: Zetta Group (Узбекистан) | Продукт: iiko")
    logger.info("Парсеры: Instagram, OLX.uz, Telegram-каналы, 2GIS")
    logger.info("Команды: /status /sources /test /pause /resume")
    logger.info("=" * 60)

    database.init_db()
    logger.info("База данных готова")

    await tg.send_startup_message()

    await asyncio.gather(
        instagram.run_forever(process_lead),
        olx.run_forever(process_lead),
        tg_channels.run_forever(process_lead),
        twogis.run_forever(process_lead),
        run_daily_digest(),
        run_watch_checker(),
        brain_learning.run_forever(notify_func=tg.send_message),
        command_bot.run_forever(trigger_test_func=run_one_test_cycle),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен вручную (KeyboardInterrupt)")
    except Exception as e:
        logger.critical(f"Критическая ошибка: {e}", exc_info=True)
        sys.exit(1)
