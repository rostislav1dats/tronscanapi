"""
Сервис бизнес-логики транзакций.

Является единственным слоем, который знает про бизнес-правила:
- Параллельный сбор транзакций по нескольким кошелькам через asyncio.gather
- Дедупликация по tx_hash (одна транзакция видна обоим участникам)
- Сортировка от старой к новой
- Фильтрация по направлению (between)
- Агрегация сумм (stats)
- Позиционирование по якорному хешу (after)
- Построение цепочки кошельков: если с кошелька ушёл весь баланс —
  продолжаем выборку по кошельку-получателю

Алгоритм цепочки (применяется ко всем эндпоинтам для wallets/wallets1):
  1. Получаем текущий баланс W через TronGrid /v1/accounts/{W}
  2. Загружаем транзакции W в нужном диапазоне (от новых к старым)
  3. Пересчитываем баланс на каждой транзакции в обратном порядке
  4. Находим последнюю исходящую транзакцию W
  5. Если баланс после неё = 0 → переходим на to_wallet этой транзакции
     и повторяем с шага 1 начиная с timestamp перехода
  6. Если баланс > 0 или транзакций больше нет → цепочка завершена

TronGridClient инжектируется через конструктор — это позволяет
легко подменить его моком в тестах.
"""

import asyncio
import logging 
from datetime import datetime, UTC

from app.core.exceptions import TransactionNotFoundError
from app.models.schemas import TransactionDTO
from app.services.trongrid_client import TronGridClient
from app.core.config import settings

logger = logging.getLogger(__name__)

