"""
Глобальное состояние бота — разделяется между парсерами, command_bot и post_generator.
"""
from datetime import datetime, date

from config import MAX_POSTS_PER_DAY, MIN_HOURS_BETWEEN_POSTS

# ── Пауза парсинга ──────────────────────────────────────────────────────────
_paused: bool = False


def is_paused() -> bool:
    return _paused


def pause():
    global _paused
    _paused = True


def resume():
    global _paused
    _paused = False


# ── Лимиты публикации в канал ───────────────────────────────────────────────
_posts_today: int = 0
_posts_date: date | None = None
_last_post_time: datetime | None = None


def _reset_if_new_day() -> None:
    """Сбрасывает счётчик если наступил новый день."""
    global _posts_today, _posts_date
    today = date.today()
    if _posts_date != today:
        _posts_today = 0
        _posts_date = today


def can_post() -> bool:
    """
    Проверяет можно ли публиковать пост прямо сейчас.
    Условия: не превышен дневной лимит И прошло минимальное время с последнего поста.
    """
    _reset_if_new_day()
    if _posts_today >= MAX_POSTS_PER_DAY:
        return False
    if _last_post_time is not None:
        elapsed_hours = (datetime.now() - _last_post_time).total_seconds() / 3600
        if elapsed_hours < MIN_HOURS_BETWEEN_POSTS:
            return False
    return True


def record_post() -> None:
    """Фиксирует факт публикации поста."""
    global _posts_today, _last_post_time
    _reset_if_new_day()
    _posts_today += 1
    _last_post_time = datetime.now()


def posts_today() -> int:
    _reset_if_new_day()
    return _posts_today


def hours_since_last_post() -> float:
    if _last_post_time is None:
        return float("inf")
    return (datetime.now() - _last_post_time).total_seconds() / 3600
