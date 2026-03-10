"""
Юнит-тесты для app/core/security.py

Покрывают:
- ApiKeyStore.add / verify / revoke / list_keys
- Хеширование (чистый текст не хранится)
- generate_api_key: формат и уникальность
- init_api_keys_from_config: форматы "key:owner" и "key"
- verify_api_key зависимость: 200 с верным, 401 без ключа, 401 с неверным, 401 с отозванным
- verify_master_key зависимость: 401 с неверным мастером
"""

import pytest
from fastapi import HTTPException
from unittest.mock import patch

from app.core.security import (
    ApiKeyStore,
    generate_api_key,
    init_api_keys_from_config,
    verify_api_key,
    verify_master_key,
)


# ---------------------------------------------------------------------------
# ApiKeyStore
# ---------------------------------------------------------------------------


class TestApiKeyStore:
    """Тесты хранилища API-ключей."""

    def setup_method(self) -> None:
        """Создаём свежий изолированный стор перед каждым тестом."""
        self.store = ApiKeyStore()

    def test_add_then_verify_returns_info(self) -> None:
        """Добавленный ключ проходит верификацию и возвращает корректные метаданные."""
        self.store.add("my-secret-key", "alice")
        info = self.store.verify("my-secret-key")
        assert info is not None
        assert info.owner == "alice"
        assert info.is_active is True

    def test_key_not_stored_as_plaintext(self) -> None:
        """Убеждаемся что чистый текст ключа не хранится в словаре store."""
        key = "plaintext-key"
        self.store.add(key, "owner")
        # Внутренний словарь не должен содержать чистый текст ключа
        assert key not in self.store._store

    def test_verify_wrong_key_returns_none(self) -> None:
        """Неверный ключ возвращает None."""
        self.store.add("correct-key", "alice")
        assert self.store.verify("wrong-key") is None

    def test_verify_on_empty_store_returns_none(self) -> None:
        """Верификация в пустом хранилище возвращает None."""
        assert self.store.verify("any-key") is None

    def test_revoke_deactivates_key(self) -> None:
        """После отзыва ключ не проходит верификацию."""
        info = self.store.add("revoke-me", "bob")
        self.store.revoke(info.key_id)
        assert self.store.verify("revoke-me") is None

    def test_revoke_returns_true_when_key_found(self) -> None:
        """revoke возвращает True если ключ найден."""
        info = self.store.add("some-key", "carol")
        assert self.store.revoke(info.key_id) is True

    def test_revoke_returns_false_when_key_not_found(self) -> None:
        """revoke возвращает False если key_id не существует."""
        assert self.store.revoke("nonexistent-id-xxxxxx") is False

    def test_revoked_key_stays_in_list_as_inactive(self) -> None:
        """Отозванный ключ остаётся в списке с is_active=False."""
        info = self.store.add("audit-key", "dave")
        self.store.revoke(info.key_id)
        keys = self.store.list_keys()
        revoked = next(k for k in keys if k.key_id == info.key_id)
        assert revoked.is_active is False

    def test_list_keys_returns_all_added(self) -> None:
        """list_keys возвращает все добавленные ключи."""
        self.store.add("key1", "alice")
        self.store.add("key2", "bob")
        self.store.add("key3", "carol")
        assert len(self.store.list_keys()) == 3

    def test_key_id_is_12_hex_chars(self) -> None:
        """key_id должен быть строкой из 12 символов."""
        info = self.store.add("test-key-abc", "tester")
        assert len(info.key_id) == 12
        assert all(c in "0123456789abcdef" for c in info.key_id)

    def test_different_keys_get_different_ids(self) -> None:
        """Разные ключи получают разные key_id."""
        info1 = self.store.add("key-alpha", "owner1")
        info2 = self.store.add("key-beta", "owner2")
        assert info1.key_id != info2.key_id

    def test_list_keys_sorted_by_created_at(self) -> None:
        """list_keys сортируется по времени создания."""
        self.store.add("first", "alice")
        self.store.add("second", "bob")
        keys = self.store.list_keys()
        assert keys[0].owner == "alice"
        assert keys[1].owner == "bob"


# ---------------------------------------------------------------------------
# generate_api_key
# ---------------------------------------------------------------------------


class TestGenerateApiKey:
    """Тесты генератора API-ключей."""

    def test_returns_32_char_hex_string(self) -> None:
        """Генерирует 32-символьную hex-строку."""
        key = generate_api_key()
        assert len(key) == 32
        assert all(c in "0123456789abcdef" for c in key)

    def test_generates_unique_keys(self) -> None:
        """Каждый вызов генерирует уникальный ключ."""
        keys = {generate_api_key() for _ in range(50)}
        assert len(keys) == 50  # все уникальны

    def test_key_is_string(self) -> None:
        """Возвращаемый тип — строка."""
        assert isinstance(generate_api_key(), str)


# ---------------------------------------------------------------------------
# init_api_keys_from_config
# ---------------------------------------------------------------------------


