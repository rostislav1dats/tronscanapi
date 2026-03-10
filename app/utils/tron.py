"""
Утилиты для работы с TRON-адресами и разбора транзакций из TronGrid API.

Содержит две публичные функции:

    validate_tron_address(address)
        Проверяет корректность TRON base58-адреса.
        Бросает InvalidWalletAddressError при несоответствии.

    parse_trc20_transaction(raw, wallet)
        Преобразует raw-словарь из ответа TronGrid в TransactionDTO.
        Возвращает None если транзакция не является USDT-переводом
        или данные повреждены.
"""
import logging
from datetime import UTC, datetime

from app.core.exceptions import InvalidWalletAddressError
from app.models.schemas import TransactionDTO

logger = logging.getLogger(__name__)

_BASE58_ALPHABET = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")

# Все TRON-адреса начинаются с 'T' и имеют длину 34 символа
_TRON_ADDRESS_PREFIX = "T"
_TRON_ADDRESS_LENGTH = 34

def validate_tron_address(address: str) -> None:
    """
    Проверяет корректность TRON-адреса формата base58check.

    Правила валидации:
    1. Длина строки ровно 34 символа
    2. Первый символ — 'T'
    3. Все символы входят в алфавит Base58 (нет 0, O, I, l)

    Примечание: функция не выполняет полную проверку контрольной суммы
    base58check — это достаточно для первичной фильтрации некорректных адресов
    без зависимости от tronpy/base58 библиотек.

    Args:
        address: Строка адреса TRON-кошелька.

    Raises:
        InvalidWalletAddressError: Если адрес не соответствует ни одному правилу.

    Examples:
        >>> validate_tron_address("TXytKFZbjHY6fCDhCGnUR5rEeKHrPwTFJZ")  # OK
        >>> validate_tron_address("TSHORT")  # InvalidWalletAddressError
    """
    if len(address) != _TRON_ADDRESS_LENGTH:
        logger.debug("Некорректная длина адреса: %d != %d (%r)", len(address), _TRON_ADDRESS_LENGTH, address)
        raise InvalidWalletAddressError(address)
    if not address.startswith(_TRON_ADDRESS_PREFIX):
        logger.debug("Адрес не начинается с 'T': %r", address)
        raise InvalidWalletAddressError(address)
    if not all(c in _BASE58_ALPHABET for c in address):
        invalid_chars = [c for c in address if c not in _BASE58_ALPHABET]
        logger.debug("Адрес содержит недопустимые символы %s: %r", invalid_chars, address)
        raise InvalidWalletAddressError(address)
    
def parse_trc20_transaction(raw: dict, wallet: str) -> TransactionDTO | None:
    """
    Преобразует raw-словарь TronGrid TRC20-транзакции в TransactionDTO.

    Алгоритм:
    1. Проверяет, что токен — USDT (по полю token_info.symbol)
    2. Извлекает поля from, to, value, transaction_id, block_timestamp
    3. Конвертирует block_timestamp (мс) → datetime UTC
    4. Конвертирует value (минимальные единицы токена) → USDT с учётом decimals
    5. Определяет тип транзакции (sent/received) относительно wallet

    Args:
        raw:    Словарь с данными транзакции из ответа TronGrid API.
                Ожидаемые ключи: from, to, value, transaction_id,
                block_timestamp, token_info {symbol, decimals}.
        wallet: Адрес кошелька, для которого определяется направление.
                Если from_wallet совпадает с wallet → type="sent",
                иначе → type="received".
                Может быть пустой строкой — тогда type всегда "received".

    Returns:
        TransactionDTO — если транзакция является USDT-переводом и данные корректны.
        None           — если:
                         - token_info.symbol != "USDT" (другой токен)
                         - данные повреждены / отсутствуют обязательные поля
                         - value не является числом

    Examples:
        raw = {
            "transaction_id": "abc123",
            "block_timestamp": 1741521296000,
            "from": "TXXXX...",
            "to": "TYYYY...",
            "value": "100000000",
            "token_info": {"symbol": "USDT", "decimals": 6}
        }
        tx = parse_trc20_transaction(raw, wallet="TXXXX...")
        # tx.amount == 100.0, tx.type == "sent"
    """
    try:
        token_info: dict = raw.get("token_info", {})
        if token_info.get("symbol", "").upper() != "USDT":
            return None
        
        symbol = token_info.get("symbol", "")
        if symbol.upper() != "USDT":
            logger.debug(
                "Пропуск токена с symbol=%r (не USDT) | tx_id=%s",
                symbol, raw.get("transaction_id", "?"),
            )
            return None
        
        from_wallet: str = raw.get("from", "")
        to_wallet: str = raw.get("to", "")
        value_raw: str = raw.get("value", "0")
        tx_hash: str = raw.get("transaction_id", "")
        block_timestamp: int = raw.get("block_timestamp", 0)
        timestamp = datetime.fromtimestamp(block_timestamp / 1000, tz=UTC)
        decimals: int = int(token_info.get("decimals", 6))
        divisor = 10 ** decimals
        amount = int(value_raw) / divisor
        tx_type = "sent" if from_wallet.lower() == wallet.lower() else "received"

        return TransactionDTO(
            timestamp=timestamp,
            amount=amount,
            type=tx_type,
            from_wallet=from_wallet,
            to_wallet=to_wallet,
            tx_hash=tx_hash
        )
    except (KeyError, ValueError, TypeError, ZeroDivisionError) as exc:
        logger.warning("Не удалось разобрать транзакцию: %s | tx_id=%s", exc, raw.get("transaction_id", "unknown"))
        return None
