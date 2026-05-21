"""
Память агента — проверка дублей и сохранение знаний.
"""
import json
import logging
from datetime import datetime, timedelta

import database
from config import WATCH_DAYS

logger = logging.getLogger(__name__)


async def check_and_save_lead(raw_data: dict, decision_result: dict) -> tuple[int | None, bool]:
    """
    Проверяет дубль и сохраняет лид в базу.
    Возвращает (lead_id, is_duplicate).
    """
    with database.SessionLocal() as session:
        # Проверяем дубль
        is_dup, dup_reason = database.is_duplicate(
            session,
            name=raw_data.get("name", ""),
            phone=decision_result.get("best_contact", {}).get("phone"),
        )

        if is_dup:
            logger.info(f"Дубль обнаружен: {dup_reason}")
            return None, True

        # Определяем дату watch_until
        watch_until = None
        if decision_result.get("decision") == "watch":
            watch_until = datetime.utcnow() + timedelta(days=WATCH_DAYS)

        lead_quality = decision_result.get("lead_quality", {})
        best_contact = decision_result.get("best_contact", {})

        # Сохраняем лид
        lead_data = {
            "name": raw_data.get("name", "Без названия"),
            "address": raw_data.get("address"),
            "phone": best_contact.get("phone") or raw_data.get("phone"),
            "instagram": best_contact.get("instagram") or raw_data.get("instagram"),
            "website": raw_data.get("website"),
            "source": raw_data.get("source", "unknown"),
            "score": lead_quality.get("score", 0),
            "decision": decision_result.get("decision", "skip"),
            "reasoning": decision_result.get("reasoning"),
            "opening_signal": lead_quality.get("opening_soon", False),
            "outreach_message": decision_result.get("outreach_message"),
            "raw_data_json": json.dumps(raw_data, ensure_ascii=False),
            "watch_until": watch_until,
            "is_duplicate": False,
            "post_url": raw_data.get("url"),
        }

        lead = database.Lead(**lead_data)
        session.add(lead)
        session.commit()
        session.refresh(lead)
        lead_id = lead.id

    # Сохраняем что узнали (what_i_learned)
    what_learned = decision_result.get("what_i_learned")
    if what_learned and len(what_learned) > 20:
        source = raw_data.get("source", "unknown")
        database.save_knowledge(
            insight_type=f"source_{source}",
            insight_text=what_learned,
            confidence=0.6,
        )

    logger.info(
        f"Сохранён лид id={lead_id}: '{raw_data.get('name')}' "
        f"решение={decision_result.get('decision')}"
    )
    return lead_id, False


async def recheck_watch_leads(process_func) -> int:
    """
    Берёт лиды на наблюдении у которых вышел срок и перепроверяет их.
    process_func — функция обработки одного лида (из main.py).
    Возвращает количество перепроверенных.
    """
    leads = database.get_watch_leads()
    if not leads:
        return 0

    logger.info(f"Перепроверяю {len(leads)} лидов из watch-списка")
    for lead in leads:
        raw_data = {
            "name": lead.name,
            "address": lead.address,
            "phone": lead.phone,
            "instagram": lead.instagram,
            "source": lead.source,
            "url": lead.post_url,
            "recheck": True,
        }
        await process_func(raw_data)

    return len(leads)
