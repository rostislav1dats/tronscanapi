"""
Юнит-тесты для app/utils/tron.py

Покрывают:
- validate_tron_address: корректные и некорректные адреса (длина, префикс, алфавит)
- parse_trc20_transaction: нормальный путь, типы sent/received, конвертация суммы,
  разбор timestamp, фильтрация не-USDT токенов, битые данные
"""

import pytest

from app.core.exceptions import InvalidWalletAddressError
from app.utils.tron import parse_trc20_transaction, validate_tron_address

# ---------------------------------------------------------------------------
# Тестовые константы
# ---------------------------------------------------------------------------

VALID_WALLET_1 = "TXytKFZbjHY6fCDhCGnUR5rEeKHrPwTFJZ"  # 34 символа, начинается с T
VALID_WALLET_2 = "TYytKFZbjHY6fCDhCGnUR5rEeKHrPwTFJY"
USDT_CONTRACT  = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

# Тестовая raw-транзакция от TronGrid
RAW_USDT_TX = {
    "transaction_id": "abc123hash456",
    "block_timestamp": 1773059696000,   # 2026-03-09T12:34:56 UTC
    "from": VALID_WALLET_1,
    "to": VALID_WALLET_2,
    "value": "100000000",               # 100 USDT при decimals=6 → 100.0
    "token_info": {
        "symbol": "USDT",
        "decimals": 6,
        "address": USDT_CONTRACT,
    },
}


# ---------------------------------------------------------------------------
# validate_tron_address
# ---------------------------------------------------------------------------


class TestValidateTronAddress:
    """Тесты функции validate_tron_address."""

    def test_valid_address_does_not_raise(self) -> None:
        """Корректный адрес не должен бросать исключение."""
        validate_tron_address(VALID_WALLET_1)  # no exception

    def test_valid_usdt_contract_passes(self) -> None:
        """Адрес контракта USDT является валидным TRON-адресом."""
        validate_tron_address(USDT_CONTRACT)

    def test_short_address_raises(self) -> None:
        """Адрес короче 34 символов должен быть отклонён."""
        with pytest.raises(InvalidWalletAddressError):
            validate_tron_address("TSHORT")

    def test_long_address_raises(self) -> None:
        """Адрес длиннее 34 символов должен быть отклонён."""
        with pytest.raises(InvalidWalletAddressError):
            validate_tron_address("T" + "X" * 34)  # 35 символов

    def test_wrong_prefix_raises(self) -> None:
        """Адрес, не начинающийся на 'T', должен быть отклонён."""
        wrong = "AXytKFZbjHY6fCDhCGnUR5rEeKHrPwTFJZ"
        with pytest.raises(InvalidWalletAddressError) as exc_info:
            validate_tron_address(wrong)
        assert exc_info.value.address == wrong

    def test_invalid_base58_char_zero_raises(self) -> None:
        """Символ '0' (ноль) не входит в Base58 и должен быть отклонён."""
        invalid = "T0ytKFZbjHY6fCDhCGnUR5rEeKHrPwTFJZ"
        with pytest.raises(InvalidWalletAddressError):
            validate_tron_address(invalid)

    def test_invalid_base58_char_O_raises(self) -> None:
        """Символ 'O' (заглавная буква О) не входит в Base58."""
        invalid = "TOytKFZbjHY6fCDhCGnUR5rEeKHrPwTFJZ"
        with pytest.raises(InvalidWalletAddressError):
            validate_tron_address(invalid)

    def test_empty_string_raises(self) -> None:
        """Пустая строка должна быть отклонена."""
        with pytest.raises(InvalidWalletAddressError):
            validate_tron_address("")

    def test_exception_carries_original_address(self) -> None:
        """Исключение должно содержать исходную строку адреса."""
        bad = "TSHORT"
        with pytest.raises(InvalidWalletAddressError) as exc_info:
            validate_tron_address(bad)
        assert exc_info.value.address == bad


# ---------------------------------------------------------------------------
# parse_trc20_transaction
# ---------------------------------------------------------------------------


class TestParseTrc20Transaction:
    """Тесты функции parse_trc20_transaction."""

    def test_parses_sent_transaction(self) -> None:
        """Транзакция, отправленная с wallet, имеет type='sent'."""
        tx = parse_trc20_transaction(RAW_USDT_TX, wallet=VALID_WALLET_1)
        assert tx is not None
        assert tx.type == "sent"
        assert tx.from_wallet == VALID_WALLET_1
        assert tx.to_wallet == VALID_WALLET_2
        assert tx.tx_hash == "abc123hash456"

    def test_parses_received_transaction(self) -> None:
        """Транзакция на wallet имеет type='received'."""
        tx = parse_trc20_transaction(RAW_USDT_TX, wallet=VALID_WALLET_2)
        assert tx is not None
        assert tx.type == "received"

    def test_amount_conversion_6_decimals(self) -> None:
        """100_000_000 при decimals=6 → 100.0 USDT."""
        tx = parse_trc20_transaction(RAW_USDT_TX, wallet=VALID_WALLET_1)
        assert tx is not None
        assert tx.amount == pytest.approx(100.0)

    def test_amount_conversion_custom_decimals(self) -> None:
        """Произвольные decimals должны корректно учитываться."""
        raw = {
            **RAW_USDT_TX,
            "value": "1000000000000",            # 1e12
            "token_info": {"symbol": "USDT", "decimals": 12},
        }
        tx = parse_trc20_transaction(raw, wallet=VALID_WALLET_1)
        assert tx is not None
        assert tx.amount == pytest.approx(1.0)

    def test_timestamp_converted_to_utc_datetime(self) -> None:
        """block_timestamp в миллисекундах должен преобразоваться в datetime UTC."""
        tx = parse_trc20_transaction(RAW_USDT_TX, wallet=VALID_WALLET_1)
        assert tx is not None
        assert tx.timestamp.year == 2026
        assert tx.timestamp.month == 3
        assert tx.timestamp.day == 9
        assert tx.timestamp.hour == 12
        assert tx.timestamp.tzinfo is not None   # aware datetime

    def test_non_usdt_token_returns_none(self) -> None:
        """Транзакция не-USDT токена должна возвращать None."""
        raw = {**RAW_USDT_TX, "token_info": {"symbol": "TRX", "decimals": 6}}
        assert parse_trc20_transaction(raw, wallet=VALID_WALLET_1) is None

    def test_missing_token_info_returns_none(self) -> None:
        """Отсутствие symbol в token_info → None (не является USDT)."""
        raw = {**RAW_USDT_TX, "token_info": {}}
        assert parse_trc20_transaction(raw, wallet=VALID_WALLET_1) is None

    def test_non_numeric_value_returns_none(self) -> None:
        """Нечисловое значение value не должно приводить к исключению → None."""
        raw = {**RAW_USDT_TX, "value": "not_a_number"}
        assert parse_trc20_transaction(raw, wallet=VALID_WALLET_1) is None

    def test_empty_raw_dict_returns_none(self) -> None:
        """Пустой словарь → None (нет symbol USDT)."""
        assert parse_trc20_transaction({}, wallet=VALID_WALLET_1) is None

    def test_case_insensitive_wallet_comparison(self) -> None:
        """Сравнение кошельков при определении типа должно быть регистронезависимым."""
        tx = parse_trc20_transaction(RAW_USDT_TX, wallet=VALID_WALLET_1.lower())
        assert tx is not None
        assert tx.type == "sent"