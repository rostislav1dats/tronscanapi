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

Форматы дат (принимаются все):
    2026-01-15 14:30        — человекочитаемый (YYYY-MM-DD HH:MM)
    2026-01-15T14:30:00Z    — ISO 8601 UTC
    2026-01-15T14:30:00+03:00 — ISO 8601 с часовым поясом

Часовой пояс:
    timezone: 3    → UTC+3
    timezone: -5   → UTC-5
    timezone: 0    → UTC (по умолчанию)
    Если указан timezone, даты start_timestamp/end_timestamp
    интерпретируются как локальное время в этом поясе и конвертируются в UTC.
"""
from datetime import datetime, timezone, timedelta
from pydantic import BaseModel, Field, field_validator, model_validator

# Форматы дат, которые мы умеем парсить
_DATETIME_FORMATS = [
    "%Y-%m-%d %H:%M",       # 2026-01-15 14:30
    "%Y-%m-%d %H:%M:%S",    # 2026-01-15 14:30:00
    "%Y-%m-%dT%H:%M:%SZ",   # ISO 8601 UTC
    "%Y-%m-%dT%H:%M:%S",    # ISO 8601 без TZ
    "%Y-%m-%d",             # только дата
]


def _parse_datetime(value: object) -> datetime | None:
    """
    Парсит строку даты в naive datetime объект.

    Принимает:
        - datetime объект (возвращает как есть, но без tzinfo)
        - None (возвращает None)
        - строку в одном из форматов _DATETIME_FORMATS
        - строку ISO 8601 с часовым поясом (fromisoformat)

    Возвращает naive datetime — часовой пояс применяется позже
    через поле timezone в apply_tz().
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if not isinstance(value, str):
        raise ValueError(f"Ожидается строка с датой, получено: {type(value)}")

    value = value.strip()

    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None)
    except ValueError:
        pass

    raise ValueError(
        f"Неверный формат даты: '{value}'. "
        "Принимаются форматы: 'YYYY-MM-DD HH:MM', 'YYYY-MM-DD HH:MM:SS', "
        "'YYYY-MM-DDTHH:MM:SSZ', 'YYYY-MM-DD'"
    )


def _apply_timezone(dt: datetime | None, tz_offset: int) -> datetime | None:
    """
    Применяет смещение часового пояса к naive datetime и конвертирует в UTC.

    Args:
        dt: Naive datetime (без tzinfo), интерпретируется как локальное время.
        tz_offset: Смещение в часах относительно UTC (например, 3 -> UTC+3).

    Returns:
        datetime с tzinfo=UTC, или None если dt is None.
    """
    if dt is None:
        return None
    local_tz = timezone(timedelta(hours=tz_offset))
    return dt.replace(tzinfo=local_tz).astimezone(timezone.utc)


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
            datetime: lambda v: v.strftime("%Y-%m-%d %H:%M")
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
        description=(
            "Начало временного диапазона. Если не указан — с самой первой транзакции.\n\n"
            "Форматы: `YYYY-MM-DD HH:MM`, `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM:SSZ`.\n\n"
            "При указании `timezone` интерпретируется как локальное время в этом поясе."
        ),
        examples=["2026-01-01 00:00", "2026-01-01T00:00:00Z"],
    )
    end_timestamp: datetime | None = Field(
        default=None,
        description=(
            "Конец временного диапазона. Если не указан — по последнюю транзакцию.\n\n"
            "Форматы: `YYYY-MM-DD HH:MM`, `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM:SSZ`.\n\n"
            "При указании `timezone` интерпретируется как локальное время в этом поясе."
        ),
        examples=["2026-03-01 23:59", "2026-03-01T00:00:00Z"],
    )
    timezone: int = Field(
        default=0,
        ge=-14,
        le=14,
        description=(
            "Смещение часового пояса от UTC в часах.\n\n"
            "Примеры: `3` → UTC+3 (Москва), `-5` → UTC-5, `0` → UTC (по умолчанию).\n\n"
            "Применяется к `start_timestamp` и `end_timestamp`."
        ),
        examples=[3, -5, 0],
    )

    @field_validator("wallets")
    @classmethod
    def wallets_not_empty_strings(cls, v: list[str]) -> list[str]:
        """Проверяет, что ни один адрес кошелька не является пустой строкой."""
        for addr in v:
            if not addr.strip():
                raise ValueError("Адрес кошелька не может быть пустой строкой")
        return [a.strip() for a in v]

    @field_validator("start_timestamp", "end_timestamp", mode="before")
    @classmethod
    def parse_dt(cls, v: object) -> datetime | None:
        return _parse_datetime(v)

    @model_validator(mode="after")
    def apply_tz(self) -> "GetTransactionsRequest":
        tz = self.timezone
        self.start_timestamp = _apply_timezone(self.start_timestamp, tz)
        self.end_timestamp = _apply_timezone(self.end_timestamp, tz)
        return self


