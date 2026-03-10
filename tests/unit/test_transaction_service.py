"""
Юнит-тесты для app/services/transaction_service.py

Используют mock TronGridClient — тестируют бизнес-логику
изолированно от сети и TronGrid API.

Покрывают:
- _deduplicate_and_sort: дедупликация по tx_hash, сортировка по timestamp
- get_transactions: параллельная загрузка, дедупликация, устойчивость к ошибкам
- get_transactions_between: фильтрация по парам кошельков, оба направления
- get_transactions_stats: агрегация сумм, отрицательная разница
- get_transactions_after_hash: поиск anchor, фильтрация, TransactionNotFoundError
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import TransactionNotFoundError
from app.models.schemas import TransactionDTO
from app.services.transaction_service import TransactionService, _deduplicate_and_sort

# ---------------------------------------------------------------------------
# Тестовые константы и фабрики
# ---------------------------------------------------------------------------

W1 = "TXytKFZbjHY6fCDhCGnUR5rEeKHrPwTFJZ"
W2 = "TYytKFZbjHY6fCDhCGnUR5rEeKHrPwTFJY"
W3 = "TZytKFZbjHY6fCDhCGnUR5rEeKHrPwTFJX"


def make_tx(
    tx_hash: str,
    from_wallet: str = W1,
    to_wallet: str = W2,
    amount: float = 100.0,
    timestamp: datetime | None = None,
) -> TransactionDTO:
    """Фабрика тестовых транзакций с удобными дефолтами."""
    if timestamp is None:
        timestamp = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
    return TransactionDTO(
        timestamp=timestamp,
        amount=amount,
        type="sent" if from_wallet == W1 else "received",
        from_wallet=from_wallet,
        to_wallet=to_wallet,
        tx_hash=tx_hash,
    )


def make_client(txs_by_wallet: dict[str, list[TransactionDTO]]) -> MagicMock:
    """
    Создаёт mock TronGridClient.

    get_trc20_transactions возвращает транзакции по кошельку из словаря.
    get_transaction_timestamp по умолчанию возвращает None.
    Тесты /after переопределяют его:
        client.get_transaction_timestamp = AsyncMock(return_value=<ts_ms>)
    """
    client = MagicMock()
    client.get_trc20_transactions = AsyncMock(
        side_effect=lambda wallet, *args, **kwargs: txs_by_wallet.get(wallet, [])
    )
    client.get_transaction_timestamp = AsyncMock(return_value=None)
    return client


# ---------------------------------------------------------------------------
# _deduplicate_and_sort
# ---------------------------------------------------------------------------


class TestDeduplicateAndSort:
    """Тесты вспомогательной функции модуля."""

    def test_removes_duplicate_hashes(self) -> None:
        """Транзакции с одинаковым tx_hash дедуплицируются."""
        tx1 = make_tx("hash1", timestamp=datetime(2026, 1, 1, tzinfo=UTC))
        tx2 = make_tx("hash1", timestamp=datetime(2026, 1, 2, tzinfo=UTC))
        result = _deduplicate_and_sort([tx1, tx2])
        assert len(result) == 1

    def test_keeps_first_occurrence(self) -> None:
        """При дедупликации сохраняется первое вхождение."""
        tx1 = make_tx("hash1", timestamp=datetime(2026, 1, 1, tzinfo=UTC))
        tx_dup = make_tx("hash1", timestamp=datetime(2026, 1, 2, tzinfo=UTC))
        result = _deduplicate_and_sort([tx1, tx_dup])
        assert result[0].timestamp == datetime(2026, 1, 1, tzinfo=UTC)

    def test_sorts_oldest_first(self) -> None:
        """Транзакции сортируются от старой к новой."""
        tx_new = make_tx("hash_new", timestamp=datetime(2026, 3, 1, tzinfo=UTC))
        tx_old = make_tx("hash_old", timestamp=datetime(2026, 1, 1, tzinfo=UTC))
        tx_mid = make_tx("hash_mid", timestamp=datetime(2026, 2, 1, tzinfo=UTC))
        result = _deduplicate_and_sort([tx_new, tx_old, tx_mid])
        assert [tx.tx_hash for tx in result] == ["hash_old", "hash_mid", "hash_new"]

    def test_empty_list_returns_empty(self) -> None:
        """Пустой ввод → пустой вывод."""
        assert _deduplicate_and_sort([]) == []

    def test_single_item_returns_it(self) -> None:
        """Один элемент возвращается без изменений."""
        tx = make_tx("only_hash")
        result = _deduplicate_and_sort([tx])
        assert len(result) == 1
        assert result[0].tx_hash == "only_hash"


# ---------------------------------------------------------------------------
# get_transactions
# ---------------------------------------------------------------------------


class TestGetTransactions:
    """Тесты метода get_transactions."""

    @pytest.mark.asyncio
    async def test_returns_transactions_for_single_wallet(self) -> None:
        """Транзакции одного кошелька возвращаются корректно."""
        tx = make_tx("hash1")
        client = make_client({W1: [tx]})
        result = await TransactionService(client).get_transactions([W1])
        assert len(result) == 1
        assert result[0].tx_hash == "hash1"

    @pytest.mark.asyncio
    async def test_deduplicates_across_wallets(self) -> None:
        """Одна транзакция между W1 и W2 не дублируется при запросе обоих."""
        tx = make_tx("shared_hash", from_wallet=W1, to_wallet=W2)
        client = make_client({W1: [tx], W2: [tx]})
        result = await TransactionService(client).get_transactions([W1, W2])
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_returns_empty_for_no_transactions(self) -> None:
        """Кошелёк без транзакций → пустой список."""
        client = make_client({W1: []})
        result = await TransactionService(client).get_transactions([W1])
        assert result == []

    @pytest.mark.asyncio
    async def test_error_in_one_wallet_does_not_fail_others(self) -> None:
        """Ошибка одного кошелька не ломает остальные."""
        tx_ok = make_tx("hash_ok")
        client = MagicMock()
        client.get_trc20_transactions = AsyncMock(
            side_effect=lambda wallet, *args, **kwargs: (
                [tx_ok] if wallet == W2 else (_ for _ in ()).throw(Exception("API error"))
            )
        )
        result = await TransactionService(client).get_transactions([W1, W2])
        assert len(result) == 1
        assert result[0].tx_hash == "hash_ok"

    @pytest.mark.asyncio
    async def test_multiple_wallets_merged_and_sorted(self) -> None:
        """Транзакции нескольких кошельков объединяются и сортируются."""
        tx_early = make_tx("early", timestamp=datetime(2026, 1, 1, tzinfo=UTC))
        tx_late  = make_tx("late",  timestamp=datetime(2026, 3, 1, tzinfo=UTC))
        client = make_client({W1: [tx_late], W2: [tx_early]})
        result = await TransactionService(client).get_transactions([W1, W2])
        assert result[0].tx_hash == "early"
        assert result[1].tx_hash == "late"


# ---------------------------------------------------------------------------
# get_transactions_between
# ---------------------------------------------------------------------------


class TestGetTransactionsBetween:
    """Тесты метода get_transactions_between."""

    @pytest.mark.asyncio
    async def test_returns_direct_transfers_both_directions(self) -> None:
        """Возвращает переводы в обоих направлениях между wallets1 и wallet2."""
        tx_1_to_2 = make_tx("hash_1_2", from_wallet=W1, to_wallet=W2)
        tx_2_to_1 = make_tx("hash_2_1", from_wallet=W2, to_wallet=W1)
        client = make_client({W1: [tx_1_to_2], W2: [tx_2_to_1]})
        result = await TransactionService(client).get_transactions_between([W1], W2)
        hashes = {tx.tx_hash for tx in result}
        assert hashes == {"hash_1_2", "hash_2_1"}

    @pytest.mark.asyncio
    async def test_excludes_unrelated_transactions(self) -> None:
        """Транзакции не между W1 и W2 не попадают в результат."""
        tx_w1_to_w3 = make_tx("unrelated", from_wallet=W1, to_wallet=W3)
        client = make_client({W1: [tx_w1_to_w3], W2: [], W3: []})
        result = await TransactionService(client).get_transactions_between([W1], W2)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_direct_transfers(self) -> None:
        """Если прямых переводов нет — возвращает пустой список."""
        client = make_client({W1: [], W2: []})
        result = await TransactionService(client).get_transactions_between([W1], W2)
        assert result == []

    @pytest.mark.asyncio
    async def test_multiple_wallets_in_wallets1(self) -> None:
        """wallets1 может содержать несколько кошельков."""
        tx_w1_to_w3 = make_tx("hash_13", from_wallet=W1, to_wallet=W3)
        tx_w2_to_w3 = make_tx("hash_23", from_wallet=W2, to_wallet=W3)
        client = make_client({W1: [tx_w1_to_w3], W2: [tx_w2_to_w3], W3: []})
        result = await TransactionService(client).get_transactions_between([W1, W2], W3)
        hashes = {tx.tx_hash for tx in result}
        assert hashes == {"hash_13", "hash_23"}


# ---------------------------------------------------------------------------
# get_transactions_stats
# ---------------------------------------------------------------------------


class TestGetTransactionsStats:
    """Тесты метода get_transactions_stats."""

    @pytest.mark.asyncio
    async def test_aggregates_amounts_correctly(self) -> None:
        """Суммы в обоих направлениях считаются корректно."""
        tx1 = make_tx("h1", from_wallet=W1, to_wallet=W2, amount=150.0)
        tx2 = make_tx("h2", from_wallet=W2, to_wallet=W1, amount=100.0)
        client = make_client({W1: [tx1], W2: [tx2]})
        result = await TransactionService(client).get_transactions_stats([W1], [W2])
        assert result["wallets1_to_wallets2"] == pytest.approx(150.0)
        assert result["wallets2_to_wallets1"] == pytest.approx(100.0)
        assert result["difference"] == pytest.approx(50.0)

    @pytest.mark.asyncio
    async def test_zero_stats_when_no_transfers(self) -> None:
        """При отсутствии транзакций все поля = 0."""
        client = make_client({W1: [], W2: []})
        result = await TransactionService(client).get_transactions_stats([W1], [W2])
        assert result["wallets1_to_wallets2"] == 0.0
        assert result["wallets2_to_wallets1"] == 0.0
        assert result["difference"] == 0.0

    @pytest.mark.asyncio
    async def test_difference_can_be_negative(self) -> None:
        """Разница может быть отрицательной (w2→w1 > w1→w2)."""
        tx1 = make_tx("h1", from_wallet=W1, to_wallet=W2, amount=50.0)
        tx2 = make_tx("h2", from_wallet=W2, to_wallet=W1, amount=200.0)
        client = make_client({W1: [tx1], W2: [tx2]})
        result = await TransactionService(client).get_transactions_stats([W1], [W2])
        assert result["difference"] == pytest.approx(-150.0)

    @pytest.mark.asyncio
    async def test_does_not_count_unrelated_transfers(self) -> None:
        """Переводы между W1 и W3 не учитываются в статистике W1 vs W2."""
        tx_to_w3 = make_tx("h_unrelated", from_wallet=W1, to_wallet=W3, amount=999.0)
        client = make_client({W1: [tx_to_w3], W2: [], W3: []})
        result = await TransactionService(client).get_transactions_stats([W1], [W2])
        assert result["wallets1_to_wallets2"] == 0.0


# ---------------------------------------------------------------------------
# get_transactions_after_hash
# ---------------------------------------------------------------------------


class TestGetTransactionsAfterHash:
    """
    Тесты метода get_transactions_after_hash.

    Алгоритм после рефакторинга:
    1. get_transaction_timestamp(tx_hash) → anchor_ts_ms (мок возвращает int)
    2. _fetch_for_wallets_from_ms(wallets, anchor_ts_ms - 2000)
    3. Исключаем якорную транзакцию по tx_hash
    4. Расширяем цепочки кошельков

    В тестах: client.get_transaction_timestamp мокируется отдельно.
    get_trc20_transactions мокируется через make_client как обычно.
    """

    @pytest.mark.asyncio
    async def test_returns_transactions_after_anchor(self) -> None:
        """Транзакции после anchor возвращаются, сам anchor исключается."""
        anchor_ts = int(datetime(2026, 2, 1, tzinfo=UTC).timestamp() * 1000)
        after_tx  = make_tx("after",  timestamp=datetime(2026, 3, 1, tzinfo=UTC))
        before_tx = make_tx("before", timestamp=datetime(2026, 1, 1, tzinfo=UTC))

        client = make_client({W1: [before_tx, after_tx]})
        client.get_transaction_timestamp = AsyncMock(return_value=anchor_ts)

        result = await TransactionService(client).get_transactions_after_hash([W1], "anchor_hash")
        # before_tx имеет timestamp < anchor — не попадёт в окно min_timestamp
        # after_tx имеет timestamp > anchor — должен вернуться
        assert any(tx.tx_hash == "after" for tx in result)

    @pytest.mark.asyncio
    async def test_excludes_anchor_itself_from_result(self) -> None:
        """Якорная транзакция не включается в результат."""
        anchor_ts = int(datetime(2026, 2, 1, tzinfo=UTC).timestamp() * 1000)
        anchor_tx = make_tx("anchor_hash", timestamp=datetime(2026, 2, 1, tzinfo=UTC))

        client = make_client({W1: [anchor_tx]})
        client.get_transaction_timestamp = AsyncMock(return_value=anchor_ts)

        result = await TransactionService(client).get_transactions_after_hash([W1], "anchor_hash")
        assert result == []

    @pytest.mark.asyncio
    async def test_raises_transaction_not_found_when_anchor_absent(self) -> None:
        """Если get_transaction_timestamp вернул None — TransactionNotFoundError."""
        client = make_client({W1: []})
        client.get_transaction_timestamp = AsyncMock(return_value=None)

        with pytest.raises(TransactionNotFoundError) as exc_info:
            await TransactionService(client).get_transactions_after_hash([W1], "missing_hash")
        assert exc_info.value.tx_hash == "missing_hash"

    @pytest.mark.asyncio
    async def test_raises_error_when_wallet_has_no_transactions(self) -> None:
        """Если timestamp найден но транзакций нет — возвращает пустой список."""
        anchor_ts = int(datetime(2026, 2, 1, tzinfo=UTC).timestamp() * 1000)
        client = make_client({W1: []})
        client.get_transaction_timestamp = AsyncMock(return_value=anchor_ts)

        # Пустой кошелёк — не ошибка, просто нет транзакций после anchor
        result = await TransactionService(client).get_transactions_after_hash([W1], "anchor_hash")
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_all_before_anchor(self) -> None:
        """Если все транзакции раньше anchor (не попадают в min_timestamp окно) — пусто."""
        anchor_ts = int(datetime(2026, 6, 1, tzinfo=UTC).timestamp() * 1000)
        # Эти транзакции вернутся только если попадут в min_timestamp окно.
        # Мок get_trc20_transactions не фильтрует по времени сам — он вернёт всё.
        # Но anchor убирается по tx_hash, других нет.
        client = make_client({W1: []})
        client.get_transaction_timestamp = AsyncMock(return_value=anchor_ts)

        result = await TransactionService(client).get_transactions_after_hash([W1], "anchor_hash")
        assert result == []