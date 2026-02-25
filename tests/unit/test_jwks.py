"""Unit tests for JWKS module."""

import time
import pytest
from unittest.mock import patch, MagicMock

from mds.jwks import fetch_jwks, get_jwks_key, _jwks_cache, JWKS_CACHE_TTL


@pytest.fixture(autouse=True)
def clear_cache():
    """Ensure a clean JWKS cache for every test."""
    _jwks_cache.clear()
    yield
    _jwks_cache.clear()


class TestFetchJwks:
    def test_caches_keys(self):
        fake_key = MagicMock()
        fake_jwk = MagicMock()
        fake_jwk.key = fake_key

        jwks_response = {"keys": [{"kid": "k1", "kty": "RSA"}]}

        with patch("mds.jwks.requests.get") as mock_get, \
             patch("mds.jwks.PyJWK.from_dict", return_value=fake_jwk):
            mock_get.return_value = MagicMock(status_code=200, json=lambda: jwks_response)
            mock_get.return_value.raise_for_status = MagicMock()

            keys = fetch_jwks("test-customer", "https://example.com/.well-known/jwks.json")
            assert "k1" in keys
            assert keys["k1"] is fake_key
            assert "test-customer" in _jwks_cache

    def test_returns_cached_within_ttl(self):
        _jwks_cache["cached-customer"] = {
            "fetched_at": time.time(),
            "keys": {"k1": "cached-key"},
        }
        keys = fetch_jwks("cached-customer", "https://example.com/jwks")
        assert keys == {"k1": "cached-key"}

    def test_stale_fallback_on_http_failure(self):
        _jwks_cache["stale-customer"] = {
            "fetched_at": time.time() - JWKS_CACHE_TTL - 100,
            "keys": {"k1": "stale-key"},
        }
        with patch("mds.jwks.requests.get", side_effect=Exception("network error")):
            keys = fetch_jwks("stale-customer", "https://example.com/jwks")
            assert keys == {"k1": "stale-key"}

    def test_empty_on_failure_no_cache(self):
        with patch("mds.jwks.requests.get", side_effect=Exception("network error")):
            keys = fetch_jwks("new-customer", "https://example.com/jwks")
            assert keys == {}

    def test_skips_keys_without_kid(self):
        jwks_response = {"keys": [{"kty": "RSA"}]}  # no kid
        with patch("mds.jwks.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: jwks_response)
            mock_get.return_value.raise_for_status = MagicMock()

            keys = fetch_jwks("no-kid", "https://example.com/jwks")
            assert keys == {}


class TestGetJwksKey:
    def test_returns_specific_kid(self):
        _jwks_cache["c"] = {"fetched_at": time.time(), "keys": {"a": "key-a", "b": "key-b"}}
        assert get_jwks_key("c", "url", "b") == "key-b"

    def test_returns_first_key_when_no_kid(self):
        _jwks_cache["c"] = {"fetched_at": time.time(), "keys": {"a": "key-a"}}
        assert get_jwks_key("c", "url") == "key-a"

    def test_returns_none_when_empty(self):
        with patch("mds.jwks.fetch_jwks", return_value={}):
            assert get_jwks_key("c", "url", "missing") is None
