"""
Настройка структурированного логирования для всего сервиса.

Принципы:
- Единый формат для всех логгеров: timestamp | уровень | имя модуля | сообщение
- Весь вывод идёт в stdout (удобно для Docker / systemd / облачных логгеров)
- Сторонние библиотеки (httpx, httpcore) заглушаются до WARNING,
  чтобы не засорять логи служебными HTTP-сообщениями
- Уровень логирования читается из settings.LOG_LEVEL (переменная окружения)

Использование в модулях сервиса:
    import logging
    logger = logging.getLogger(__name__)
    logger.info("Сообщение: %s", value)

Функция setup_logging() вызывается один раз при старте приложения в app/main.py.
"""

import logging
import sys

from app.core.config import settings

_NOISE_LOGGERS = [
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "uvicorn.access",
]

def setup_logging() -> None:
    """
    Инициализирует логирование для всего приложения.

    Настраивает:
    - Корневой логгер с уровнем из settings.LOG_LEVEL
    - Единый форматтер для всех обработчиков
    - StreamHandler → stdout
    - Подавление шумных сторонних логгеров

    Должна вызываться **один раз** при старте приложения,
    до создания экземпляра FastAPI.
    """
    log_level = _resolve_log_level(settings.LOG_LEVEL)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S", 
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.setLevel(log_level)

    app_logger = logging.getLogger("app")
    app_logger.setLevel(log_level)
    app_logger.handlers.clear()
    app_logger.addHandler(handler)
    app_logger.propagate = False 

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    root_logger.handlers.clear()
    root_logger.addHandler(handler)

    for noisy_logger_name in _NOISE_LOGGERS:
        logging.getLogger(noisy_logger_name).setLevel(logging.WARNING)

    if log_level > logging.DEBUG:
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "Логирование инициализировано: уровень=%s", settings.LOG_LEVEL.upper()
    )

def get_uvicorn_log_config() -> dict:
    """
    Конфиг логирования для передачи в uvicorn.run(log_config=...).

    При запуске через CLI этот метод не нужен — логгер "app" с propagate=False
    работает независимо. Нужен только при программном запуске uvicorn.run().
    """
    log_level = settings.LOG_LEVEL.upper()

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                "datefmt": "%Y-%m-%dT%H:%M:%S",
            },
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "default",
            },
        },
        "loggers": {
            "uvicorn":        {"handlers": ["default"], "level": "ERROR",   "propagate": False},
            "uvicorn.error":  {"handlers": ["default"], "level": "ERROR",   "propagate": False},
            "uvicorn.access": {"handlers": ["default"], "level": "WARNING", "propagate": False},
            "fastapi":        {"handlers": ["default"], "level": "WARNING", "propagate": False},
            "app":            {"handlers": ["default"], "level": log_level, "propagate": False},
        },
    }

def _resolve_log_level(level_str: str) -> int:
    """
    Преобразует строковое название уровня логирования в числовую константу.

    Args:
        level_str: Строка вида "DEBUG", "INFO", "WARNING", "ERROR" (регистр не важен).

    Returns:
        Числовая константа logging.* (например, logging.INFO = 20).
        Если строка не распознана — возвращает logging.INFO.
    """
    level = getattr(logging, level_str.upper(), None)
    if not isinstance(level, int):
        logging.warning(
            "Неизвестный уровень логирования %r, используется INFO", level_str
        )
        return logging.INFO
    return level