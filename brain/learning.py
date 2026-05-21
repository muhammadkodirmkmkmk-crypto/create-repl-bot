"""
Самообучение — агент анализирует свою эффективность каждые 24 часа.
"""
import asyncio
import json
import logging

import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, PAUSE_LEARNING
import database

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

LEARNING_PROMPT = """Ты — умный агент по поиску лидов (ресторанный бизнес Узбекистан).
Анализируй свою работу за последние 24 часа.

СТАТИСТИКА:
{stats}

ПОСЛЕДНИЕ РЕШЕНИЯ ({count} штук):
{recent_decisions}

Ответь ТОЛЬКО в формате JSON (без markdown):

{{
  "best_source": "какой источник дал лучшие лиды сегодня и почему",
  "worst_source": "какой источник тратит время зря",
  "pattern_discovered": "какой новый паттерн ты заметил (или null)",
  "recommendation": "что изменить в поиске завтра",
  "new_keywords": ["список новых ключевых слов которые стоит добавить"],
  "keywords_to_remove": ["список ключевых слов которые дают мусор"]
}}"""


async def run_learning_cycle() -> dict | None:
    """
    Запускает анализ эффективности агента через Claude.
    Возвращает результат анализа.
    """
    try:
        stats = database.get_last_24h_stats()

        if stats["total"] == 0:
            logger.info("Самообучение: нет данных за последние 24 часа, пропускаю")
            return None

        stats_text = (
            f"Всего найдено: {stats['total']}\n"
            f"Горячих (send_now): {stats['hot']}\n"
            f"Тёплых (send_digest): {stats['warm']}\n"
            f"Пропущено (skip): {stats['skipped']}\n"
            f"На наблюдении (watch): {stats['watch']}\n"
            f"\nПо источникам:\n"
        )
        for source, s in stats["source_stats"].items():
            hot_pct = (s["hot"] / s["total"] * 100) if s["total"] > 0 else 0
            stats_text += f"  {source}: найдено {s['total']}, горячих {s['hot']} ({hot_pct:.0f}%)\n"

        decisions_text = json.dumps(
            stats["recent_decisions"], ensure_ascii=False, indent=2
        )

        prompt = LEARNING_PROMPT.format(
            stats=stats_text,
            count=len(stats["recent_decisions"]),
            recent_decisions=decisions_text,
        )

        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1])

        result = json.loads(raw)

        # Сохраняем инсайты в знания агента
        if result.get("pattern_discovered"):
            database.save_knowledge(
                insight_type="pattern",
                insight_text=result["pattern_discovered"],
                confidence=0.65,
            )

        if result.get("best_source"):
            database.save_knowledge(
                insight_type="source_quality",
                insight_text=f"Лучший источник: {result['best_source']}",
                confidence=0.7,
            )

        logger.info(f"Самообучение завершено: {result.get('recommendation', '')}")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"Ошибка парсинга JSON в обучении: {e}")
        return None
    except Exception as e:
        logger.error(f"Ошибка в цикле обучения: {e}")
        return None


async def run_forever(notify_func=None):
    """
    Бесконечный цикл самообучения — каждые 24 часа.
    notify_func — функция отправки результата в Telegram (опционально).
    """
    logger.info("Модуль самообучения запущен (интервал: 24ч)")

    while True:
        await asyncio.sleep(PAUSE_LEARNING)

        logger.info("Запускаю анализ эффективности за 24 часа...")
        result = await run_learning_cycle()

        if result and notify_func:
            summary = (
                f"🧠 *Самообучение агента*\n\n"
                f"*Лучший источник:* {result.get('best_source', '—')}\n"
                f"*Слабый источник:* {result.get('worst_source', '—')}\n"
                f"*Новый паттерн:* {result.get('pattern_discovered') or '—'}\n"
                f"*Рекомендация:* {result.get('recommendation', '—')}"
            )
            try:
                await notify_func(summary)
            except Exception as e:
                logger.error(f"Ошибка отправки результата обучения: {e}")
