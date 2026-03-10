"""
Pydantic-модели запросов и ответов API.

Все модели строго типизированы. Описания полей (Field description)
автоматически попадают в OpenAPI-схему и отображаются в Swagger UI.

Структура:
    TransactionDTO              — единица данных одной транзакции
    GetTransactionsRequest/Response         — 3.1 /transactions
    GetTransactionsBetweenRequest/Response  — 3.2 /transactions/between
    GetTransactionsStatsRequest/Response    — 3.3 /transactions/stats
    GetTransactionsAfterHashRequest/Response — 3.4 /transactions/after
"""
from datetime import datetime
from pydantic import BaseModel, Field, field_validator

class TransactionDTO(BaseModel):
    """
    Одна USDT (TRC20) транзакция.

    Возвращается во всех ответах сервиса как элемент списка.
    Тип транзакции (sent/received) определяется относительно
    запрошенного кошелька — не является абсолютным свойством транзакции.
    """
    timestamp: datetime = Field(description="Время транзакции в формате ISO 8601 UTC")
    amount: float = Field(description="Сумма перевода в USDT", ge=0)
    type: str = Field(description='"sent" - исходящая транзакция, "received" - входящая')
    from_wallet: str = Field(description="TRON-адрес отправителя (base58)")
    to_wallet: str = Field(description="TRON-адрес получателя (base58)")
    tx_hash: str = Field(description="Уникальный хеш транзакции в блокчейне")

    model_config = {
        "json_encoders": {
            datetime: lambda v: v.strftime("%Y-%m-%dT%H:%M:%SZ")
        }
    }

class GetTransactionsRequest(BaseModel):
    """
    Запрос на получение всех транзакций по одному или нескольким кошелькам.

    Если start_timestamp и end_timestamp не указаны — возвращаются
    все транзакции за всё время (пагинация обрабатывается автоматически).
    """
    wallets: list[str] = Field(
        min_length=1,
        description="Один или несколько адресов TRON-кошельков",
        examples=[["TXytKFZbjHY6fCDhCGnUR5rEeKHrPwTFJZ", "TYytKFZbjHY6fCDhCGnUR5rEeKHrPwTFJY"]],
    )
    start_timestamp: datetime | None = Field(
        default=None,
        description="Начало временного диапазона (UTC). Если не указан — с самой первой транзакции.",
        examples=["2026-01-01T00:00:00Z"],
    )
    end_timestamp: datetime | None = Field(
        default=None,
        description="Конец временного диапазона (UTC). Если не указан — по последнюю транзакцию.",
        examples=["2026-03-01T00:00:00Z"],
    )

    @field_validator("wallets")
    @classmethod
    def wallets_not_empty_strings(cls, v: list[str]) -> list[str]:
        """Проверяет, что ни один адрес кошелька не является пустой строкой."""
        for addr in v:
            if not addr.strip():
                raise ValueError("Адрес кошелька не может быть пустой строкой")
        return [a.strip() for a in v]
    
class GetTransactionsResponse(BaseModel):
    wallets: list[str] = Field(description="Список запрошенных кошельков")
    transactions: list[TransactionDTO] = Field(description="Транзакции отсортированные от старой к новой")

class GetTransactionsBetweenRequest(BaseModel):
    """
    Запрос на получение транзакций между группой wallets1 и одним wallet2.

    Возвращаются переводы в обоих направлениях:
    - wallets1 → wallet2
    - wallet2 → wallets1
    """
    wallets1: list[str] = Field(
        min_length=1,
        description="Один или несколько кошельков первой стороны",
        examples=[["TXytKFZbjHY6fCDhCGnUR5rEeKHrPwTFJZ"]],
    )
    wallet2: str = Field(
        description="Кошелёк второй стороны",
        examples=["TYytKFZbjHY6fCDhCGnUR5rEeKHrPwTFJY"],
    )
    start_timestamp: datetime | None = Field(default=None)
    end_timestamp: datetime | None = Field(default=None)

    @field_validator("wallets1")
    @classmethod
    def wallets1_not_empty(clas, v: list[str]) -> list[str]:
        for addr in v:
            if not addr.strip():
                raise ValueError("Адрес кошелька не может быть пустой строкой")
        return [a.strip() for a in v]
    
class GetTransactionsBetweenResponse(BaseModel):
    wallets1: list[str]
    wallet2: str
    transactions: list[TransactionDTO] = Field(description="Транзакции в обоих направлениях от старой к новой")

class GetTransactionsStatsRequest(BaseModel):
    """
    Запрос на агрегированную статистику переводов между двумя группами кошельков.

    Считает суммарный объём в каждом направлении и разницу.
    """
    wallets1: list[str] = Field(min_length=1, description="Первая группа кошельков")
    wallets2: list[str] = Field(min_length=1, description="Вторая группа кошельков")
    start_timestamp: datetime | None = Field(default=None)
    end_timestamp: datetime | None = Field(default=None)

class GetTransactionsStatsResponse(BaseModel):
    """Агрегированная статистика транзакций между двумя группами."""

    wallets1_to_wallets2: float = Field(
        description="Суммарный объём переводов из wallets1 в wallets2 (USDT)"
    )
    wallets2_to_wallets1: float = Field(
        description="Суммарный объём переводов из wallets2 в wallets1 (USDT)"
    )
    difference: float = Field(
        description="Разница: wallets1_to_wallets2 − wallets2_to_wallets1"
    )

class GetTransactionsAfterHashRequest(BaseModel):
    """
    Запрос на получение транзакций после указанной по хешу.

    Транзакция с переданным tx_hash служит якорем — сервис вернёт
    все транзакции кошельков с более поздним timestamp.
    """

    wallets: list[str] = Field(
        min_length=1,
        description="Один или несколько кошельков для поиска",
    )
    tx_hash: str = Field(
        description="Хеш транзакции-якоря. После неё будут возвращены все более поздние транзакции.",
        examples=["abcd1234efgh5678..."],
    )

    @field_validator("tx_hash")
    @classmethod
    def tx_hash_not_empty(cls, v: str) -> str:
        """Проверяет, что хеш транзакции не пустой."""
        if not v.strip():
            raise ValueError("tx_hash не может быть пустой строкой")
        return v.strip()
    
class GetTransactionsAfterHashResponse(BaseModel):
    """ответ с транзакциями после указанного хеша."""
    wallets: list[str]
    transactions_after: list[TransactionDTO] = Field(description="Транзакции после якорной от старой к новой.")