# _ZERO_BALANCE_THRESHOLD = 0.000001
_ZERO_BALANCE_THRESHOLD = 1

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

        Для каждого кошелька из списка строится цепочка: если последняя
        транзакция кошелька обнулила его баланс — продолжаем по получателю.

        Args:
            wallets: Список адресов TRON-кошельков (один или несколько).
            start_timestamp: Начало фильтра по времени (UTC, включительно).
            end_timestamp: Конец фильтра по времени (UTC, включительно).

        Returns:
            Уникальные транзакции, отсортированные по timestamp ASC.
        """
        logger.info("get_transactions: wallets=%s", wallets)
        all_txs: list[TransactionDTO] = []
        for wallet in wallets:
            chain_txs = await self._collect_chain(
                start_wallet=wallet,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp
            )
            all_txs.extend(chain_txs)
        
        return _deduplicate_and_sort(all_txs)
    
    async def get_transactions_raw(
            self,
            wallets: list[str],
            start_timestamp: datetime | None = None,
            end_timestamp: datetime | None = None
    ) -> list[TransactionDTO]:
        """
        Возвращает транзакции указанных кошельков БЕЗ построения цепочки.
 
        В отличие от get_transactions, не переходит на кошельки-получатели
        даже если баланс обнулился. Возвращает только то что есть по
        переданным адресам.
        """
        logger.info(f'get_transactions_raw: wallets={wallets}')
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

        Цепочка строится только для wallets1. wallet2 остаётся неизменным.
        """
        logger.info(
            "get_transactions_between: wallets1=%s wallet2=%s", wallets1, wallet2
        )

        all_chain_txs: list[TransactionDTO] = []
        visited_wallets = set()
        
        for w1 in wallets1:
            chain_txs = await self._collect_chain(
                start_wallet=w1,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                visited=visited_wallets
            )
            all_chain_txs.extend(chain_txs)

        # Дедуплицируем транзакции, собранные ТОЛЬКО по цепочке wallets1
        deduped = _deduplicate_and_sort(all_chain_txs)

        chain_set = {w.lower() for w in visited_wallets}
        wallet2_lower = wallet2.lower()

        # Фильтруем: оставляем только те транзакции из цепочки, 
        # где второй стороной был wallet2
        return [
            tx for tx in deduped
            if (
                (tx.from_wallet.lower() in chain_set and tx.to_wallet.lower() == wallet2_lower)
                or
                (tx.from_wallet.lower() == wallet2_lower and tx.to_wallet.lower() in chain_set)
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

        all_chain_txs: list[TransactionDTO] = []
        visited_wallets1 = set()
        
        for w1 in wallets1:
            chain_txs = await self._collect_chain(
                start_wallet=w1,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                visited=visited_wallets1
            )
            all_chain_txs.extend(chain_txs)

        # 2. Дедуплицируем транзакции, найденные в цепочке
        deduped = _deduplicate_and_sort(all_chain_txs)

        # 3. Подготавливаем сеты для быстрой фильтрации
        w1_chain_set = {w.lower() for w in visited_wallets1}
        w2_set = {w.lower() for w in wallets2}

        # 4. Считаем суммы, фильтруя только взаимодействия с wallets2
        w1_to_w2 = sum(
            tx.amount for tx in deduped
            if tx.from_wallet.lower() in w1_chain_set and tx.to_wallet.lower() in w2_set
        )
        w2_to_w1 = sum(
            tx.amount for tx in deduped
            if tx.from_wallet.lower() in w2_set and tx.to_wallet.lower() in w1_chain_set
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
        start_timestamp: datetime | None = None,
        end_timestamp: datetime | None = None,
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
            start_timestamp: Дополнительная нижняя граница (поверх якоря, UTC).
            end_timestamp: Верхняя граница по времени (UTC).

        Returns:
            Транзакции после якорной от старой к новой, включая цепочку.

        Raises:
            TransactionNotFoundError: Если tx_hash не найден среди
                                      USDT-транзакций указанных кошельков.
        """
        logger.info(
            "get_transactions_after_hash: wallets=%s tx_hash=%s", wallets, tx_hash
        )

        anchor_ts_ms = await self._client.get_transaction_timestamp(tx_hash)
        if anchor_ts_ms is None:
            raise TransactionNotFoundError(tx_hash)

        logger.debug("Якорь найден: hash=%s timestamp_ms=%d", tx_hash, anchor_ts_ms)

        anchor_dt = datetime.fromtimestamp(anchor_ts_ms / 1000, tz=UTC)

        # Нижняя граница — max(anchor_dt, start_timestamp) чтобы не возвращать
        # транзакции раньше якоря даже если start_timestamp задан позже него
        effective_start = anchor_dt
        if start_timestamp is not None and start_timestamp > anchor_dt:
            effective_start = start_timestamp

        all_txs: list[TransactionDTO] = []
        for wallet in wallets:
            chain_txs = await self._collect_chain(
                start_wallet=wallet,
                start_timestamp=effective_start,
                end_timestamp=end_timestamp,
                anchor_hash=tx_hash
            )
            all_txs.extend(chain_txs)

        return _deduplicate_and_sort(all_txs)

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
        
    async def _collect_chain(
        self,
        start_wallet: str,
        start_timestamp: datetime | None = None,
        end_timestamp: datetime | None = None,
        anchor_hash: str | None = None,
        visited: set[str] | None = None,
    ) -> list[TransactionDTO]:
        """
        Рекурсивно собирает транзакции по цепочке кошельков.
 
        Начинает с start_wallet, загружает его транзакции. Если последняя
        исходящая транзакция обнулила баланс — переходит на кошелёк-получатель
        и продолжает выборку с момента этого перехода.
 
        Args:
            start_wallet: Кошелёк с которого начинаем.
            start_timestamp: Нижняя граница выборки (включительно).
            end_timestamp: Верхняя граница выборки.
            anchor_hash: Хеш якорной транзакции — исключается из результата.
            visited: Множество уже посещённых кошельков (защита от циклов).
 
        Returns:
            Все транзакции цепочки начиная с start_wallet.
        """
        if visited is None:
            visited = set()

        wallet_lower = start_wallet.lower()
        if wallet_lower in visited:
            logger.warning("Цикл в цепочке кошельков: %s уже посещён", start_wallet)
            return []
        
        visited.add(wallet_lower)

        try:
            current_balance = await self._client.get_usdt_balance(start_wallet)
        except Exception as e:
            logger.error("Не удалось получить баланс %s: %s", start_wallet, e)
            current_balance = None

        txs = await self._fetch_one_wallet(
            start_wallet,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp
        )

        result = [tx for tx in txs if tx.tx_hash != anchor_hash]
        if not txs:
            return result
        
        # Ищем переход цепочки: последняя исходящая транзакция обнулила баланс
        next_wallet, next_ts = self._find_chain_transaction(
            txs=txs,
            wallet=start_wallet,
            current_balance=current_balance
        )

        if next_wallet is not None and next_ts is not None:
            logger.info(
                "Цепочка: %s → %s (с %s)", start_wallet, next_wallet, next_ts
            )
            next_txs = await self._collect_chain(
                start_wallet=next_wallet,
                start_timestamp=next_ts,
                end_timestamp=end_timestamp,
                anchor_hash=anchor_hash,
                visited=visited
            )
            result.extend(next_txs)
        
        return result
    
    def _find_chain_transaction(
            self, 
            txs: list[TransactionDTO],
            wallet: str,
            current_balance: float | None
    ) -> tuple[str | None, datetime | None]:
        """
        Определяет переход цепочки: находит последнюю исходящую транзакцию
        и проверяет обнулила ли она баланс кошелька.
 
        Алгоритм:
        - Идём от новых к старым, пересчитываем баланс в обратном направлении
        - Если текущий баланс неизвестен — не можем определить переход
        - Если после последней исходящей транзакции баланс = 0 → переход
 
        Returns:
            (next_wallet, transition_timestamp) или (None, None) если перехода нет.
        """
        if current_balance is None:
            return None, None
        
        wallet_lower = wallet.lower()
        sorted_txs = sorted(txs, key=lambda t: t.timestamp, reverse=True)

        balance = current_balance
        last_outgoing: TransactionDTO | None = None

        for tx in sorted_txs:
            is_outgoing = tx.from_wallet.lower() == wallet_lower

            if is_outgoing:
                balance += tx.amount
                if last_outgoing is None:
                    last_outgoing = tx
            
            else:
                balance -= tx.amount

        if last_outgoing is None:
            return None, None
        
        balance_after = current_balance
        for tx in sorted_txs:
            if tx.timestamp > last_outgoing.timestamp:
                if tx.from_wallet.lower() == wallet_lower:
                    balance_after += tx.amount
                else:
                    balance_after -= tx.amount

        logger.debug(
            "Цепочка анализ %s: баланс после последней исходящей = %.6f USDT",
            wallet, balance_after,
        )

        if balance_after <= _ZERO_BALANCE_THRESHOLD:
            return last_outgoing.to_wallet, last_outgoing.timestamp
        
        return None, None
    
    async def _collect_chain_wallets(
            self,
            wallets: list[str],
            start_timestamp: datetime | None,
            end_timestamp: datetime | None
    ) -> list[str]:
        """
        Возвращает все адреса кошельков в цепочке (без транзакций).
 
        Используется в between /stats чтобы сначала определить полный
        набор кошельков цепочки, а потом загрузить их транзакции.
        """
        all_chain_wallets: list[str] = []
        visited: set[str] = set()
 
        async def _walk(wallet: str) -> None:
            wallet_lower = wallet.lower()
            if wallet_lower in visited:
                return
            visited.add(wallet_lower)
 
            if wallet not in all_chain_wallets:
                all_chain_wallets.append(wallet)
 
            try:
                current_balance = await self._client.get_usdt_balance(wallet)
            except Exception as e:
                logger.error("Не удалось получить баланс %s: %s", wallet, e)
                return
 
            txs = await self._fetch_one_wallet(
                wallet,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
            )
 
            next_wallet, _ = self._find_chain_transaction(
                txs=txs,
                wallet=wallet,
                current_balance=current_balance,
            )
 
            if next_wallet is not None:
                logger.debug("Цепочка wallets: %s → %s", wallet, next_wallet)
                await _walk(next_wallet)
 
        for wallet in wallets:
            await _walk(wallet)
 
        return all_chain_wallets
    
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