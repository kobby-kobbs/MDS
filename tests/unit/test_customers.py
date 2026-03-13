"""Unit tests for customers module."""

import hashlib
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from mds.customers import (
    CUSTOMERS,
    get_customer_by_api_key,
    get_customer_registry,
    get_customer_storage,
    reload_customers,
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


class TestGetCustomerByApiKey:
    def test_no_key(self):
        assert get_customer_by_api_key("") is None
        assert get_customer_by_api_key(None) is None

    def test_unknown_key(self):
        assert get_customer_by_api_key("random-key-that-doesnt-exist") is None

    def test_matching_key_from_env(self):
        """API key is read from CUSTOMER_<ID>_API_KEY env var."""
        with patch.dict(os.environ, {"CUSTOMER_PHONEPE_API_KEY": "secret-123"}):
            assert get_customer_by_api_key("secret-123") == "phonepe"

    def test_wrong_key_rejected(self):
        with patch.dict(os.environ, {"CUSTOMER_PHONEPE_API_KEY": "correct-key"}):
            assert get_customer_by_api_key("wrong-key") is None

    def test_no_env_var_set(self):
        """When no API key env var is set, no match."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CUSTOMER_PHONEPE_API_KEY", None)
            assert get_customer_by_api_key("any-key") is None


class TestReloadCustomers:
    @pytest.fixture(autouse=True)
    def _restore_customers(self):
        """Save and restore CUSTOMERS dict around reload tests."""
        saved = dict(CUSTOMERS)
        yield
        CUSTOMERS.clear()
        CUSTOMERS.update(saved)

    def test_reload_refreshes_data(self, tmp_path):
        cfg = tmp_path / "customers.json"
        cfg.write_text(
            json.dumps(
                {
                    "test": {
                        "name": "Test",
                        "issuer": "https://test.auth0.com",
                        "jwks_url": "https://test.auth0.com/.well-known/jwks.json",
                        "models": ["model-a"],
                    }
                }
            )
        )
        with patch("mds.customers._find_customers_file", return_value=cfg):
            reload_customers()
            assert "test" in CUSTOMERS
            assert CUSTOMERS["test"]["models"] == ["model-a"]

    def test_reload_picks_up_new_customers(self, tmp_path):
        cfg = tmp_path / "customers.json"
        cfg.write_text(
            json.dumps(
                {
                    "alpha": {
                        "name": "Alpha",
                        "issuer": "https://alpha.com",
                        "jwks_url": "https://alpha.com/.well-known/jwks.json",
                        "models": ["*"],
                    }
                }
            )
        )
        with patch("mds.customers._find_customers_file", return_value=cfg):
            reload_customers()
            assert "alpha" in CUSTOMERS
            # Update file and reload again
            cfg.write_text(
                json.dumps(
                    {
                        "alpha": {
                            "name": "Alpha",
                            "issuer": "https://alpha.com",
                            "jwks_url": "https://alpha.com/.well-known/jwks.json",
                            "models": ["*"],
                        },
                        "beta": {
                            "name": "Beta",
                            "issuer": "https://beta.com",
                            "jwks_url": "https://beta.com/.well-known/jwks.json",
                            "models": ["model-x"],
                        },
                    }
                )
            )
            reload_customers()
            assert "beta" in CUSTOMERS

    def test_reload_removes_deleted_customers(self, tmp_path):
        cfg = tmp_path / "customers.json"
        cfg.write_text(
            json.dumps(
                {
                    "gone": {
                        "name": "Gone",
                        "issuer": "https://gone.com",
                        "jwks_url": "https://gone.com/.well-known/jwks.json",
                        "models": ["*"],
                    }
                }
            )
        )
        with patch("mds.customers._find_customers_file", return_value=cfg):
            reload_customers()
            assert "gone" in CUSTOMERS
            # Remove from file
            cfg.write_text(json.dumps({}))
            reload_customers()
            assert "gone" not in CUSTOMERS


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


class TestDiscoverJwksUrl:
    """Tests for OIDC auto-discovery of JWKS URL."""

    def test_oidc_discovery_success(self):
        from mds.customers import discover_jwks_url

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"jwks_uri": "https://auth.example.com/.well-known/jwks.json"}
        mock_resp.raise_for_status = MagicMock()

        with patch("mds.customers.requests.get", return_value=mock_resp):
            url = discover_jwks_url("https://auth.example.com")
            assert url == "https://auth.example.com/.well-known/jwks.json"

    def test_oidc_fallback_when_oidc_fails(self):
        from mds.customers import discover_jwks_url

        def side_effect(url, **kwargs):
            if "openid-configuration" in url:
                raise Exception("not found")
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        with patch("mds.customers.requests.get", side_effect=side_effect):
            url = discover_jwks_url("https://auth.example.com")
            assert url == "https://auth.example.com/.well-known/jwks.json"

    def test_raises_when_both_fail(self):
        from mds.customers import discover_jwks_url

        with patch("mds.customers.requests.get", side_effect=Exception("network")):
            with pytest.raises(ValueError, match="Could not discover JWKS"):
                discover_jwks_url("https://auth.example.com")

    def test_strips_trailing_slash(self):
        from mds.customers import discover_jwks_url

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"jwks_uri": "https://auth.example.com/jwks"}
        mock_resp.raise_for_status = MagicMock()

        with patch("mds.customers.requests.get", return_value=mock_resp) as mock_get:
            discover_jwks_url("https://auth.example.com/")
            call_url = mock_get.call_args_list[0][0][0]
            assert "//" not in call_url.replace("https://", "")


class TestRegisterCustomer:
    """Tests for self-service customer registration."""

    @pytest.fixture(autouse=True)
    def _restore_customers(self):
        saved = dict(CUSTOMERS)
        yield
        CUSTOMERS.clear()
        CUSTOMERS.update(saved)

    def test_register_new_customer(self, tmp_path):
        from mds.customers import register_customer

        cfg = tmp_path / "customers.json"
        cfg.write_text(json.dumps({}))

        with (
            patch("mds.customers._find_customers_file", return_value=cfg),
            patch("mds.customers._get_writable_config_path", return_value=cfg),
            patch(
                "mds.customers.discover_jwks_url",
                return_value="https://auth.acme.com/.well-known/jwks.json",
            ),
        ):
            result = register_customer(
                customer_name="Acme Corp",
                issuer="https://auth.acme.com",
                models=["model-a", "model-b"],
                registry_name="mds-acme-registry",
                storage_account="mdsacmestorage",
            )

            assert result["customer_id"] == "acme-corp"
            assert "api_key" in result
            assert len(result["api_key"]) > 20
            assert result["jwks_url"] == "https://auth.acme.com/.well-known/jwks.json"

            assert "acme-corp" in CUSTOMERS
            assert CUSTOMERS["acme-corp"]["registry_name"] == "mds-acme-registry"

            written = json.loads(cfg.read_text())
            assert "acme-corp" in written
            assert "api_key_hash" in written["acme-corp"]

    def test_register_duplicate_raises(self, tmp_path):
        from mds.customers import register_customer

        cfg = tmp_path / "customers.json"
        cfg.write_text(json.dumps({}))

        with (
            patch("mds.customers._find_customers_file", return_value=cfg),
            patch("mds.customers._get_writable_config_path", return_value=cfg),
            patch(
                "mds.customers.discover_jwks_url",
                return_value="https://x.com/jwks",
            ),
        ):
            register_customer("dup", "https://x.com", ["*"], "reg", "stor")
            with pytest.raises(ValueError, match="already exists"):
                register_customer("dup", "https://x.com", ["*"], "reg", "stor")

    def test_register_with_explicit_jwks_url(self, tmp_path):
        from mds.customers import register_customer

        cfg = tmp_path / "customers.json"
        cfg.write_text(json.dumps({}))

        with (
            patch("mds.customers._find_customers_file", return_value=cfg),
            patch("mds.customers._get_writable_config_path", return_value=cfg),
        ):
            result = register_customer(
                customer_name="explicit",
                issuer="https://auth.explicit.com",
                models=["*"],
                registry_name="reg",
                storage_account="stor",
                jwks_url="https://custom.jwks.com/keys",
            )
            assert result["jwks_url"] == "https://custom.jwks.com/keys"

    def test_api_key_hash_verifiable(self, tmp_path):
        from mds.customers import register_customer

        cfg = tmp_path / "customers.json"
        cfg.write_text(json.dumps({}))

        with (
            patch("mds.customers._find_customers_file", return_value=cfg),
            patch("mds.customers._get_writable_config_path", return_value=cfg),
            patch(
                "mds.customers.discover_jwks_url",
                return_value="https://x.com/jwks",
            ),
        ):
            result = register_customer("hashtest", "https://x.com", ["*"], "r", "s")
            expected_hash = hashlib.sha256(result["api_key"].encode()).hexdigest()
            assert CUSTOMERS["hashtest"]["api_key_hash"] == expected_hash


class TestRemoveCustomer:
    @pytest.fixture(autouse=True)
    def _restore_customers(self):
        saved = dict(CUSTOMERS)
        yield
        CUSTOMERS.clear()
        CUSTOMERS.update(saved)

    def test_remove_existing(self, tmp_path):
        from mds.customers import remove_customer

        cfg = tmp_path / "customers.json"
        data = {
            "removeme": {
                "name": "R",
                "issuer": "i",
                "jwks_url": "j",
                "models": ["*"],
            }
        }
        cfg.write_text(json.dumps(data))
        CUSTOMERS.update(data)

        with (
            patch("mds.customers._find_customers_file", return_value=cfg),
            patch("mds.customers._get_writable_config_path", return_value=cfg),
        ):
            assert remove_customer("removeme") is True
            assert "removeme" not in CUSTOMERS
            written = json.loads(cfg.read_text())
            assert "removeme" not in written

    def test_remove_nonexistent(self):
        from mds.customers import remove_customer

        assert remove_customer("ghost") is False


class TestGetCustomerByApiKeyWithHash:
    """Tests for hash-based API key lookup (self-service registrations)."""

    @pytest.fixture(autouse=True)
    def _restore_customers(self):
        saved = dict(CUSTOMERS)
        yield
        CUSTOMERS.clear()
        CUSTOMERS.update(saved)

    def test_hash_based_lookup(self):
        api_key = "test-secret-key-12345"
        api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        CUSTOMERS["hashcust"] = {
            "name": "Hash Customer",
            "issuer": "https://hash.com",
            "jwks_url": "https://hash.com/jwks",
            "models": ["*"],
            "api_key_hash": api_key_hash,
        }
        assert get_customer_by_api_key(api_key) == "hashcust"

    def test_wrong_key_rejected(self):
        api_key_hash = hashlib.sha256(b"correct-key").hexdigest()
        CUSTOMERS["hashcust2"] = {
            "name": "H2",
            "issuer": "i",
            "jwks_url": "j",
            "models": ["*"],
            "api_key_hash": api_key_hash,
        }
        assert get_customer_by_api_key("wrong-key") is None

    def test_env_var_takes_priority(self):
        api_key = "dual-key"
        api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        CUSTOMERS["dualcust"] = {
            "name": "Dual",
            "issuer": "i",
            "jwks_url": "j",
            "models": ["*"],
            "api_key_hash": api_key_hash,
        }
        with patch.dict(os.environ, {"CUSTOMER_DUALCUST_API_KEY": api_key}):
            assert get_customer_by_api_key(api_key) == "dualcust"
