"""
Вся память агента — SQLite база через SQLAlchemy.
Хранит лиды, знания, статистику источников.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer, String, Text,
    create_engine, func
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config import DATABASE_URL

logger = logging.getLogger(__name__)

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


class Lead(Base):
    """Все найденные заведения"""
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True)
    name = Column(String(500), nullable=False)
    address = Column(String(1000))
    phone = Column(String(100))
    instagram = Column(String(300))
    website = Column(String(500))
    source = Column(String(50), nullable=False)  # instagram, olx, tg_channels
    score = Column(Integer, default=0)
    decision = Column(String(20))  # send_now, send_digest, skip, watch
    reasoning = Column(Text)
    opening_signal = Column(Boolean, default=False)
    outreach_message = Column(Text)
    raw_data_json = Column(Text)
    found_at = Column(DateTime, default=datetime.utcnow)
    sent_at = Column(DateTime)
    watch_until = Column(DateTime)
    is_client = Column(Boolean, default=False)
    telegram_message_id = Column(Integer)
    is_duplicate = Column(Boolean, default=False)
    post_url = Column(String(1000))


class AgentKnowledge(Base):
    """Чему научился агент"""
    __tablename__ = "agent_knowledge"

    id = Column(Integer, primary_key=True)
    insight_type = Column(String(100))
    insight_text = Column(Text)
    confidence = Column(Float, default=0.5)
    created_at = Column(DateTime, default=datetime.utcnow)
    times_confirmed = Column(Integer, default=1)


class SourceStats(Base):
    """Статистика по источникам"""
    __tablename__ = "source_stats"

    id = Column(Integer, primary_key=True)
    source_name = Column(String(50), unique=True)
    total_found = Column(Integer, default=0)
    total_sent = Column(Integer, default=0)
    hot_leads_count = Column(Integer, default=0)
    avg_score = Column(Float, default=0)
    last_run_at = Column(DateTime)
    errors_count = Column(Integer, default=0)


def init_db():
    """Создать все таблицы при первом запуске"""
    Base.metadata.create_all(engine)
    # Инициализируем статистику источников
    with SessionLocal() as session:
        for source in ["instagram", "olx", "tg_channels", "2gis"]:
            exists = session.query(SourceStats).filter_by(source_name=source).first()
            if not exists:
                session.add(SourceStats(source_name=source))
        session.commit()
    logger.info("База данных инициализирована")


def is_duplicate(session: Session, name: str, phone: Optional[str]) -> tuple[bool, str]:
    """
    Проверяем дубль по телефону (точное совпадение) или
    по названию (нечёткое совпадение > 85%)
    """
    from thefuzz import fuzz

    # Проверка по телефону
    if phone:
        clean_phone = "".join(filter(str.isdigit, phone))
        if len(clean_phone) >= 7:
            existing = session.query(Lead).filter(
                Lead.phone.isnot(None)
            ).all()
            for lead in existing:
                if lead.phone:
                    existing_phone = "".join(filter(str.isdigit, lead.phone))
                    if existing_phone and existing_phone[-7:] == clean_phone[-7:]:
                        return True, f"Тот же телефон: {lead.name} (id={lead.id})"

    # Проверка по названию
    existing_leads = session.query(Lead).filter(
        Lead.decision != "skip"
    ).all()
    for lead in existing_leads:
        similarity = fuzz.ratio(name.lower(), lead.name.lower())
        if similarity >= 85:
            return True, f"Похожее название ({similarity}%): {lead.name} (id={lead.id})"

    return False, ""


def save_lead(lead_data: dict) -> int:
    """Сохранить лид в базу. Возвращает ID."""
    with SessionLocal() as session:
        lead = Lead(**lead_data)
        session.add(lead)
        session.commit()
        session.refresh(lead)
        return lead.id


def mark_lead_sent(lead_id: int, telegram_message_id: Optional[int] = None):
    """Пометить лид как отправленный"""
    with SessionLocal() as session:
        lead = session.get(Lead, lead_id)
        if lead:
            lead.sent_at = datetime.utcnow()
            if telegram_message_id:
                lead.telegram_message_id = telegram_message_id
            session.commit()


def update_source_stats(source: str, found: int = 0, sent: int = 0,
                        hot: int = 0, score: float = 0, error: bool = False):
    """Обновить статистику источника"""
    with SessionLocal() as session:
        stats = session.query(SourceStats).filter_by(source_name=source).first()
        if not stats:
            stats = SourceStats(source_name=source)
            session.add(stats)
        stats.total_found += found
        stats.total_sent += sent
        stats.hot_leads_count += hot
        if score > 0:
            # Скользящее среднее
            if stats.avg_score == 0:
                stats.avg_score = score
            else:
                stats.avg_score = (stats.avg_score + score) / 2
        if error:
            stats.errors_count += 1
        stats.last_run_at = datetime.utcnow()
        session.commit()


def get_watch_leads() -> list[Lead]:
    """Лиды для повторной проверки (watch)"""
    with SessionLocal() as session:
        now = datetime.utcnow()
        leads = session.query(Lead).filter(
            Lead.decision == "watch",
            Lead.watch_until <= now
        ).all()
        # detach from session
        session.expunge_all()
        return leads


def get_digest_leads() -> list[Lead]:
    """Тёплые лиды для утреннего дайджеста (не отправленные)"""
    with SessionLocal() as session:
        leads = session.query(Lead).filter(
            Lead.decision == "send_digest",
            Lead.sent_at.is_(None)
        ).order_by(Lead.score.desc()).all()
        session.expunge_all()
        return leads


def get_last_24h_stats() -> dict:
    """Статистика за последние 24 часа для самообучения"""
    with SessionLocal() as session:
        since = datetime.utcnow() - timedelta(hours=24)
        total = session.query(func.count(Lead.id)).filter(Lead.found_at >= since).scalar()
        hot = session.query(func.count(Lead.id)).filter(
            Lead.found_at >= since, Lead.decision == "send_now"
        ).scalar()
        warm = session.query(func.count(Lead.id)).filter(
            Lead.found_at >= since, Lead.decision == "send_digest"
        ).scalar()
        skipped = session.query(func.count(Lead.id)).filter(
            Lead.found_at >= since, Lead.decision == "skip"
        ).scalar()
        watch = session.query(func.count(Lead.id)).filter(
            Lead.found_at >= since, Lead.decision == "watch"
        ).scalar()

        # Статистика по источникам
        source_stats = {}
        for source in ["instagram", "olx", "tg_channels"]:
            count = session.query(func.count(Lead.id)).filter(
                Lead.found_at >= since, Lead.source == source
            ).scalar()
            hot_from_source = session.query(func.count(Lead.id)).filter(
                Lead.found_at >= since, Lead.source == source,
                Lead.decision == "send_now"
            ).scalar()
            source_stats[source] = {"total": count, "hot": hot_from_source}

        # Последние 50 решений
        recent = session.query(Lead).filter(
            Lead.found_at >= since
        ).order_by(Lead.found_at.desc()).limit(50).all()
        recent_decisions = [
            {
                "name": l.name,
                "source": l.source,
                "score": l.score,
                "decision": l.decision,
                "reasoning": l.reasoning,
            }
            for l in recent
        ]

        return {
            "total": total,
            "hot": hot,
            "warm": warm,
            "skipped": skipped,
            "watch": watch,
            "source_stats": source_stats,
            "recent_decisions": recent_decisions,
        }


def get_agent_knowledge() -> list[dict]:
    """Все знания агента для передачи в промпт"""
    with SessionLocal() as session:
        knowledge = session.query(AgentKnowledge).order_by(
            AgentKnowledge.confidence.desc()
        ).limit(20).all()
        return [
            {
                "type": k.insight_type,
                "text": k.insight_text,
                "confidence": k.confidence,
                "confirmed": k.times_confirmed,
            }
            for k in knowledge
        ]


def save_knowledge(insight_type: str, insight_text: str, confidence: float = 0.7):
    """Сохранить новое знание агента"""
    with SessionLocal() as session:
        # Проверяем похожее знание
        existing = session.query(AgentKnowledge).filter_by(
            insight_type=insight_type
        ).first()
        if existing and insight_text.lower() in existing.insight_text.lower():
            existing.times_confirmed += 1
            existing.confidence = min(1.0, existing.confidence + 0.05)
        else:
            session.add(AgentKnowledge(
                insight_type=insight_type,
                insight_text=insight_text,
                confidence=confidence,
            ))
        session.commit()
