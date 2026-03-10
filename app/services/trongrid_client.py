"""
HTTP-клиент для TronGrid API.

Отвечает исключительно за сетевое взаимодействие:
- Постраничную загрузку TRC20-транзакций (cursor-based pagination через fingerprint)
- Поиск транзакции по хешу
- Валидацию HTTP-ответов и проброс доменных исключений

Бизнес-логика (фильтрация, дедупликация, сортировка) намеренно
вынесена в слой сервисов (services/transaction_service.py).

Использование:
    async with TronGridClient() as client:
        txs = await client.get_trc20_transactions("TXXXX...")
        tx = await client.get_transaction_by_hash("abc123...")
"""
import asyncio
import logging
from datetime import datetime
import httpx

from app.core.config import settings
from app.core.exceptions import InvalidWalletAddressError, TronGridAPIError, TronNetworkError
from app.models.schemas import TransactionDTO
from app.utils.tron import parse_trc20_transaction, validate_tron_address

logger = logging.getLogger(__name__)
_USDT_CONTRACT = settings.USDT_CONTRACT_ADDRESS

class TronGridClient:
    """
    Асинхронный HTTP-клиент TronGrid API.

    Реализован как async context manager — httpx.AsyncClient создаётся
    при входе и закрывается при выходе:

        async with TronGridClient() as client:
            txs = await client.get_trc20_transactions(wallet)

    Особенности:
    - Автоматический retry на уровне транспорта (httpx.AsyncHTTPTransport)
    - Постраничный обход через cursor fingerprint (TronGrid v1 API)
    - Заголовок TRON-PRO-API-KEY добавляется к каждому запросу
    - HTTP 400 от TronGrid интерпретируется как InvalidWalletAddressError
    - Таймаут и сетевые ошибки оборачиваются в TronNetworkError

    Attributes:
        _client: Внутренний httpx.AsyncClient (None до входа в контекст).
    """

    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "TronGridClient":
        transport = httpx.AsyncHTTPTransport(retries=settings.HTTP_MAX_RETRIES)
        self._client = httpx.AsyncClient(
            base_url=settings.TRONGRID_BASE_URL,
            headers={
                "TRON-PRO-API-KEY": settings.TRONGRID_API_KEY,
                "Accept": "application/json",
            },
            timeout=settings.HTTP_TIMEOUT_SECONDS,
            transport=transport
        )
        logger.debug("TronGridClient: соединение открыто (base_url=%s)", settings.TRONGRID_BASE_URL)
        return self
    
    async def __aexit__(self, *_: object) -> None:
        if self._client:
            await self._client.aclose()
            logger.debug("Соединение закрыто")

    async def get_trc20_transactions(self, wallet: str, start_timestamp: datetime | None = None, end_timestamp: datetime | None = None, min_timestamp_ms: int | None = None,) -> list[TransactionDTO]:
        """
        Возвращает все USDT TRC20-транзакции кошелька.

        TronGrid возвращает не более 200 записей за один запрос.
        Метод автоматически обходит все страницы через cursor fingerprint:
        пока в поле meta.fingerprint есть значение — загружается следующая страница.

        Args:
            wallet: Адрес TRON-кошелька (base58, 34 символа).
            start_timestamp: Начало временного диапазона (UTC).
                             None — без нижней границы.
            end_timestamp: Конец временного диапазона (UTC).
                           None — без верхней границы.

        Returns:
            Список TransactionDTO, отсортированных по block_timestamp ASC
            (TronGrid гарантирует порядок при order_by=block_timestamp,asc).

        Raises:
            InvalidWalletAddressError: Если адрес кошелька некорректен
                                       (HTTP 400 от TronGrid или предварительная валидация).
            TronGridAPIError: При любом другом HTTP-ответе >= 400 от TronGrid.
            TronNetworkError: При таймауте или разрыве соединения.
        """
        validate_tron_address(wallet)
        self._ensure_client()

        params: dict[str, str | int] = {
            "contract_address": _USDT_CONTRACT,
            "only_confirmed": "true",
            "limit": settings.TRONGRID_PAGE_SIZE,
            "order_by": "block_timestamp,asc",
        }

        if min_timestamp_ms is not None:
            params["min_timestamp"] = min_timestamp_ms
        elif start_timestamp:
            params["min_timestamp"] = int(start_timestamp.timestamp() * 1000)

        if end_timestamp:
            params["max_timestamp"] = int(end_timestamp.timestamp() * 1000)

        transactions: list[TransactionDTO] = []
        fingerprint: str | None = None
        page = 0

        while True:
            page += 1

            if page > 1 and settings.TRONGRID_PAGE_DELAY > 0:
                await asyncio.sleep(settings.TRONGRID_PAGE_DELAY)

            if fingerprint:
                params["fingerprint"] = fingerprint

            logger.debug(
                "TronGrid: страница %d кошелька %s (fingerprint=%s)", page, wallet, fingerprint
            )

            try:
                response = await self._client.get(  # type: ignore[union-attr]
                    f"/v1/accounts/{wallet}/transactions/trc20",
                    params=params,
                )
            except httpx.TimeoutException as exc:
                logger.error("TronGrid: таймаут при запросе кошелька %s: %s", wallet, exc)
                raise TronNetworkError(
                    f"Таймаут при запросе транзакций кошелька {wallet}"
                ) from exc
            except httpx.RequestError as exc:
                logger.error("TronGrid: сетевая ошибка кошелька %s: %s", wallet, exc)
                raise TronNetworkError(f"Сетевая ошибка: {exc}") from exc

            # HTTP 400 от TronGrid = некорректный адрес кошелька
            if response.status_code == 400:
                logger.warning("TronGrid: 400 Bad Request для кошелька %s", wallet)
                raise InvalidWalletAddressError(wallet)

            if not response.is_success:
                raise TronGridAPIError(
                    response.text, status_code=response.status_code
                )
            
            data = response.json()
            raw_txs: list[str] = data.get("data", [])

            if page == 1 and raw_txs:
                logger.debug("TronGrid raw[0] для кошелька %s: %s", wallet, raw_txs[0])

            for raw in raw_txs:
                tx = parse_trc20_transaction(raw, wallet)
                if tx is not None:
                    transactions.append(tx)

            meta = data.get("meta", {})
            fingerprint = meta.get("fingerprint")

            if not fingerprint or len(raw_txs) < settings.TRONGRID_PAGE_SIZE:
                break

        logger.info("TronGrid: загружено %d транзакция для %s (%d страниц)", len(transactions), wallet, page)
        return transactions

    async def get_transaction_timestamp(self, tx_hash: str) -> int | None:
        """
        Возвращает block_timestamp транзакции по хешу (в миллисекундах).

        Используется в /transactions/after для определения точки отсчёта
        без загрузки всей истории кошелька.

        Эндпоинт /v1/transactions/{hash} возвращает raw on-chain данные —
        это не TRC20-транзакция, поэтому parse_trc20_transaction не подходит.
        Нам нужен только timestamp, который есть в любой транзакции.

        Args:
            tx_hash: Хеш транзакции (64-символьная hex-строка).

        Returns:
            block_timestamp в миллисекундах, или None если не найдена.

        Raises:
            TronGridAPIError:  HTTP-ошибка от TronGrid (кроме 404).
            TronNetworkError:  Таймаут или разрыв соединения.
        """
        self._ensure_client()
        logger.debug("TronGrid: получение timestamp транзакции %s", tx_hash)

        try:
            response = await self._client.post(
                "/wallet/gettransactionbyid",
                json={"value": tx_hash}
            )
        except httpx.TimeoutException as exc:
            raise TronNetworkError(f"Таймаут при поиске транзакции {tx_hash}") from exc
        except httpx.RequestError as exc:
            raise TronNetworkError(f"Сетевая ошибка: {exc}") from exc

        if response.status_code == 404:
            logger.info("TronGrid: транзакция %s не найдена", tx_hash)
            return None

        if not response.is_success:
            raise TronGridAPIError(response.text, status_code=response.status_code)

        raw_list = response.json().get("raw_data", {})        

        ts = raw_list.get("timestamp")
        logger.debug("TronGrid: транзакция %s → timestamp=%s", tx_hash, ts)
        return int(ts) if ts is not None else None
    
    def _ensure_client(self) -> None:
        """
        Проверяет, что клиент инициализирован.

        Raises:
            RuntimeError: Если метод вызван вне async context manager.
        """
        if self._client is None:
            raise RuntimeError(
                "TronGridClient не инициализирован. "
                "Используйте его как async context manager:\n"
                "    async with TronGridClient() as client:\n"
                "        await client.get_trc20_transactions(...)"
            )