class GetTransactionsResponse(BaseModel):
    wallets: list[str] = Field(description="Список запрошенных кошельков")
    transactions: list[TransactionDTO] = Field(description="Транзакции отсортированные от старой к новой")


class GetTransactionsBetweenRequest(BaseModel):
    """
    Запрос на получение транзакций между группой wallets1 и одним wallet2.

    Возвращаются переводы в обоих направлениях:
    - wallets1 -> wallet2
    - wallet2 -> wallets1
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
    start_timestamp: datetime | None = Field(
        default=None,
        description=(
            "Начало временного диапазона.\n\n"
            "Форматы: `YYYY-MM-DD HH:MM`, `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM:SSZ`.\n\n"
            "При указании `timezone` интерпретируется как локальное время в этом поясе."
        ),
        examples=["2026-01-01 00:00"],
    )
    end_timestamp: datetime | None = Field(
        default=None,
        description=(
            "Конец временного диапазона.\n\n"
            "Форматы: `YYYY-MM-DD HH:MM`, `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM:SSZ`.\n\n"
            "При указании `timezone` интерпретируется как локальное время в этом поясе."
        ),
        examples=["2026-03-01 23:59"],
    )
    timezone: int = Field(
        default=0,
        ge=-14,
        le=14,
        description="Смещение часового пояса от UTC в часах. Пример: `3` → UTC+3.",
        examples=[3, 0],
    )

    @field_validator("wallets1")
    @classmethod
    def wallets1_not_empty(cls, v: list[str]) -> list[str]:
        for addr in v:
            if not addr.strip():
                raise ValueError("Адрес кошелька не может быть пустой строкой")
        return [a.strip() for a in v]

    @field_validator("start_timestamp", "end_timestamp", mode="before")
    @classmethod
    def parse_dt(cls, v: object) -> datetime | None:
        return _parse_datetime(v)

    @model_validator(mode="after")
    def apply_tz(self) -> "GetTransactionsBetweenRequest":
        tz = self.timezone
        self.start_timestamp = _apply_timezone(self.start_timestamp, tz)
        self.end_timestamp = _apply_timezone(self.end_timestamp, tz)
        return self


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
    start_timestamp: datetime | None = Field(
        default=None,
        description=(
            "Начало временного диапазона.\n\n"
            "Форматы: `YYYY-MM-DD HH:MM`, `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM:SSZ`.\n\n"
            "При указании `timezone` интерпретируется как локальное время в этом поясе."
        ),
        examples=["2026-01-01 00:00"],
    )
    end_timestamp: datetime | None = Field(
        default=None,
        description=(
            "Конец временного диапазона.\n\n"
            "Форматы: `YYYY-MM-DD HH:MM`, `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM:SSZ`.\n\n"
            "При указании `timezone` интерпретируется как локальное время в этом поясе."
        ),
        examples=["2026-03-01 23:59"],
    )
    timezone: int = Field(
        default=0,
        ge=-14,
        le=14,
        description="Смещение часового пояса от UTC в часах. Пример: `3` → UTC+3.",
        examples=[3, 0],
    )

    @field_validator("start_timestamp", "end_timestamp", mode="before")
    @classmethod
    def parse_dt(cls, v: object) -> datetime | None:
        return _parse_datetime(v)

    @model_validator(mode="after")
    def apply_tz(self) -> "GetTransactionsStatsRequest":
        tz = self.timezone
        self.start_timestamp = _apply_timezone(self.start_timestamp, tz)
        self.end_timestamp = _apply_timezone(self.end_timestamp, tz)
        return self


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

    Опциональные start_timestamp/end_timestamp позволяют дополнительно
    ограничить результат по времени (в дополнение к якорю).
    """

    wallets: list[str] = Field(
        min_length=1,
        description="Один или несколько кошельков для поиска",
    )
    tx_hash: str = Field(
        description="Хеш транзакции-якоря. После неё будут возвращены все более поздние транзакции.",
        examples=["abcd1234efgh5678..."],
    )
    start_timestamp: datetime | None = Field(
        default=None,
        description=(
            "Дополнительная нижняя граница по времени (не заменяет якорь — применяется поверх него).\n\n"
            "Форматы: `YYYY-MM-DD HH:MM`, `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM:SSZ`.\n\n"
            "При указании `timezone` интерпретируется как локальное время в этом поясе."
        ),
        examples=["2026-01-01 00:00"],
    )
    end_timestamp: datetime | None = Field(
        default=None,
        description=(
            "Верхняя граница по времени.\n\n"
            "Форматы: `YYYY-MM-DD HH:MM`, `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM:SSZ`.\n\n"
            "При указании `timezone` интерпретируется как локальное время в этом поясе."
        ),
        examples=["2026-03-01 23:59"],
    )
    timezone: int = Field(
        default=0,
        ge=-14,
        le=14,
        description="Смещение часового пояса от UTC в часах. Пример: `3` → UTC+3.",
        examples=[3, 0],
    )

    @field_validator("tx_hash")
    @classmethod
    def tx_hash_not_empty(cls, v: str) -> str:
        """Проверяет, что хеш транзакции не пустой."""
        if not v.strip():
            raise ValueError("tx_hash не может быть пустой строкой")
        return v.strip()

    @field_validator("wallets")
    @classmethod
    def wallets_not_empty_strings(cls, v: list[str]) -> list[str]:
        for addr in v:
            if not addr.strip():
                raise ValueError("Адрес кошелька не может быть пустой строкой")
        return [a.strip() for a in v]

    @field_validator("start_timestamp", "end_timestamp", mode="before")
    @classmethod
    def parse_dt(cls, v: object) -> datetime | None:
        return _parse_datetime(v)

    @model_validator(mode="after")
    def apply_tz(self) -> "GetTransactionsAfterHashRequest":
        tz = self.timezone
        self.start_timestamp = _apply_timezone(self.start_timestamp, tz)
        self.end_timestamp = _apply_timezone(self.end_timestamp, tz)
        return self


