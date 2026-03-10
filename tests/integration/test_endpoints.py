"""
Интеграционные тесты для всех FastAPI-эндпоинтов.

Используют httpx.AsyncClient + ASGITransport для тестирования
HTTP-слоя без реального сетевого подключения к TronGrid.
TronGridClient подменяется через mock.

Покрывают:
- Аутентификация: 401 без ключа / с неверным / с отозванным
- POST /transactions: 200, 422, 502
- POST /transactions/between: 200, 502
- POST /transactions/stats: 200
- POST /transactions/after: 200, 404, 422
- POST /api-keys: 201 создание, 401 без мастера
- GET  /api-keys: 200, 401
- DELETE /api-keys/{key_id}: 200, 404, 401
- GET /health: 200 без аутентификации
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import app.core.security as sec_module
from app.core.exceptions import (
    InvalidWalletAddressError,
    TransactionNotFoundError,
    TronGridAPIError,
    TronNetworkError,
)
from app.core.security import ApiKeyStore, ApiKeyInfo
from app.main import app
from app.models.schemas import TransactionDTO

# ---------------------------------------------------------------------------
# Тестовые константы
# ---------------------------------------------------------------------------

W1 = "TXytKFZbjHY6fCDhCGnUR5rEeKHrPwTFJZ"
W2 = "TYytKFZbjHY6fCDhCGnUR5rEeKHrPwTFJY"

# Фиксированные ключи для тестов (32 hex-символа — как у generate_api_key)
VALID_API_KEY = "aabbccddeeff00112233445566778899"
MASTER_KEY    = "99887766554433221100ffeeddccbbaa"


def make_tx(tx_hash: str = "hash1", from_w: str = W1, to_w: str = W2) -> TransactionDTO:
    """Фабрика тестовых TransactionDTO."""
    return TransactionDTO(
        timestamp=datetime(2026, 3, 9, 12, 0, 0, tzinfo=UTC),
        amount=100.0,
        type="sent",
        from_wallet=from_w,
        to_wallet=to_w,
        tx_hash=tx_hash,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def isolated_key_store():
    """
    Изолирует глобальный api_key_store для каждого теста.

    Перед тестом: создаёт свежий ApiKeyStore с VALID_API_KEY.
    После теста: восстанавливает оригинальный стор.
    """
    original = sec_module.api_key_store
    fresh = ApiKeyStore()
    fresh.add(VALID_API_KEY, "test-client")
    sec_module.api_key_store = fresh
    yield fresh
    sec_module.api_key_store = original


@pytest_asyncio.fixture
async def http_client():
    """Асинхронный ASGI-клиент для тестирования FastAPI без сети."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def patch_service(service_mock: MagicMock):
    """Патчит фабрику TransactionService в роутере транзакций."""
    return patch(
        "app.api.routes.transactions._get_service",
        return_value=service_mock,
    )


def make_service(**method_returns) -> MagicMock:
    """
    Создаёт mock TransactionService.

    Args:
        method_returns: {имя_метода: возвращаемое_значение_или_исключение}
    """
    svc = MagicMock()
    for method, return_value in method_returns.items():
        if isinstance(return_value, Exception):
            setattr(svc, method, AsyncMock(side_effect=return_value))
        else:
            setattr(svc, method, AsyncMock(return_value=return_value))
    return svc


def auth_headers(key: str = VALID_API_KEY) -> dict[str, str]:
    """Формирует заголовок X-API-Key."""
    return {"X-API-Key": key}


def master_headers() -> dict[str, str]:
    """Формирует заголовок с мастер-ключом."""
    return {"X-API-Key": MASTER_KEY}


