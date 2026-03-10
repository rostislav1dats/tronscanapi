"""
Tron Wallet Service — точка входа FastAPI приложения.

Запуск:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

Переменные окружения (см. .env.example):
    TRONGRID_API_KEY       — ключ TronGrid API
    SERVICE_MASTER_KEY     — мастер-ключ для управления API-ключами
    SERVICE_API_KEYS       — начальный набор ключей (необязательно)
    LOG_LEVEL              — уровень логирования (INFO по умолчанию)
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import api_keys, transactions
from app.core.config import settings
from app.core.logging import setup_logging
from app.core.security import init_api_keys_from_config

# 1. Настраиваем логирование до всего остального
setup_logging()

# 2. Загружаем начальные API-ключи из переменной SERVICE_API_KEYS
init_api_keys_from_config(settings.SERVICE_API_KEYS)

from app.core.config import settings

print(f"DEBUG: Master Key loaded: {settings.SERVICE_MASTER_KEY}")


app = FastAPI(
    title='Tron Wallet Service',
    description=(
        "Сервис для получения и анализа USDT (TRC20) транзакций "
        "через TronGrid API. Поддерживает работу с несколькими кошельками, "
        "временными диапазонами и цепочками переводов.\n\n"
        "**Аутентификация:** все запросы к `/transactions/*` требуют заголовка "
        "`X-API-Key: <ваш_ключ>`. Управление ключами — через `/api-keys` "
        "(требуется мастер-ключ)."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Транзакции — каждый эндпоинт защищён Depends(verify_api_key)
app.include_router(
    transactions.router,
    prefix="/transactions",
    tags=["transactions"],
)

# Управление API-ключами — каждый эндпоинт защищён Depends(verify_master_key)
app.include_router(
    api_keys.router,
    prefix="/api-keys",
    tags=["api-keys"],
)

@app.get("/health", tags=["health"])
async def health_check() -> dict:
    """Проверка работоспособности сервера. Не требует ауутентификации."""
    return {"status": "ok", "service": "tron-wallet-service"}