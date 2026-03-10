"""
Сервис бизнес-логики транзакций.

Является единственным слоем, который знает про бизнес-правила:
- Параллельный сбор транзакций по нескольким кошелькам через asyncio.gather
- Дедупликация по tx_hash (одна транзакция видна обоим участникам)
- Сортировка от старой к новой
- Фильтрация по направлению (between)
- Агрегация сумм (stats)
- Позиционирование по якорному хешу (after)

TronGridClient инжектируется через конструктор — это позволяет
легко подменить его моком в тестах.
"""

import asyncio
import logging 
from datetime import datetime

from app.core.exceptions import TransactionNotFoundError
from app.models.schemas import TransactionDTO
from app.services.trongrid_client import TronGridClient
from app.core.config import settings

logger = logging.getLogger(__name__)

class TransactionService:
    """
    Сервис получения и анализа USDT TRC20-транзакций.

    Args:
        client: Экземпляр TronGridClient для взаимодействия с TronGrid API.
                Должен быть инициализирован (открыт как context manager)
                до передачи в сервис.
    """
    def __init__(self, client: TronGridClient) -> None:
        self._client = client
        self._semaphore = asyncio.Semaphore(settings.TRONGRID_MAX_CONCURRENT)

    async def get_transactions(
            self, 
            wallets: list[str],
            start_timestamp: datetime | None = None,
            end_timestamp: datetime | None = None,
            ) -> list[TransactionDTO]:
        """
        Возвращает дедуплицированные и отсортированные транзакции для кошельков.

        Запросы к TronGrid выполняются параллельно через asyncio.gather.
        Дедупликация производится по tx_hash — одна и та же транзакция
        может появиться в ответах TronGrid для кошелька-отправителя и
        кошелька-получателя одновременно.

        Args:
            wallets: Список адресов TRON-кошельков (один или несколько).
            start_timestamp: Начало фильтра по времени (UTC, включительно).
            end_timestamp: Конец фильтра по времени (UTC, включительно).

        Returns:
            Уникальные транзакции, отсортированные по timestamp ASC.
        """
        logger.info("get_transactions: wallets=%s", wallets)
        all_txs = await self._fetch_for_wallets(wallets, start_timestamp, end_timestamp)
        return _deduplicate_and_sort(all_txs)
    
    async def get_transactions_between(
            self,
            wallets1: list[str],
            wallet2: str,
            start_timestamp: datetime | None = None,
            end_timestamp: datetime | None = None, 
    ) -> list[TransactionDTO]:
        """
        Возвращает транзакции между группой wallets1 и одним wallet2.

        Фильтрует только переводы, где участвуют обе стороны:
        - from ∈ wallets1 И to == wallet2  (исходящие из wallets1)
        - from == wallet2 И to ∈ wallets1  (входящие в wallets1)

        Args:
            wallets1: Список кошельков первой стороны.
            wallet2: Кошелёк второй стороны.
            start_timestamp: Начало фильтра.
            end_timestamp: Конец фильтра.

        Returns:
            Отсортированные транзакции между двумя сторонами.
        """
        logger.info(
            "get_transactions_between: wallets1=%s wallet2=%s", wallets1, wallet2
        )
        all_wallets = list(set(wallets1 + [wallet2]))
        all_txs = await self._fetch_for_wallets(all_wallets, start_timestamp, end_timestamp)
        deduped = _deduplicate_and_sort(all_txs)

        wallets1_set = {w.lower() for w in wallets1}
        wallet2_lower = wallet2.lower()

        return [
            tx for tx in deduped
            if (
                # wallets1 → wallet2
                (tx.from_wallet.lower() in wallets1_set
                 and tx.to_wallet.lower() == wallet2_lower)
                or
                # wallet2 → wallets1
                (tx.from_wallet.lower() == wallet2_lower
                 and tx.to_wallet.lower() in wallets1_set)
            )
        ]
    
    async def get_transactions_stats(
        self,
        wallets1: list[str],
        wallets2: list[str],
        start_timestamp: datetime | None = None,
        end_timestamp: datetime | None = None,
    ) -> dict[str, float]:
        """
        Считает агрегированную статистику переводов между двумя группами кошельков.

        Args:
            wallets1: Первая группа кошельков.
            wallets2: Вторая группа кошельков.
            start_timestamp: Начало фильтра.
            end_timestamp: Конец фильтра.

        Returns:
            Словарь с ключами:
            - wallets1_to_wallets2 (float): сумма переводов 1→2 в USDT
            - wallets2_to_wallets1 (float): сумма переводов 2→1 в USDT
            - difference (float): разница (1→2) − (2→1)
        """
        logger.info(
            "get_transactions_stats: wallets1=%s wallets2=%s", wallets1, wallets2
        )

        all_wallets = list(set(wallets1 + wallets2))
        all_txs = await self._fetch_for_wallets(all_wallets, start_timestamp, end_timestamp)
        deduped = _deduplicate_and_sort(all_txs)

        w1_set = {w.lower() for w in wallets1}
        w2_set = {w.lower() for w in wallets2}

        w1_to_w2 = sum(
            tx.amount for tx in deduped
            if tx.from_wallet.lower() in w1_set and tx.to_wallet.lower() in w2_set
        )
        w2_to_w1 = sum(
            tx.amount for tx in deduped
            if tx.from_wallet.lower() in w2_set and tx.to_wallet.lower() in w1_set
        )

        return {
            "wallets1_to_wallets2": round(w1_to_w2, 6),
            "wallets2_to_wallets1": round(w2_to_w1, 6),
            "difference": round(w1_to_w2 - w2_to_w1, 6),
        }
    
    async def get_transactions_after_hash(
        self,
        wallets: list[str],
        tx_hash: str,
    ) -> list[TransactionDTO]:
        """
        Возвращает все USDT-транзакции кошельков после указанной транзакции по хешу.

        Алгоритм (ТЗ п.3.4 + п.6):
        1. Загружаем все USDT TRC20-транзакции переданных кошельков.
        2. Ищем anchor (якорную транзакцию) по tx_hash среди загруженных — это
           гарантирует что anchor является USDT-переводом.
           ВАЖНО: нельзя искать anchor через /v1/transactions/{hash} — этот
           endpoint возвращает raw on-chain транзакцию без поля token_info,
           поэтому парсер USDT вернёт None.
        3. Берём транзакции с timestamp > anchor.timestamp.
        4. Строим цепочку кошельков (ТЗ п.6): смотрим на to_wallet каждой
           транзакции после anchor. Если этот кошелёк ещё не загружался —
           дозагружаем его транзакции. Повторяем итеративно пока не появятся
           новые кошельки. Это покрывает случай «первый перевод следующего
           кошелька совпадает с последним переводом предыдущего».

        Args:
            wallets: Список начальных кошельков для поиска.
            tx_hash: Хеш транзакции-якоря (должна быть USDT TRC20-переводом
                     одного из переданных кошельков).

        Returns:
            Транзакции после якорной от старой к новой, включая цепочку.

        Raises:
            TransactionNotFoundError: Если tx_hash не найден среди
                                      USDT-транзакций указанных кошельков.
        """
        logger.info(
            "get_transactions_after_hash: wallets=%s tx_hash=%s", wallets, tx_hash
        )

        # Шаг 1: получаем timestamp якоря — один быстрый запрос
        anchor_ts_ms = await self._client.get_transaction_timestamp(tx_hash)
        print("="*80)
        print(anchor_ts_ms)
        print("="*80)
        if anchor_ts_ms is None:
            raise TransactionNotFoundError(tx_hash)

        logger.debug("Якорь найден: hash=%s timestamp_ms=%d", tx_hash, anchor_ts_ms)

        # Шаг 2: загружаем транзакции начиная с момента якоря
        # min_timestamp_ms передаётся напрямую в миллисекундах — TronGrid
        # вернёт только транзакции >= anchor, пагинация минимальна
        all_txs = await self._fetch_for_wallets_from_ms(wallets, anchor_ts_ms)

        # Шаг 3: убираем саму якорную транзакцию из результата
        result = [tx for tx in all_txs if tx.tx_hash != tx_hash]

        # Шаг 4: цепочки кошельков (ТЗ п.6)
        known_wallets: set[str] = {w.lower() for w in wallets}
        result = await self._expand_wallet_chains(result, known_wallets, anchor_ts_ms)

        return _deduplicate_and_sort(result)

    async def _expand_wallet_chains(
        self,
        txs: list[TransactionDTO],
        known_wallets: set[str],
        anchor_ts_ms: int,
    ) -> list[TransactionDTO]:
        """
        Расширяет список по цепочкам кошельков (ТЗ п.6).

        Если в транзакциях после anchor появляется to_wallet которого ещё
        не загружали — дозагружаем его транзакции. Повторяем итеративно.
        """
        result = list(txs)

        for _ in range(10):
            new_wallets = {
                tx.to_wallet
                for tx in result
                if tx.to_wallet.lower() not in known_wallets
            }
            if not new_wallets:
                break

            logger.debug("Цепочка кошельков: дозагружаем %s", new_wallets)
            extra = await self._fetch_for_wallets_from_ms(list(new_wallets), anchor_ts_ms)
            result.extend(extra)
            known_wallets |= {w.lower() for w in new_wallets}

        return result
    
    async def _fetch_for_wallets_from_ms(
        self,
        wallets: list[str],
        min_timestamp_ms: int,
    ) -> list[TransactionDTO]:
        """
        Параллельная загрузка транзакций начиная с min_timestamp_ms.

        Используется в /after — передаём миллисекунды напрямую в TronGrid
        чтобы не грузить всю историю кошелька.
        """
        tasks = [
            self._client.get_trc20_transactions(
                wallet, min_timestamp_ms=min_timestamp_ms
            )
            for wallet in wallets
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        collected: list[TransactionDTO] = []
        for wallet, result in zip(wallets, results):
            if isinstance(result, Exception):
                logger.error("Ошибка загрузки кошелька %s: %s", wallet, result)
            else:
                collected.extend(result)
        return collected
    
    async def _fetch_one_wallet(
        self,
        wallet: str,
        start_timestamp: datetime | None = None,
        end_timestamp: datetime | None = None,
        min_timestamp_ms: int | None = None,
    ) -> list[TransactionDTO]:
        async with self._semaphore:
            return await self._client.get_trc20_transactions(
                wallet,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                min_timestamp_ms=min_timestamp_ms,
            )
    
    async def _fetch_for_wallets(
        self,
        wallets: list[str],
        start_timestamp: datetime | None = None,
        end_timestamp: datetime | None = None,
    ) -> list[TransactionDTO]:
        """
        Параллельно загружает транзакции для списка кошельков.

        Использует asyncio.gather — все запросы к TronGrid выполняются
        одновременно, что кратно ускоряет работу при нескольких кошельках.

        Ошибки отдельных кошельков логируются, но не прерывают загрузку
        остальных. Это позволяет вернуть частичный результат вместо
        полного отказа при временной недоступности одного адреса.

        Args:
            wallets: Список адресов кошельков.
            start_timestamp: Начало временного фильтра.
            end_timestamp: Конец временного фильтра.

        Returns:
            Объединённый список транзакций (без дедупликации, может содержать дубли).
        """
        tasks = [
            self._fetch_one_wallet(wallet, start_timestamp, end_timestamp)
            for wallet in wallets
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        collected: list[TransactionDTO] = []
        for wallet, result in zip(wallets, results):
            if isinstance(result, Exception):
                logger.error(
                    "Ошибка загрузки транзакций кошелька %s: %s", wallet, result
                )
            else:
                collected.extend(result)

        return collected
    
def _deduplicate_and_sort(transactions: list[TransactionDTO]) -> list[TransactionDTO]:
    """
    Дедуплицирует транзакции по tx_hash и сортирует от старой к новой.

    Дедупликация необходима потому, что одна транзакция между двумя
    кошельками появляется в ответах TronGrid для обоих кошельков
    (у отправителя и у получателя).

    При дедупликации сохраняется первое вхождение транзакции.

    Args:
        transactions: Сырой список транзакций (может содержать дубли).

    Returns:
        Уникальные транзакции, отсортированные по timestamp ASC.
    """
    seen: set[str] = set()
    unique: list[TransactionDTO] = []

    for tx in transactions:
        if tx.tx_hash not in seen:
            seen.add(tx.tx_hash)
            unique.append(tx)

    return sorted(unique, key=lambda t: t.timestamp)