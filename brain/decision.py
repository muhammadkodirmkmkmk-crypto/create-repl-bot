"""
Мозг агента — Claude принимает решение по каждому найденному заведению.
"""
import json
import logging

import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, SCORE_HOT, SCORE_WARM
import database

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

DECISION_PROMPT = """Ты — умный агент по поиску клиентов для компании Zetta Group (Узбекистан).
Zetta Group продаёт систему автоматизации ресторанов iiko.

Твоя задача: изучить данные о заведении и принять решение.

КОНТЕКСТ О РЫНКЕ (из памяти агента):
{agent_memory}

ДАННЫЕ О ЗАВЕДЕНИИ:
{research_data}

ИСТОРИЯ: видел ли я это заведение раньше?
{history}

Ответь ТОЛЬКО в формате JSON (без markdown, без пояснений):

{{
  "decision": "send_now",
  "reasoning": "объясни своё решение в 1-2 предложениях",
  "is_duplicate": false,
  "duplicate_explanation": null,
  "lead_quality": {{
    "score": 75,
    "is_new_venue": true,
    "opening_soon": true,
    "has_iiko_already": false,
    "has_competitor_system": false,
    "contact_found": true
  }},
  "best_contact": {{
    "phone": "номер или null",
    "instagram": "ссылка или null",
    "name": "имя владельца если известно или null"
  }},
  "outreach_message": "персональное сообщение для WhatsApp на русском, 3-4 предложения, живое, не шаблонное, упомяни название заведения",
  "what_i_learned": "что нового ты узнал из этого лида что поможет в будущем"
}}

Значения decision:
- send_now: горячий лид, отправить немедленно (score >= {score_hot}, новое/открывающееся заведение)
- send_digest: тёплый лид, добавить в утренний дайджест (score {score_warm}-{score_hot_minus1})
- skip: нерелевантно, дубль, уже клиент iiko, не ресторан/кафе
- watch: интересно но мало данных — добавить в список для повторной проверки через 3 дня"""


async def analyze_venue(raw_data: dict) -> dict | None:
    """
    Главная функция — анализирует заведение через Claude и возвращает решение.
    raw_data: словарь с данными о заведении от парсера.
    Возвращает словарь с решением или None при ошибке.
    """
    try:
        # Загружаем знания агента из памяти
        knowledge = database.get_agent_knowledge()
        memory_text = "\n".join([
            f"- [{k['type']}] {k['text']} (уверенность: {k['confidence']:.0%})"
            for k in knowledge
        ]) or "Первый запуск — знаний пока нет."

        # Формируем промпт
        prompt = DECISION_PROMPT.format(
            agent_memory=memory_text,
            research_data=json.dumps(raw_data, ensure_ascii=False, indent=2),
            history="Нет истории по данному заведению.",
            score_hot=SCORE_HOT,
            score_warm=SCORE_WARM,
            score_hot_minus1=SCORE_HOT - 1,
        )

        logger.info(f"Анализирую заведение: {raw_data.get('name', 'Без названия')}")

        # Вызываем Claude
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )

        raw_response = response.content[0].text.strip()

        # Убираем markdown если Claude обернул в блок кода
        if raw_response.startswith("```"):
            lines = raw_response.split("\n")
            raw_response = "\n".join(lines[1:-1])

        result = json.loads(raw_response)

        logger.info(
            f"Решение по '{raw_data.get('name')}': "
            f"{result.get('decision')} (score={result.get('lead_quality', {}).get('score')})"
        )

        return result

    except json.JSONDecodeError as e:
        logger.error(f"Claude вернул невалидный JSON: {e}")
        return None
    except anthropic.APIError as e:
        logger.error(f"Ошибка Anthropic API: {e}")
        return None
    except Exception as e:
        logger.error(f"Неожиданная ошибка в analyze_venue: {e}")
        return None
