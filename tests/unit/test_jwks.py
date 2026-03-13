"""Unit tests for JWKS module."""

import time
from unittest.mock import MagicMock, patch

import pytest

from mds.jwks import JWKS_CACHE_TTL, _jwks_cache, derive_jwks_url, fetch_jwks, get_jwks_key


@pytest.fixture(autouse=True)
def clear_cache():
    """Ensure a clean JWKS cache for every test."""
    _jwks_cache.clear()
    yield
    _jwks_cache.clear()


class TestDeriveJwksUrl:
    def test_basic(self):
        assert derive_jwks_url("https://phonepe.auth0.com/") == "https://phonepe.auth0.com/.well-known/jwks.json"

    def test_no_trailing_slash(self):
        assert derive_jwks_url("https://phonepe.auth0.com") == "https://phonepe.auth0.com/.well-known/jwks.json"

    def test_with_path(self):
        assert derive_jwks_url("https://login.okta.com/oauth2") == "https://login.okta.com/oauth2/.well-known/jwks.json"


class TestFetchJwks:
    def test_caches_keys(self):
        fake_key = MagicMock()
        fake_jwk = MagicMock()
        fake_jwk.key = fake_key

        jwks_response = {"keys": [{"kid": "k1", "kty": "RSA"}]}

        with patch("mds.jwks.requests.get") as mock_get, patch("mds.jwks.PyJWK.from_dict", return_value=fake_jwk):
            mock_get.return_value = MagicMock(status_code=200, json=lambda: jwks_response)
            mock_get.return_value.raise_for_status = MagicMock()

            keys = fetch_jwks("https://example.com")
            assert "k1" in keys
            assert keys["k1"] is fake_key
            assert "https://example.com" in _jwks_cache

    def test_returns_cached_within_ttl(self):
        _jwks_cache["https://cached.com"] = {
            "fetched_at": time.time(),
            "keys": {"k1": "cached-key"},
        }
        keys = fetch_jwks("https://cached.com")
        assert keys == {"k1": "cached-key"}

    def test_stale_fallback_on_http_failure(self):
        _jwks_cache["https://stale.com"] = {
            "fetched_at": time.time() - JWKS_CACHE_TTL - 100,
            "keys": {"k1": "stale-key"},
        }
        with patch("mds.jwks.requests.get", side_effect=Exception("network error")):
            keys = fetch_jwks("https://stale.com")
            assert keys == {"k1": "stale-key"}

    def test_empty_on_failure_no_cache(self):
        with patch("mds.jwks.requests.get", side_effect=Exception("network error")):
            keys = fetch_jwks("https://new.com")
            assert keys == {}

    def test_skips_keys_without_kid(self):
        jwks_response = {"keys": [{"kty": "RSA"}]}  # no kid
        with patch("mds.jwks.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: jwks_response)
            mock_get.return_value.raise_for_status = MagicMock()

            keys = fetch_jwks("https://no-kid.com")
            assert keys == {}

    def test_derives_correct_url(self):
        """Verify fetch_jwks calls the derived JWKS URL."""
        jwks_response = {"keys": []}
        with patch("mds.jwks.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: jwks_response)
            mock_get.return_value.raise_for_status = MagicMock()

            fetch_jwks("https://auth.example.com/")
            mock_get.assert_called_once()
            called_url = mock_get.call_args[0][0]
            assert called_url == "https://auth.example.com/.well-known/jwks.json"


class TestGetJwksKey:
    def test_returns_specific_kid(self):
        _jwks_cache["https://c.com"] = {"fetched_at": time.time(), "keys": {"a": "key-a", "b": "key-b"}}
        assert get_jwks_key("https://c.com", "b") == "key-b"

    def test_returns_first_key_when_no_kid(self):
        _jwks_cache["https://c.com"] = {"fetched_at": time.time(), "keys": {"a": "key-a"}}
        assert get_jwks_key("https://c.com") == "key-a"

    def test_returns_none_when_empty(self):
        with patch("mds.jwks.fetch_jwks", return_value={}):
            assert get_jwks_key("https://c.com", "missing") is None


class TestInvalidateIssuerCache:
    def test_invalidate_existing(self):
        from mds.jwks import invalidate_issuer_cache

        _jwks_cache["https://to-invalidate.com"] = {
            "fetched_at": time.time(),
            "keys": {"k1": "v1"},
        }
        assert invalidate_issuer_cache("https://to-invalidate.com") is True
        assert "https://to-invalidate.com" not in _jwks_cache

    def test_invalidate_nonexistent(self):
        from mds.jwks import invalidate_issuer_cache

        assert invalidate_issuer_cache("https://ghost.com") is False
