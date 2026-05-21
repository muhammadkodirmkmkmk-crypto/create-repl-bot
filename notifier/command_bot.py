"""
Telegram-команды для управления ботом (long polling без PTB).
Работает только для владельца (TELEGRAM_CHAT_ID).

/status  — лиды за сегодня
/sources — статистика источников
/test    — немедленный тестовый прогон
/pause   — поставить на паузу
/resume  — возобновить парсинг
"""
import asyncio
import logging
from datetime import datetime

import httpx

import database
import state
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Функция тестового прогона — устанавливается из main.py
_trigger_test_func = None


async def _send(text: str) -> None:
    """Отправить ответ владельцу."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{TG_API}/sendMessage",
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
    except Exception as e:
        logger.error(f"Command bot send error: {e}")


async def _handle_status():
    stats = database.get_last_24h_stats()
    status_icon = "⏸" if state.is_paused() else "▶️"
    status_text = "На паузе" if state.is_paused() else "Работает"
    text = (
        f"📊 *Статус за последние 24ч*\n\n"
        f"🔍 Всего найдено: *{stats['total']}*\n"
        f"🔥 Горячих лидов: *{stats['hot']}*\n"
        f"📋 Тёплых (в дайджест): *{stats['warm']}*\n"
        f"👁 На наблюдении: *{stats['watch']}*\n"
        f"⏭ Пропущено: *{stats['skipped']}*\n\n"
        f"{status_icon} Парсинг: *{status_text}*\n"
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )
    await _send(text)


async def _handle_sources():
    stats = database.get_last_24h_stats()
    text = "📡 *Источники за последние 24ч*\n\n"
    emoji_map = {
        "instagram": "📸",
        "olx": "🏠",
        "tg_channels": "📢",
        "2gis": "🗺",
    }
    for source, s in stats["source_stats"].items():
        total = s.get("total", 0)
        hot = s.get("hot", 0)
        hot_pct = (hot / total * 100) if total > 0 else 0
        emoji = emoji_map.get(source, "📍")
        bar = "🟢" if total > 0 else "🔴"
        text += (
            f"{bar} {emoji} *{source}*\n"
            f"   Найдено: {total} | Горячих: {hot} ({hot_pct:.0f}%)\n\n"
        )
    if state.is_paused():
        text += "⚠️ _Парсинг на паузе — данные не обновляются_"
    await _send(text)


async def _handle_test():
    global _trigger_test_func
    await _send(
        "🔄 *Запускаю тестовый прогон...*\n\n"
        "Прогоняю все парсеры прямо сейчас.\n"
        "Найденные лиды придут отдельными сообщениями."
    )
    if _trigger_test_func:
        asyncio.create_task(_trigger_test_func())
    else:
        await _send("⚠️ Тестовый прогон недоступен — бот только стартовал.")


async def _handle_pause():
    if state.is_paused():
        await _send("ℹ️ Парсинг уже на паузе.\n\nОтправь /resume чтобы возобновить.")
        return
    state.pause()
    await _send(
        "⏸ *Парсинг поставлен на паузу.*\n\n"
        "Бот не ищет новые лиды.\n"
        "Отправь /resume чтобы возобновить."
    )
    logger.info("Парсинг поставлен на паузу через Telegram-команду")


async def _handle_resume():
    if not state.is_paused():
        await _send("ℹ️ Парсинг уже работает.\n\nОтправь /pause чтобы поставить на паузу.")
        return
    state.resume()
    await _send(
        "▶️ *Парсинг возобновлён!*\n\n"
        "Бот снова ищет новые лиды."
    )
    logger.info("Парсинг возобновлён через Telegram-команду")


async def _handle_update(update: dict):
    """Обрабатывает одно входящее сообщение."""
    msg = update.get("message", {})
    if not msg:
        return

    chat_id = str(msg.get("chat", {}).get("id", ""))
    if chat_id != str(TELEGRAM_CHAT_ID):
        logger.debug(f"Command bot: игнорирую сообщение от {chat_id} (не владелец)")
        return

    text = msg.get("text", "").strip().split()[0].lower()  # первое слово команды

    if text in ("/status", "/status@zetta_lead_bot"):
        await _handle_status()
    elif text in ("/sources", "/sources@zetta_lead_bot"):
        await _handle_sources()
    elif text in ("/test", "/test@zetta_lead_bot"):
        await _handle_test()
    elif text in ("/pause", "/pause@zetta_lead_bot"):
        await _handle_pause()
    elif text in ("/resume", "/resume@zetta_lead_bot"):
        await _handle_resume()
    elif text in ("/start", "/help"):
        await _send(
            "🤖 *Zetta Lead Bot — команды управления*\n\n"
            "/status — лиды за последние 24 часа\n"
            "/sources — статистика по источникам\n"
            "/test — запустить прогон прямо сейчас\n"
            "/pause — поставить парсинг на паузу\n"
            "/resume — возобновить парсинг"
        )


async def run_forever(trigger_test_func=None):
    """
    Бесконечный цикл получения команд через Telegram long polling.
    trigger_test_func — async функция для немедленного тестового прогона.
    """
    global _trigger_test_func
    _trigger_test_func = trigger_test_func

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Command bot: TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы — команды недоступны")
        return

    logger.info("Command bot запущен (long polling)")

    # Сбрасываем накопившиеся апдейты при старте
    try:
        async with httpx.AsyncClient() as client:
            await client.get(
                f"{TG_API}/getUpdates",
                params={"offset": -1, "timeout": 1},
                timeout=5,
            )
    except Exception:
        pass

    offset = 0
    while True:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{TG_API}/getUpdates",
                    params={
                        "offset": offset,
                        "timeout": 30,
                        "allowed_updates": ["message"],
                    },
                    timeout=35,
                )
                data = resp.json()

                if data.get("ok"):
                    for upd in data.get("result", []):
                        offset = upd["update_id"] + 1
                        try:
                            await _handle_update(upd)
                        except Exception as e:
                            logger.error(f"Command bot handle error: {e}")

        except httpx.TimeoutException:
            pass  # норма при long polling — нет новых сообщений
        except Exception as e:
            logger.error(f"Command bot polling error: {e}")
            await asyncio.sleep(5)
