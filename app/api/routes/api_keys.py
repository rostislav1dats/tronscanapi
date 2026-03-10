"""
FastAPI-роутер управления API-ключами сервиса.

Все маршруты защищены мастер-ключом (заголовок X-API-Key = SERVICE_MASTER_KEY).
Обычные API-ключи для этих эндпоинтов не подходят — только мастер-ключ.

Маршруты:
    POST   /api-keys              — создать новый API-ключ
    GET    /api-keys              — список всех ключей (без значений)
    DELETE /api-keys/{key_id}     — отозвать ключ по key_id

Жизненный цикл ключа:
    Создание (POST) → Использование (X-API-Key в запросах) → Отзыв (DELETE)

После отзыва ключ остаётся в хранилище с is_active=False (для аудита),
но все запросы с ним будут возвращать HTTP 401.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.security import (
    ApiKeyInfo,
    api_key_store,
    generate_api_key,
    verify_master_key,
)

logger = logging.getLogger(__name__)

router = APIRouter()

class CreateApiKeyRequest(BaseModel):
    """Запрос на создание нового API-ключа."""

    owner: str = "unnamed"


class CreateApiKeyResponse(BaseModel):
    """
    Ответ с новым API-ключом.

    ВАЖНО: поле `key` отображается ТОЛЬКО ОДИН РАЗ при создании.
    После этого чистый текст ключа нигде не хранится и не может
    быть восстановлен. Сохраните ключ в безопасном месте.
    """

    key: str
    key_id: str
    owner: str


class ApiKeyListItem(BaseModel):
    """Элемент списка ключей. Поле `key` (чистый текст) намеренно отсутствует."""

    key_id: str
    owner: str
    is_active: bool
    created_at: str


class RevokeApiKeyResponse(BaseModel):
    """Ответ на запрос отзыва ключа."""

    key_id: str
    revoked: bool

@router.post(
    "",
    response_model=CreateApiKeyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Создать новый API-ключ",
    description=(
        "Генерирует новый API-ключ для доступа к `/transactions/*`.\n\n"
        "**Требуется мастер-ключ** в заголовке `X-API-Key`.\n\n"
        "⚠️ Возвращённый ключ показывается **только один раз** — сохраните его."
    ),
    responses={
        401: {"description": "Неверный или отсутствующий мастер-ключ"},
    },
)
def create_api_key(
    body: CreateApiKeyRequest,
    _: None = Depends(verify_master_key),
) -> CreateApiKeyResponse:
    """
    Создаёт новый API-ключ.

    Генерирует 32-символьный hex-ключ (128 бит энтропии) через secrets.token_hex.
    Ключ хранится в ApiKeyStore только в виде SHA-256 хеша.

    Args:
        body: Тело запроса — имя владельца ключа (необязательно).
        _: Зависимость verify_master_key (проверяет мастер-ключ, возвращает None).

    Returns:
        CreateApiKeyResponse — новый ключ и его метаданные.
                               Поле key показывается только здесь.
    """
    new_key = generate_api_key()
    info: ApiKeyInfo = api_key_store.add(new_key, body.owner)

    logger.info("Создан новый API-ключ: owner=%r key_id=%s", info.owner, info.key_id)

    return CreateApiKeyResponse(
        key=new_key,
        key_id=info.key_id,
        owner=info.owner,
    )


@router.get(
    "",
    response_model=list[ApiKeyListItem],
    summary="Список всех API-ключей",
    description=(
        "Возвращает все ключи: активные и отозванные. "
        "Значения ключей не включаются.\n\n"
        "**Требуется мастер-ключ** в заголовке `X-API-Key`."
    ),
    responses={
        401: {"description": "Неверный или отсутствующий мастер-ключ"},
    },
)
def list_api_keys(
    _: None = Depends(verify_master_key),
) -> list[ApiKeyListItem]:
    """
    Возвращает список всех API-ключей без их значений.

    Returns:
        Список ApiKeyListItem, отсортированный по времени создания.
    """
    return [
        ApiKeyListItem(
            key_id=info.key_id,
            owner=info.owner,
            is_active=info.is_active,
            created_at=info.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        for info in api_key_store.list_keys()
    ]


@router.delete(
    "/{key_id}",
    response_model=RevokeApiKeyResponse,
    summary="Отозвать API-ключ",
    description=(
        "Деактивирует ключ по `key_id`. После отзыва все запросы "
        "с этим ключом будут возвращать HTTP 401.\n\n"
        "**Требуется мастер-ключ** в заголовке `X-API-Key`."
    ),
    responses={
        401: {"description": "Неверный или отсутствующий мастер-ключ"},
        404: {"description": "Ключ с указанным key_id не найден"},
    },
)
def revoke_api_key(
    key_id: str,
    _: None = Depends(verify_master_key),
) -> RevokeApiKeyResponse:
    """
    Отзывает (деактивирует) API-ключ.

    Args:
        key_id: Идентификатор ключа (из поля key_id при создании).
        _: Зависимость verify_master_key.

    Returns:
        RevokeApiKeyResponse — результат операции.

    Raises:
        HTTPException 404: Если ключ с таким key_id не существует.
    """
    revoked = api_key_store.revoke(key_id)
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"API-ключ с key_id={key_id!r} не найден.",
        )
    logger.info("API-ключ отозван через DELETE /api-keys/%s", key_id)
    return RevokeApiKeyResponse(key_id=key_id, revoked=True)