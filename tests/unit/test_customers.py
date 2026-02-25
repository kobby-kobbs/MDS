"""Unit tests for customers module."""

import pytest
from unittest.mock import patch, MagicMock

from mds.customers import (
    CUSTOMERS, get_public_key, get_customer_by_issuer,
    get_customer_by_api_key, get_customer_registry, get_customer_storage,
)


class TestCustomersConfig:
    def test_phonepe_exists(self):
        assert "phonepe" in CUSTOMERS

    def test_phonepe_has_required_fields(self):
        cfg = CUSTOMERS["phonepe"]
        assert "jwks_url" in cfg
        assert "issuer" in cfg
        assert "models" in cfg
        assert "sub" in cfg

    def test_phonepe_wildcard_models(self):
        assert "*" in CUSTOMERS["phonepe"]["models"]


class TestGetCustomerByIssuer:
    def test_known_issuer(self):
        issuer = CUSTOMERS["phonepe"]["issuer"]
        assert get_customer_by_issuer(issuer) == "phonepe"

    def test_unknown_issuer(self):
        assert get_customer_by_issuer("https://auth.unknown.com") is None

    def test_empty_issuer(self):
        assert get_customer_by_issuer("") is None

    def test_none_issuer(self):
        assert get_customer_by_issuer(None) is None


class TestGetCustomerByApiKey:
    def test_no_key(self):
        assert get_customer_by_api_key("") is None
        assert get_customer_by_api_key(None) is None

    def test_unknown_key(self):
        assert get_customer_by_api_key("random-key-that-doesnt-exist") is None

    def test_matching_key(self):
        with patch.dict(CUSTOMERS, {"t": {"api_key": "secret-123", "models": ["*"], "sub": "t"}}):
            assert get_customer_by_api_key("secret-123") == "t"

    def test_empty_api_key_in_config_not_matched(self):
        """Customers without api_key set should not match empty strings."""
        with patch.dict(CUSTOMERS, {"t": {"api_key": "", "models": ["*"], "sub": "t"}}):
            assert get_customer_by_api_key("") is None


class TestGetPublicKey:
    def test_unknown_customer_returns_none(self):
        key = get_public_key("nonexistent", None)
        assert key is None

    def test_no_customer_returns_none(self):
        key = get_public_key(None, None)
        assert key is None

    def test_jwks_key_returned_when_available(self):
        mock_key = MagicMock()
        with patch("mds.jwks.get_jwks_key", return_value=mock_key):
            key = get_public_key("phonepe", "kid-1")
            assert key is mock_key

    def test_jwks_failure_returns_none(self):
        """When JWKS cannot fetch keys, get_public_key returns None."""
        with patch("mds.jwks.get_jwks_key", return_value=None):
            key = get_public_key("phonepe", "any-kid")
            assert key is None

    @patch.dict("os.environ", {"CUSTOMER_PHONEPE_PUBLIC_KEY": "not-pem"})
    @patch("mds.jwks.get_jwks_key", return_value=None)
    def test_bad_env_pem_falls_through(self, mock_jwks):
        result = get_public_key("phonepe", "kid")
        mock_jwks.assert_called_once()

    @patch.dict("os.environ", {"CUSTOMER_PHONEPE_PUBLIC_KEY": ""})
    @patch("mds.jwks.get_jwks_key", return_value="k")
    def test_empty_env_var_uses_jwks(self, m):
        assert get_public_key("phonepe", None) == "k"

    def test_no_jwks_url_returns_none(self):
        with patch.dict(CUSTOMERS, {"t": {"jwks_url": "", "issuer": "i", "models": ["*"], "sub": "s", "name": "t"}}):
            assert get_public_key("t", None) is None


class TestGetCustomerRegistry:
    def test_unknown(self):
        assert get_customer_registry("nope") is None

    def test_none(self):
        assert get_customer_registry(None) is None

    def test_empty_string(self):
        assert get_customer_registry("") is None

    def test_empty_value_returns_none(self):
        with patch.dict(CUSTOMERS, {"t": {"registry_name": ""}}):
            assert get_customer_registry("t") is None

    def test_returns_value(self):
        with patch.dict(CUSTOMERS, {"t": {"registry_name": "my-reg"}}):
            assert get_customer_registry("t") == "my-reg"


class TestGetCustomerStorage:
    def test_unknown(self):
        assert get_customer_storage("nope") is None

    def test_none(self):
        assert get_customer_storage(None) is None

    def test_empty_value_returns_none(self):
        with patch.dict(CUSTOMERS, {"t": {"storage_account": ""}}):
            assert get_customer_storage("t") is None

    def test_returns_value(self):
        with patch.dict(CUSTOMERS, {"t": {"storage_account": "acct"}}):
            assert get_customer_storage("t") == "acct"
