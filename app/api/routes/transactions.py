"""
FastAPI-роутер для всех эндпоинтов транзакций.

Все маршруты защищены зависимостью verify_api_key:
клиент обязан передавать заголовок X-API-Key с действующим ключом.
При неверном или отсутствующем ключе возвращается HTTP 401.

Маршруты:
    POST /transactions              — 3.1 Все транзакции по кошелькам
    POST /transactions/between      — 3.2 Транзакции между двумя сторонами
    POST /transactions/stats        — 3.3 Статистика транзакций
    POST /transactions/after        — 3.4 Транзакции после указанного хеша
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.exceptions import (
    InvalidWalletAddressError,
    TransactionNotFoundError,
    TronGridAPIError,
    TronNetworkError,
)
from app.core.security import ApiKeyInfo, verify_api_key
from app.models.schemas import (
    GetTransactionsAfterHashRequest,
    GetTransactionsAfterHashResponse,
    GetTransactionsBetweenRequest,
    GetTransactionsBetweenResponse,
    GetTransactionsRequest,
    GetTransactionsResponse,
    GetTransactionsStatsRequest,
    GetTransactionsStatsResponse,
)
from app.services.transaction_service import TransactionService
from app.services.trongrid_client import TronGridClient

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_service(client: TronGridClient) -> TransactionService:
    """
    Фабрика TransactionService.

    Вынесена в отдельную функцию для удобной замены в тестах
    через unittest.mock.patch.
    """
    return TransactionService(client)

@router.post(
    "",
    response_model=GetTransactionsResponse,
    summary="Получить все транзакции по кошелькам",
    description=(
        "Возвращает все USDT (TRC20) транзакции для одного или нескольких кошельков. "
        "Транзакции дедуплицированы и отсортированы от старой к новой.\n\n"
        "Поддерживает необязательную фильтрацию по временному диапазону "
        "(`start_timestamp`, `end_timestamp`).\n\n"
        "**Требуется заголовок `X-API-Key`.**"
    ),
    responses={
        401: {"description": "Отсутствует или неверный API-ключ"},
        422: {"description": "Некорректный адрес кошелька"},
        502: {"description": "Ошибка TronGrid API или сетевой сбой"},
    },
)
async def get_transactions(
    body: GetTransactionsRequest,
    auth: ApiKeyInfo = Depends(verify_api_key),
) -> GetTransactionsResponse:
    """
    Endpoint 3.1: Все транзакции по кошелькам.

    Args:
        body: Тело запроса — список кошельков и опциональные временные границы.
        auth: Метаданные API-ключа (инжектируются зависимостью verify_api_key).
              Используются для логирования.

    Returns:
        GetTransactionsResponse — список транзакций по запрошенным кошелькам.
    """
    logger.info(
        "POST /transactions: owner=%r wallets=%s", auth.owner, body.wallets
    )
    async with TronGridClient() as client:
        service = _get_service(client)
        try:
            txs = await service.get_transactions(
                wallets=body.wallets,
                start_timestamp=body.start_timestamp,
                end_timestamp=body.end_timestamp,
            )
        except InvalidWalletAddressError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        except (TronGridAPIError, TronNetworkError) as exc:
            logger.error("TronGrid error at /transactions: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc

    return GetTransactionsResponse(wallets=body.wallets, transactions=txs)

@router.post(
    "/between",
    response_model=GetTransactionsBetweenResponse,
    summary="Транзакции между двумя сторонами",
    description=(
        "Возвращает транзакции USDT между группой `wallets1` и кошельком `wallet2` "
        "в обоих направлениях.\n\n"
        "**Требуется заголовок `X-API-Key`.**"
    ),
    responses={
        401: {"description": "Отсутствует или неверный API-ключ"},
        422: {"description": "Некорректный адрес кошелька"},
        502: {"description": "Ошибка TronGrid API или сетевой сбой"},
    },
)
async def get_transactions_between(
    body: GetTransactionsBetweenRequest,
    auth: ApiKeyInfo = Depends(verify_api_key),
) -> GetTransactionsBetweenResponse:
    """
    Endpoint 3.2: Транзакции между двумя сторонами.

    Args:
        body: Тело запроса — wallets1, wallet2 и опциональные временные границы.
        auth: Метаданные API-ключа.

    Returns:
        GetTransactionsBetweenResponse — транзакции в обоих направлениях.
    """
    logger.info(
        "POST /transactions/between: owner=%r wallets1=%s wallet2=%s",
        auth.owner, body.wallets1, body.wallet2,
    )
    async with TronGridClient() as client:
        service = _get_service(client)
        try:
            txs = await service.get_transactions_between(
                wallets1=body.wallets1,
                wallet2=body.wallet2,
                start_timestamp=body.start_timestamp,
                end_timestamp=body.end_timestamp,
            )
        except InvalidWalletAddressError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        except (TronGridAPIError, TronNetworkError) as exc:
            logger.error("TronGrid error at /transactions/between: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc

    return GetTransactionsBetweenResponse(
        wallets1=body.wallets1,
        wallet2=body.wallet2,
        transactions=txs,
    )

@router.post(
    "/stats",
    response_model=GetTransactionsStatsResponse,
    summary="Статистика транзакций между группами кошельков",
    description=(
        "Подсчитывает суммарные объёмы переводов USDT между двумя группами кошельков "
        "в обоих направлениях и возвращает разницу.\n\n"
        "**Требуется заголовок `X-API-Key`.**"
    ),
    responses={
        401: {"description": "Отсутствует или неверный API-ключ"},
        422: {"description": "Некорректный адрес кошелька"},
        502: {"description": "Ошибка TronGrid API или сетевой сбой"},
    },
)
async def get_transactions_stats(
    body: GetTransactionsStatsRequest,
    auth: ApiKeyInfo = Depends(verify_api_key),
) -> GetTransactionsStatsResponse:
    """
    Endpoint 3.3: Статистика транзакций.

    Args:
        body: Тело запроса — wallets1, wallets2 и опциональные временные границы.
        auth: Метаданные API-ключа.

    Returns:
        GetTransactionsStatsResponse — суммы и разница между группами.
    """
    logger.info(
        "POST /transactions/stats: owner=%r wallets1=%s wallets2=%s",
        auth.owner, body.wallets1, body.wallets2,
    )
    async with TronGridClient() as client:
        service = _get_service(client)
        try:
            stats = await service.get_transactions_stats(
                wallets1=body.wallets1,
                wallets2=body.wallets2,
                start_timestamp=body.start_timestamp,
                end_timestamp=body.end_timestamp,
            )
        except InvalidWalletAddressError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        except (TronGridAPIError, TronNetworkError) as exc:
            logger.error("TronGrid error at /transactions/stats: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc

    return GetTransactionsStatsResponse(**stats)

@router.post(
    "/after",
    response_model=GetTransactionsAfterHashResponse,
    summary="Транзакции после указанного хеша",
    description=(
        "Возвращает все USDT-транзакции кошельков, совершённые "
        "после транзакции с указанным `tx_hash`.\n\n"
        "Транзакция по хешу служит якорем — возвращаются все более поздние записи.\n\n"
        "**Требуется заголовок `X-API-Key`.**"
    ),
    responses={
        401: {"description": "Отсутствует или неверный API-ключ"},
        404: {"description": "Транзакция с указанным tx_hash не найдена"},
        422: {"description": "Некорректный адрес кошелька"},
        502: {"description": "Ошибка TronGrid API или сетевой сбой"},
    },
)
async def get_transactions_after_hash(
    body: GetTransactionsAfterHashRequest,
    auth: ApiKeyInfo = Depends(verify_api_key),
) -> GetTransactionsAfterHashResponse:
    """
    Endpoint 3.4: Транзакции после хеша.

    Args:
        body: Тело запроса — список кошельков и хеш транзакции-якоря.
        auth: Метаданные API-ключа.

    Returns:
        GetTransactionsAfterHashResponse — транзакции после якорной.
    """
    logger.info(
        "POST /transactions/after: owner=%r wallets=%s tx_hash=%s",
        auth.owner, body.wallets, body.tx_hash,
    )
    async with TronGridClient() as client:
        service = _get_service(client)
        try:
            txs = await service.get_transactions_after_hash(
                wallets=body.wallets,
                tx_hash=body.tx_hash,
            )
        except TransactionNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except InvalidWalletAddressError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        except (TronGridAPIError, TronNetworkError) as exc:
            logger.error("TronGrid error at /transactions/after: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc

    return GetTransactionsAfterHashResponse(
        wallets=body.wallets,
        transactions_after=txs,
    )