"""
Глобальное состояние бота — разделяется между парсерами и command_bot.
"""

_paused: bool = False


def is_paused() -> bool:
    return _paused


def pause():
    global _paused
    _paused = True


def resume():
    global _paused
    _paused = False
