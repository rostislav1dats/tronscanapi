"""
Конфигурация приложения через переменные окружения.

Все настройки читаются из файла .env (если он существует)
или напрямую из переменных окружения. Используется pydantic-settings,
что даёт автоматическую валидацию типов и читаемые сообщения об ошибках.

Пример .env:
    TRONGRID_API_KEY=abc123
    SERVICE_MASTER_KEY=supersecret
    LOG_LEVEL=DEBUG
"""

from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """
    Централизованные настройки сервиса.

    Все поля могут быть переопределены через переменные окружения
    или .env файл. Имена переменных нечувствительны к регистру.
    """
    model_config = SettingsConfigDict(
          env_file=".env",
          env_file_encoding="utf-8",
          case_sensitive=False,
    )
     
    TRONGRID_API_KEY: str = ""
    TRONGRID_BASE_URL: str = "https://api.trongrid.io"
    USDT_CONTRACT_ADDRESS: str = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
    HTTP_TIMEOUT_SECONDS: float = 30.0
    HTTP_MAX_RETRIES: int = 3
    TRONGRID_PAGE_SIZE: int = 200
    TRONGRID_PAGE_DELAY: float = 3
    TRONGRID_MAX_CONCURRENT: int = 3
    ALLOWED_ORIGINS: list[str] = ["*"]

    # Уровень логирования: DEBUG | INFO | WARNING | ERROR
    LOG_LEVEL: str = "INFO"
    SERVICE_MASTER_KEY: str = "change-me-in-production"
    SERVICE_API_KEYS: list[str] = []

settings = Settings()