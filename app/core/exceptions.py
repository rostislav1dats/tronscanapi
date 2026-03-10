"""
Кастомные исключения сервиса.

Иерархия:
    TronServiceError                  — базовый класс всех ошибок сервиса
    ├── InvalidWalletAddressError     — некорректный TRON-адрес
    ├── TransactionNotFoundError      — транзакция по хешу не найдена
    ├── TronGridAPIError              — HTTP-ошибка от TronGrid API
    └── TronNetworkError              — сетевой сбой (таймаут, разрыв соединения)

Все исключения наследуются от TronServiceError — это позволяет
единообразно перехватывать любые ошибки домена одним except-блоком,
если нужно, либо обрабатывать каждый вид отдельно.

Использование в роутерах:
    try:
        result = await service.get_transactions(...)
    except InvalidWalletAddressError as exc:
        raise HTTPException(422, detail=str(exc))
    except (TronGridAPIError, TronNetworkError) as exc:
        raise HTTPException(502, detail=str(exc))
"""


class TronServiceError(Exception):
    """
    Базовый класс всех бизнес-ошибок сервиса.

    Не используется напрямую — служит для группового перехвата:
        except TronServiceError as exc: ...
    """


class InvalidWalletAddressError(TronServiceError):
    """
    Некорректный адрес TRON-кошелька.

    Возникает при:
    - Длине адреса != 34 символа
    - Адрес не начинается с 'T'
    - Адрес содержит символы вне алфавита Base58

    Attributes:
        address: Исходная строка, не прошедшая валидацию.
    """

    def __init__(self, address: str) -> None:
        self.address = address
        super().__init__(
            f"Некорректный адрес TRON-кошелька: {address!r}. "
            "Адрес должен начинаться с 'T' и содержать ровно 34 символа Base58."
        )


class TransactionNotFoundError(TronServiceError):
    """
    Транзакция с указанным хешем не найдена в блокчейне.

    Attributes:
        tx_hash: Хеш транзакции, которую не удалось найти.
    """

    def __init__(self, tx_hash: str) -> None:
        self.tx_hash = tx_hash
        super().__init__(f"Транзакция не найдена: {tx_hash!r}")


class TronGridAPIError(TronServiceError):
    """
    HTTP-ошибка при обращении к TronGrid API.

    Возникает при ответах с кодом >= 400 от TronGrid,
    кроме 400 (невалидный адрес — обрабатывается как InvalidWalletAddressError).

    Attributes:
        status_code: HTTP-код ответа TronGrid (может быть None при парсинге).
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(
            f"Ошибка TronGrid API (HTTP {status_code}): {message}"
        )


class TronNetworkError(TronServiceError):
    """
    Сетевой сбой при обращении к TronGrid.

    Возникает при:
    - Таймауте запроса (httpx.TimeoutException)
    - Разрыве соединения (httpx.ConnectError)
    - Других транспортных ошибках (httpx.RequestError)
    """