class GetTransactionsAfterHashResponse(BaseModel):
    """Ответ с транзакциями после указанного хеша."""
    wallets: list[str]
    transactions_after: list[TransactionDTO] = Field(description="Транзакции после якорной от старой к новой.")


class GetTransactionsRawRequest(BaseModel):
    """
    Запрос на получение транзакций кошелька без построения цепочки.

    В отличие от /transactions, этот эндпоинт возвращает только транзакции
    указанных кошельков — без перехода на кошельки-получатели.
    """
    wallets: list[str] = Field(
        min_length=1,
        description="Один или несколько адресов кошельков TRON",
        examples=[["TXytKFZbjHY6fCDhCGnUR5rEeKHrPwTFJZ"]],
    )
    start_timestamp: datetime | None = Field(
        default=None,
        description=(
            "Начало временного диапазона.\n\n"
            "Форматы: `YYYY-MM-DD HH:MM`, `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM:SSZ`.\n\n"
            "При указании `timezone` интерпретируется как локальное время в этом поясе."
        ),
        examples=["2026-01-01 00:00"],
    )
    end_timestamp: datetime | None = Field(
        default=None,
        description=(
            "Конец временного диапазона.\n\n"
            "Форматы: `YYYY-MM-DD HH:MM`, `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM:SSZ`.\n\n"
            "При указании `timezone` интерпретируется как локальное время в этом поясе."
        ),
        examples=["2026-03-01 23:59"],
    )
    timezone: int = Field(
        default=0,
        ge=-14,
        le=14,
        description="Смещение часового пояса от UTC в часах. Пример: `3` → UTC+3.",
        examples=[3, 0],
    )

    @field_validator("wallets")
    @classmethod
    def wallets_not_empty_strings(cls, v: list[str]) -> list[str]:
        for addr in v:
            if not addr.strip():
                raise ValueError("Адрес кошелька не может быть пустой строкой")
        return [a.strip() for a in v]

    @field_validator("start_timestamp", "end_timestamp", mode="before")
    @classmethod
    def parse_dt(cls, v: object) -> datetime | None:
        return _parse_datetime(v)

    @model_validator(mode="after")
    def apply_tz(self) -> "GetTransactionsRawRequest":
        tz = self.timezone
        self.start_timestamp = _apply_timezone(self.start_timestamp, tz)
        self.end_timestamp = _apply_timezone(self.end_timestamp, tz)
        return self


class GetTransactionsRawResponse(BaseModel):
    """Ответ с транзакциями без цепочки."""
    wallets: list[str] = Field(description="Список запрошенных кошельков")
    transactions: list[TransactionDTO] = Field(
        description="Транзакции, отсортированные от старой к новой"
    )