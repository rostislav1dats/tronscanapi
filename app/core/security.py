"""
Аутентификация сервиса через API-ключи.

Схема работы
------------
1. Клиент передаёт ключ в HTTP-заголовке:   X-API-Key: <key>
2. FastAPI-зависимость verify_api_key перехватывает заголовок
3. Ключ хешируется (SHA-256) и ищется в ApiKeyStore
4. При совпадении возвращается ApiKeyInfo с метаданными владельца
5. При неверном / отсутствующем ключе — HTTP 401 Unauthorized

Управление ключами
------------------
- Ключи хранятся в памяти (ApiKeyStore) — заменяемо на БД без
  изменения интерфейса зависимостей и роутеров
- Начальный набор ключей загружается из переменной SERVICE_API_KEYS при старте
- Роутер /api-keys позволяет создавать / отзывать ключи в рантайме
  (доступ только с мастер-ключом SERVICE_MASTER_KEY)

Безопасность
------------
- Чистый текст ключа НИГДЕ не хранится — только SHA-256 хеш
- Ключ показывается только один раз (при создании через POST /api-keys)
- Сравнение мастер-ключа через secrets.compare_digest (защита от timing-атак)
- Генерация ключей через secrets.token_hex(16) — 128 бит энтропии
"""

import hashlib
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastAPI security scheme — описывает X-API-Key в OpenAPI / Swagger UI
# ---------------------------------------------------------------------------

_API_KEY_HEADER = APIKeyHeader(
    name="X-API-Key",
    description=(
        "API-ключ для доступа к сервису. "
        "Передаётся в HTTP-заголовке: `X-API-Key: <ваш_ключ>`. "
        "Ключи выдаются через POST /api-keys (требуется мастер-ключ)."
    ),
    auto_error=False
)

@dataclass
class ApiKeyInfo:
    """
    Метаданные API-ключа.

    Чистый текст ключа в этом объекте НИКОГДА не хранится.

    Attributes:
        key_id:     Первые 12 символов SHA-256 хеша ключа.
                    Используется как публичный идентификатор (для отзыва, списка).
        owner:      Произвольное имя / описание владельца ключа.
        created_at: Время создания ключа (UTC).
        is_active:  False — ключ отозван и больше не принимается.
    """

    key_id: str
    owner: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    is_active: bool = True

class ApiKeyStore:
    """
    In-memory хранилище API-ключей.

    Ключ хранится исключительно как SHA-256 хеш — чистый текст
    нигде не сохраняется и не может быть восстановлен.

    При проверке входящий ключ хешируется и сравнивается с хранимым хешем.
    """

    def __init__(self) -> None:
        self._store: dict[str, ApiKeyInfo] = {}

    def add(self, key: str, owner: str) -> ApiKeyInfo:
        """
        Добавляет новый API-ключ в хранилище.

        Args:
            key:   Чистый текст ключа. Передаётся только при создании,
                   после этого восстановить его невозможно.
            owner: Имя или описание владельца (произвольная строка).

        Returns:
            ApiKeyInfo — метаданные сохранённого ключа (без самого ключа).
        """
        key_hash = self._hash(key)
        info = ApiKeyInfo(key_id=key_hash[:12], owner=owner)
        self._store[key_hash] = info
        logger.info("API-ключ добавлен: owner=%r key_id=%s", owner, info.key_id)
        return info
    
    def revoke(self, key_id: str) -> bool:
        """
        Деактивирует ключ по его key_id.

        После отзыва все запросы с этим ключом будут возвращать HTTP 401.
        Запись в хранилище сохраняется (is_active=False) — для аудита.

        Args:
            key_id: Первые 12 символов SHA-256 хеша (поле из ApiKeyInfo).

        Returns:
            True — ключ найден и деактивирован.
            False — ключ с таким key_id не найден.
        """
        for info in self._store.values():
            if info.key_id == key_id:
                info.is_active = False
                logger.info(
                    "API-ключ отозван: key_id=%s owner=%r", key_id, info.owner
                )
                return True
        logger.warning("Попытка отозвать несуществующий ключ: key_id=%s", key_id)
        return False
    
    def verify(self, key: str) -> ApiKeyInfo | None:
        """
        Проверяет ключ и возвращает его метаданные.

        Args:
            key: Чистый текст ключа из HTTP-заголовка X-API-Key.

        Returns:
            ApiKeyInfo — если ключ существует и активен.
            None       — если ключ не найден или отозван.
        """
        info = self._store.get(self._hash(key))
        if info is None:
            return None
        if not info.is_active:
            logger.warning("Использование отозванного ключа: key_id=%s", info.key_id)
            return None
        return info

    def list_keys(self) -> list[ApiKeyInfo]:
        """
        Возвращает все ключи (активные и отозванные).

        Returns:
            Список ApiKeyInfo, отсортированный по времени создания (новые — последними).
        """
        return sorted(self._store.values(), key=lambda k: k.created_at)
    
    @staticmethod
    def _hash(key: str) -> str:
        """Возвращает SHA-256 хеш ключа в виде hex-строки."""
        return hashlib.sha256(key.encode("utf-8")).hexdigest()
    