class TestInitApiKeysFromConfig:
    """Тесты загрузки ключей из конфигурации при старте."""

    def setup_method(self) -> None:
        """Подменяем глобальный api_key_store свежим экземпляром."""
        import app.core.security as sec
        self._original = sec.api_key_store
        sec.api_key_store = ApiKeyStore()

    def teardown_method(self) -> None:
        """Восстанавливаем оригинальный api_key_store."""
        import app.core.security as sec
        sec.api_key_store = self._original

    def test_loads_key_with_owner_format(self) -> None:
        """Формат 'key:owner' корректно парсится."""
        import app.core.security as sec
        init_api_keys_from_config(["mykey123:alice"])
        info = sec.api_key_store.verify("mykey123")
        assert info is not None
        assert info.owner == "alice"

    def test_loads_key_without_owner(self) -> None:
        """Ключ без ':owner' получает owner='unknown'."""
        import app.core.security as sec
        init_api_keys_from_config(["standalone-key"])
        info = sec.api_key_store.verify("standalone-key")
        assert info is not None
        assert info.owner == "unknown"

    def test_skips_empty_and_whitespace_entries(self) -> None:
        """Пустые строки и строки из пробелов игнорируются."""
        import app.core.security as sec
        init_api_keys_from_config(["", "   ", "valid-key:bob", "  "])
        assert len(sec.api_key_store.list_keys()) == 1

    def test_loads_multiple_keys(self) -> None:
        """Несколько ключей загружаются все."""
        import app.core.security as sec
        init_api_keys_from_config(["k1:alice", "k2:bob", "k3:carol"])
        assert len(sec.api_key_store.list_keys()) == 3

    def test_empty_list_loads_nothing(self) -> None:
        """Пустой список не добавляет ключей."""
        import app.core.security as sec
        init_api_keys_from_config([])
        assert len(sec.api_key_store.list_keys()) == 0

    def test_key_with_colon_in_owner(self) -> None:
        """Если owner содержит ':', берётся только первое разделение."""
        import app.core.security as sec
        init_api_keys_from_config(["mykey:owner:with:colons"])
        info = sec.api_key_store.verify("mykey")
        assert info is not None
        assert info.owner == "owner:with:colons"


# ---------------------------------------------------------------------------
# verify_api_key зависимость
# ---------------------------------------------------------------------------


class TestVerifyApiKeyDependency:
    """Тесты FastAPI-зависимости verify_api_key."""

    VALID_KEY = "valid-test-key-for-unit-tests"

    def setup_method(self) -> None:
        import app.core.security as sec
        self._original = sec.api_key_store
        fresh = ApiKeyStore()
        fresh.add(self.VALID_KEY, "test-owner")
        sec.api_key_store = fresh

    def teardown_method(self) -> None:
        import app.core.security as sec
        sec.api_key_store = self._original

    def test_valid_key_returns_key_info(self) -> None:
        """Верный ключ возвращает ApiKeyInfo с корректным owner."""
        info = verify_api_key(self.VALID_KEY)
        assert info.owner == "test-owner"
        assert info.is_active is True

    def test_none_raises_401(self) -> None:
        """Отсутствующий ключ (None) бросает HTTP 401."""
        with pytest.raises(HTTPException) as exc_info:
            verify_api_key(None)
        assert exc_info.value.status_code == 401

    def test_empty_string_raises_401(self) -> None:
        """Пустая строка бросает HTTP 401."""
        with pytest.raises(HTTPException) as exc_info:
            verify_api_key("")
        assert exc_info.value.status_code == 401

    def test_wrong_key_raises_401(self) -> None:
        """Неверный ключ бросает HTTP 401."""
        with pytest.raises(HTTPException) as exc_info:
            verify_api_key("totally-wrong-key-xyz")
        assert exc_info.value.status_code == 401

    def test_revoked_key_raises_401(self) -> None:
        """Отозванный ключ бросает HTTP 401."""
        import app.core.security as sec
        info = sec.api_key_store.verify(self.VALID_KEY)
        sec.api_key_store.revoke(info.key_id)
        with pytest.raises(HTTPException) as exc_info:
            verify_api_key(self.VALID_KEY)
        assert exc_info.value.status_code == 401

    def test_401_response_has_www_authenticate_header(self) -> None:
        """HTTP 401 должен содержать заголовок WWW-Authenticate."""
        with pytest.raises(HTTPException) as exc_info:
            verify_api_key(None)
        assert "WWW-Authenticate" in exc_info.value.headers


# ---------------------------------------------------------------------------
# verify_master_key зависимость
# ---------------------------------------------------------------------------


class TestVerifyMasterKeyDependency:
    """Тесты FastAPI-зависимости verify_master_key."""

    MASTER = "test-master-key-for-unit-testing"

    def test_correct_master_key_does_not_raise(self) -> None:
        """Верный мастер-ключ не бросает исключение."""
        with patch("app.core.config.settings.SERVICE_MASTER_KEY", self.MASTER):
            verify_master_key(self.MASTER)   # no exception

    def test_wrong_master_key_raises_401(self) -> None:
        """Неверный мастер-ключ бросает HTTP 401."""
        with patch("app.core.config.settings.SERVICE_MASTER_KEY", self.MASTER):
            with pytest.raises(HTTPException) as exc_info:
                verify_master_key("wrong-master-key")
        assert exc_info.value.status_code == 401

    def test_none_master_key_raises_401(self) -> None:
        """Отсутствующий мастер-ключ (None) бросает HTTP 401."""
        with patch("app.core.config.settings.SERVICE_MASTER_KEY", self.MASTER):
            with pytest.raises(HTTPException) as exc_info:
                verify_master_key(None)
        assert exc_info.value.status_code == 401