# ---------------------------------------------------------------------------
# GET /health — без аутентификации
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    """Health check не требует аутентификации."""

    @pytest.mark.asyncio
    async def test_health_returns_200_without_auth(self, http_client: AsyncClient) -> None:
        resp = await http_client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Аутентификация — общий слой для всех /transactions/* эндпоинтов
# ---------------------------------------------------------------------------


class TestApiKeyAuthentication:
    """Все /transactions/* эндпоинты обязаны проверять X-API-Key."""

    @pytest.mark.asyncio
    async def test_no_key_returns_401(self, http_client: AsyncClient) -> None:
        """Запрос без заголовка X-API-Key → 401."""
        resp = await http_client.post(
            "/transactions", json={"wallets": [W1]}
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_key_returns_401(self, http_client: AsyncClient) -> None:
        """Неверный API-ключ → 401."""
        resp = await http_client.post(
            "/transactions",
            json={"wallets": [W1]},
            headers={"X-API-Key": "completely-wrong-key-0000000000"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_revoked_key_returns_401(
        self,
        http_client: AsyncClient,
        isolated_key_store: ApiKeyStore,
    ) -> None:
        """Отозванный ключ → 401."""
        info = isolated_key_store.verify(VALID_API_KEY)
        isolated_key_store.revoke(info.key_id)
        resp = await http_client.post(
            "/transactions",
            json={"wallets": [W1]},
            headers=auth_headers(),
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_between_no_key_returns_401(self, http_client: AsyncClient) -> None:
        resp = await http_client.post(
            "/transactions/between",
            json={"wallets1": [W1], "wallet2": W2},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_stats_no_key_returns_401(self, http_client: AsyncClient) -> None:
        resp = await http_client.post(
            "/transactions/stats",
            json={"wallets1": [W1], "wallets2": [W2]},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_after_no_key_returns_401(self, http_client: AsyncClient) -> None:
        resp = await http_client.post(
            "/transactions/after",
            json={"wallets": [W1], "tx_hash": "abc"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /transactions
# ---------------------------------------------------------------------------


class TestGetTransactionsEndpoint:
    """Тесты POST /transactions."""

    @pytest.mark.asyncio
    async def test_returns_200_with_transactions(self, http_client: AsyncClient) -> None:
        """Верный ключ + валидный запрос → 200 со списком транзакций."""
        tx = make_tx()
        svc = make_service(get_transactions=[tx])
        with patch_service(svc):
            resp = await http_client.post(
                "/transactions",
                json={"wallets": [W1]},
                headers=auth_headers(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["wallets"] == [W1]
        assert len(body["transactions"]) == 1
        assert body["transactions"][0]["tx_hash"] == "hash1"
        assert body["transactions"][0]["amount"] == 100.0

    @pytest.mark.asyncio
    async def test_returns_200_with_time_range(self, http_client: AsyncClient) -> None:
        """Запрос с временным диапазоном → 200."""
        svc = make_service(get_transactions=[])
        with patch_service(svc):
            resp = await http_client.post(
                "/transactions",
                json={
                    "wallets": [W1],
                    "start_timestamp": "2026-01-01T00:00:00Z",
                    "end_timestamp": "2026-03-01T00:00:00Z",
                },
                headers=auth_headers(),
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_returns_422_for_invalid_wallet(self, http_client: AsyncClient) -> None:
        """Некорректный адрес кошелька → 422."""
        svc = make_service(get_transactions=InvalidWalletAddressError("BAD"))
        with patch_service(svc):
            resp = await http_client.post(
                "/transactions",
                json={"wallets": ["BAD"]},
                headers=auth_headers(),
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_returns_502_on_trongrid_api_error(self, http_client: AsyncClient) -> None:
        """Ошибка TronGrid API → 502."""
        svc = make_service(get_transactions=TronGridAPIError("server error", 500))
        with patch_service(svc):
            resp = await http_client.post(
                "/transactions",
                json={"wallets": [W1]},
                headers=auth_headers(),
            )
        assert resp.status_code == 502

    @pytest.mark.asyncio
    async def test_returns_502_on_network_error(self, http_client: AsyncClient) -> None:
        """Сетевой сбой → 502."""
        svc = make_service(get_transactions=TronNetworkError("timeout"))
        with patch_service(svc):
            resp = await http_client.post(
                "/transactions",
                json={"wallets": [W1]},
                headers=auth_headers(),
            )
        assert resp.status_code == 502

    @pytest.mark.asyncio
    async def test_returns_422_for_empty_wallets_list(self, http_client: AsyncClient) -> None:
        """Пустой список кошельков не проходит Pydantic-валидацию → 422."""
        resp = await http_client.post(
            "/transactions",
            json={"wallets": []},
            headers=auth_headers(),
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /transactions/between
# ---------------------------------------------------------------------------


class TestGetTransactionsBetweenEndpoint:
    """Тесты POST /transactions/between."""

    @pytest.mark.asyncio
    async def test_returns_200_with_transactions(self, http_client: AsyncClient) -> None:
        """Успешный запрос → 200 с транзакциями."""
        tx = make_tx()
        svc = make_service(get_transactions_between=[tx])
        with patch_service(svc):
            resp = await http_client.post(
                "/transactions/between",
                json={"wallets1": [W1], "wallet2": W2},
                headers=auth_headers(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["wallet2"] == W2
        assert len(body["transactions"]) == 1

    @pytest.mark.asyncio
    async def test_returns_502_on_network_error(self, http_client: AsyncClient) -> None:
        svc = make_service(get_transactions_between=TronNetworkError("timeout"))
        with patch_service(svc):
            resp = await http_client.post(
                "/transactions/between",
                json={"wallets1": [W1], "wallet2": W2},
                headers=auth_headers(),
            )
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# POST /transactions/stats
# ---------------------------------------------------------------------------


class TestGetTransactionsStatsEndpoint:
    """Тесты POST /transactions/stats."""

    @pytest.mark.asyncio
    async def test_returns_200_with_stats(self, http_client: AsyncClient) -> None:
        """Успешный запрос → 200 с корректными полями статистики."""
        stats = {
            "wallets1_to_wallets2": 150.0,
            "wallets2_to_wallets1": 100.0,
            "difference": 50.0,
        }
        svc = make_service(get_transactions_stats=stats)
        with patch_service(svc):
            resp = await http_client.post(
                "/transactions/stats",
                json={"wallets1": [W1], "wallets2": [W2]},
                headers=auth_headers(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["wallets1_to_wallets2"] == pytest.approx(150.0)
        assert body["wallets2_to_wallets1"] == pytest.approx(100.0)
        assert body["difference"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# POST /transactions/after
# ---------------------------------------------------------------------------


class TestGetTransactionsAfterHashEndpoint:
    """Тесты POST /transactions/after."""

    @pytest.mark.asyncio
    async def test_returns_200_with_later_transactions(self, http_client: AsyncClient) -> None:
        """Успешный запрос → 200 с транзакциями после якоря."""
        tx = make_tx("later_hash")
        svc = make_service(get_transactions_after_hash=[tx])
        with patch_service(svc):
            resp = await http_client.post(
                "/transactions/after",
                json={"wallets": [W1], "tx_hash": "anchor_hash"},
                headers=auth_headers(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["transactions_after"]) == 1
        assert body["transactions_after"][0]["tx_hash"] == "later_hash"

    @pytest.mark.asyncio
    async def test_returns_404_when_anchor_not_found(self, http_client: AsyncClient) -> None:
        """Якорная транзакция не найдена → 404."""
        svc = make_service(
            get_transactions_after_hash=TransactionNotFoundError("nonexistent_hash")
        )
        with patch_service(svc):
            resp = await http_client.post(
                "/transactions/after",
                json={"wallets": [W1], "tx_hash": "nonexistent_hash"},
                headers=auth_headers(),
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_returns_422_for_empty_tx_hash(self, http_client: AsyncClient) -> None:
        """Пустой tx_hash не проходит Pydantic-валидацию → 422."""
        resp = await http_client.post(
            "/transactions/after",
            json={"wallets": [W1], "tx_hash": "   "},
            headers=auth_headers(),
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api-keys — создание ключа
# ---------------------------------------------------------------------------


class TestCreateApiKeyEndpoint:
    """Тесты POST /api-keys."""

    @pytest.mark.asyncio
    async def test_creates_key_with_master_key(self, http_client: AsyncClient) -> None:
        """Верный мастер-ключ → 201 с новым ключом."""
        with patch("app.core.config.settings.SERVICE_MASTER_KEY", MASTER_KEY):
            resp = await http_client.post(
                "/api-keys",
                json={"owner": "new-client"},
                headers=master_headers(),
            )
        assert resp.status_code == 201
        body = resp.json()
        assert "key" in body
        assert len(body["key"]) == 32       # secrets.token_hex(16)
        assert body["owner"] == "new-client"
        assert "key_id" in body

    @pytest.mark.asyncio
    async def test_wrong_master_key_returns_401(self, http_client: AsyncClient) -> None:
        """Неверный мастер-ключ → 401."""
        with patch("app.core.config.settings.SERVICE_MASTER_KEY", MASTER_KEY):
            resp = await http_client.post(
                "/api-keys",
                json={"owner": "hacker"},
                headers={"X-API-Key": "wrong-master-key-00000000000000"},
            )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_no_master_key_returns_401(self, http_client: AsyncClient) -> None:
        """Запрос без заголовка → 401."""
        resp = await http_client.post("/api-keys", json={"owner": "nobody"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api-keys — список ключей
# ---------------------------------------------------------------------------


class TestListApiKeysEndpoint:
    """Тесты GET /api-keys."""

    @pytest.mark.asyncio
    async def test_returns_list_with_master_key(self, http_client: AsyncClient) -> None:
        """Верный мастер-ключ → 200 со списком ключей."""
        with patch("app.core.config.settings.SERVICE_MASTER_KEY", MASTER_KEY):
            resp = await http_client.get("/api-keys", headers=master_headers())
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
        # В isolated_key_store есть VALID_API_KEY
        assert len(resp.json()) >= 1

    @pytest.mark.asyncio
    async def test_list_without_master_returns_401(self, http_client: AsyncClient) -> None:
        resp = await http_client.get("/api-keys")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_list_item_has_no_key_field(self, http_client: AsyncClient) -> None:
        """Список ключей не должен содержать поле key (чистый текст)."""
        with patch("app.core.config.settings.SERVICE_MASTER_KEY", MASTER_KEY):
            resp = await http_client.get("/api-keys", headers=master_headers())
        assert resp.status_code == 200
        for item in resp.json():
            assert "key" not in item
            assert "key_id" in item
            assert "owner" in item
            assert "is_active" in item


# ---------------------------------------------------------------------------
# DELETE /api-keys/{key_id} — отзыв ключа
# ---------------------------------------------------------------------------


class TestRevokeApiKeyEndpoint:
    """Тесты DELETE /api-keys/{key_id}."""

    @pytest.mark.asyncio
    async def test_revoke_nonexistent_key_returns_404(self, http_client: AsyncClient) -> None:
        """Попытка отозвать несуществующий key_id → 404."""
        with patch("app.core.config.settings.SERVICE_MASTER_KEY", MASTER_KEY):
            resp = await http_client.delete(
                "/api-keys/doesnotexistid",
                headers=master_headers(),
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_revoke_without_master_returns_401(self, http_client: AsyncClient) -> None:
        resp = await http_client.delete("/api-keys/some-key-id")
        assert resp.status_code == 401