api_key_store = ApiKeyStore()

def init_api_keys_from_config(raw_keys: list[str]) -> None:
    """
    Загружает начальный набор API-ключей из конфигурации при старте сервиса.

    Вызывается один раз в app/main.py до запуска сервера.

    Формат каждого элемента raw_keys:
        "ключ:владелец"  →  owner = "владелец"
        "ключ"           →  owner = "unknown"

    Args:
        raw_keys: Список строк из settings.SERVICE_API_KEYS.
                  Пустые строки и строки из пробелов игнорируются.

    Example:
        init_api_keys_from_config(["abc123:mobile-app", "xyz789:backend"])
    """
    loaded = 0
    for entry in raw_keys:
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            key, owner = entry.split(":", 1)
        else:
            key, owner = entry, "unknown"
        api_key_store.add(key.strip(), owner.strip())
        loaded += 1

    if loaded:
        logger.info("Загружено %d API-ключей из конфигурации", loaded)
    else:
        logger.info(
            "SERVICE_API_KEYS не заданы. Используйте POST /api-keys для создания ключей."
        )

def generate_api_key() -> str:
    """
    Генерирует криптографически случайный API-ключ.

    Использует secrets.token_hex — криптографически стойкий ГПСЧ,
    подходящий для генерации секретов (PEP 506).

    Returns:
        32-символьная hex-строка (128 бит энтропии).

    Example:
        >>> key = generate_api_key()
        >>> len(key)
        32
        >>> all(c in "0123456789abcdef" for c in key)
        True
    """
    return secrets.token_hex(16)

def verify_api_key(raw_key: str | None = Security(_API_KEY_HEADER)) -> ApiKeyInfo:
    """
    FastAPI-зависимость: проверяет API-ключ из заголовка X-API-Key.

    Подключается к эндпоинтам через Depends:

        from app.core.security import verify_api_key, ApiKeyInfo
        from fastapi import Depends

        @router.post("/some-endpoint")
        async def my_endpoint(auth: ApiKeyInfo = Depends(verify_api_key)):
            # auth.owner — имя владельца ключа
            ...

    Args:
        raw_key: Значение заголовка X-API-Key (None если заголовок отсутствует).

    Returns:
        ApiKeyInfo — метаданные валидного ключа (owner, key_id, created_at).

    Raises:
        HTTPException 401: Если ключ отсутствует, неверен или отозван.
                           Заголовок WWW-Authenticate: ApiKey включён для
                           соответствия RFC 7235.
    """
    if not raw_key:
        logger.warning("Запрос без X-API-Key заголовка")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется API-ключ. Укажите заголовок: X-API-Key: <ваш_ключ>",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    key_info = api_key_store.verify(raw_key)

    if key_info is None:
        # Не логируем сам ключ — только первые 8 символов для диагностики
        logger.warning(
            "Неверный или отозванный API-ключ (prefix=%s...)", raw_key[:8]
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный или отозванный API-ключ.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    logger.debug(
        "Аутентификация успешна: owner=%r key_id=%s", key_info.owner, key_info.key_id
    )
    return key_info

def verify_master_key(raw_key: str | None = Security(_API_KEY_HEADER)) -> None:
    """
    FastAPI-зависимость: проверяет мастер-ключ для управления API-ключами.

    Используется только в роутере /api-keys.
    Мастер-ключ задаётся через переменную окружения SERVICE_MASTER_KEY.

    Сравнение выполняется через secrets.compare_digest — это защищает
    от timing-атак (атак по времени выполнения), при которых злоумышленник
    мог бы угадать ключ посимвольно по времени ответа.

    Args:
        raw_key: Значение заголовка X-API-Key.

    Raises:
        HTTPException 401: Если ключ не совпадает с SERVICE_MASTER_KEY.
    """
    from app.core.config import settings

    if not raw_key or not secrets.compare_digest(raw_key, settings.SERVICE_MASTER_KEY):
        logger.warning("Неверный апи ключ при обращении к /api-keys")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный апи ключ.",
            headers={"WWW-Authenticate": "ApiKey"},
        )