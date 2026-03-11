"""
Зависимости для парсинга тела запроса в нескольких форматах.

FastAPI нативно поддерживает только один тип тела за раз.
Эти функции позволяют эндпоинтам принимать данные в трёх форматах:

    Content-Type: application/json
        {"wallets": ["Tabc...", "Tdef..."], "tx_hash": "abc123"}

    Content-Type: application/x-www-form-urlencoded
        wallets=Tabc...&wallets=Tdef...&tx_hash=abc123

    Content-Type: multipart/form-data
        wallets=Tabc...
        wallets=Tdef...
        tx_hash=abc123

Списки (wallets, wallets1, wallets2) передаются повторением поля:
    wallets=T111&wallets=T222   → ["T111", "T222"]

Временные метки принимаются в ISO 8601:
    start_timestamp=2026-01-01T00:00:00Z
"""

import json
import logging
from datetime import datetime

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.models.schemas import (
    GetTransactionsAfterHashRequest,
    GetTransactionsBetweenRequest,
    GetTransactionsRequest,
    GetTransactionsStatsRequest,
)

logger = logging.getLogger(__name__)

_FORM_CONTENT_TYPES = {
    "application/x-www-form-urlencoded",
    "multipart/form-data",
}


def _is_form(request: Request) -> bool:
    """Возвращает True если Content-Type указывает на form-data."""
    ct = request.headers.get("content-type", "")
    return any(ct.startswith(t) for t in _FORM_CONTENT_TYPES)


async def _parse_body(request: Request) -> dict:
    """
    Читает тело запроса и возвращает dict независимо от Content-Type.

    JSON   → json.loads
    Form   → request.form() + нормализация списков
    """
    if _is_form(request):
        form = await request.form()
        data: dict = {}
        for key, value in form.multi_items():
            if key in data:
                if isinstance(data[key], list):
                    data[key].append(value)
                else:
                    data[key] = [data[key], value]
            else:
                data[key] = value
        for list_field in ("wallets", "wallets1", "wallets2"):
            if list_field in data and isinstance(data[list_field], str):
                data[list_field] = [data[list_field]]
        return data
    else:
        try:
            return await request.json()
        except Exception:
            return {}


async def parse_get_transactions_request(
    request: Request,
) -> GetTransactionsRequest:
    """Dependency для POST /transactions — принимает JSON, form-data, urlencoded."""
    data = await _parse_body(request)
    try:
        return GetTransactionsRequest.model_validate(data)
    except ValidationError as exc:
        raise RequestValidationError(errors=exc.errors()) from exc


async def parse_get_transactions_between_request(
    request: Request,
) -> GetTransactionsBetweenRequest:
    """Dependency для POST /transactions/between."""
    data = await _parse_body(request)
    try:
        return GetTransactionsBetweenRequest.model_validate(data)
    except ValidationError as exc:
        raise RequestValidationError(errors=exc.errors()) from exc


async def parse_get_transactions_stats_request(
    request: Request,
) -> GetTransactionsStatsRequest:
    """Dependency для POST /transactions/stats."""
    data = await _parse_body(request)
    try:
        return GetTransactionsStatsRequest.model_validate(data)
    except ValidationError as exc:
        raise RequestValidationError(errors=exc.errors()) from exc


async def parse_get_transactions_after_hash_request(
    request: Request,
) -> GetTransactionsAfterHashRequest:
    """Dependency для POST /transactions/after."""
    data = await _parse_body(request)
    try:
        return GetTransactionsAfterHashRequest.model_validate(data)
    except ValidationError as exc:
        raise RequestValidationError(errors=exc.errors()) from exc