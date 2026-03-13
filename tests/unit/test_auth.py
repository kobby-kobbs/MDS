"""Unit tests for auth module."""

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from mds.auth import check_entitlements, require_auth


class TestCheckEntitlements:
    def test_wildcard_allows_all(self):
        claims = {"entitlements": {"models": ["*"], "versions": ["*"]}}
        # Should not raise
        check_entitlements(claims, "any-model")

    def test_specific_model_allowed(self):
        claims = {"entitlements": {"models": ["fraud-model", "rec-model"], "versions": ["*"]}}
        check_entitlements(claims, "fraud-model")

    def test_model_not_entitled(self):
        claims = {"entitlements": {"models": ["fraud-model"], "versions": ["*"]}}
        with pytest.raises(HTTPException) as exc:
            check_entitlements(claims, "other-model")
        assert exc.value.status_code == 403

    def test_version_not_entitled(self):
        claims = {"entitlements": {"models": ["*"], "versions": ["1.0", "2.0"]}}
        with pytest.raises(HTTPException) as exc:
            check_entitlements(claims, "model", version="3.0")
        assert exc.value.status_code == 403

    def test_version_wildcard_allows_all(self):
        claims = {"entitlements": {"models": ["*"], "versions": ["*"]}}
        check_entitlements(claims, "model", version="99.0")

    def test_empty_versions_allows_all(self):
        claims = {"entitlements": {"models": ["*"], "versions": []}}
        check_entitlements(claims, "model", version="1.0")

    def test_no_entitlements_denies(self):
        claims = {}
        with pytest.raises(HTTPException) as exc:
            check_entitlements(claims, "model")
        assert exc.value.status_code == 403


class TestRequireAuth:
    def test_missing_bearer_prefix(self):
        with pytest.raises(HTTPException) as exc:
            require_auth("Token abc")
        assert exc.value.status_code == 401

    def test_empty_header(self):
        with pytest.raises(HTTPException) as exc:
            require_auth("")
        assert exc.value.status_code == 401

    def test_invalid_token_format(self):
        with pytest.raises(HTTPException) as exc:
            require_auth("Bearer not.a.jwt")
        assert exc.value.status_code == 401

    def test_missing_issuer(self):
        """A JWT without iss claim should be rejected."""
        import jwt as pyjwt
        from tests.conftest import generate_keypair

        priv, _ = generate_keypair()
        from datetime import datetime, timedelta, timezone

        token = pyjwt.encode(
            {
                "sub": "x",
                "aud": "model-distribution-service",
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            priv,
            algorithm="RS256",
        )
        with pytest.raises(HTTPException) as exc:
            require_auth(f"Bearer {token}")
        assert exc.value.status_code == 401
        assert "iss" in exc.value.detail.lower()

    def test_jwks_fetch_failure(self):
        """When JWKS can't be fetched, should raise 401."""
        import jwt as pyjwt
        from tests.conftest import generate_keypair

        priv, _ = generate_keypair()
        from datetime import datetime, timedelta, timezone

        token = pyjwt.encode(
            {
                "sub": "x",
                "iss": "https://auth.nobody.com",
                "aud": "model-distribution-service",
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            priv,
            algorithm="RS256",
        )
        with patch("mds.auth.get_jwks_key", return_value=None):
            with pytest.raises(HTTPException) as exc:
                require_auth(f"Bearer {token}")
            assert exc.value.status_code == 401

    def test_valid_self_service_token(self, rsa_keypair, valid_token):
        """A valid token with self-service claims returns them directly."""
        _, public_key = rsa_keypair
        with patch("mds.auth.get_jwks_key", return_value=public_key):
            claims = require_auth(f"Bearer {valid_token}")
            assert claims["sub"] == "phonepe-india"
            assert claims["registry_name"] == "phonepe-models-registry"
            assert claims["storage_account"] == "phonepemodelsstorage"
            assert claims["entitlements"]["models"] == ["*"]

    def test_valid_legacy_token(self, rsa_keypair):
        """A valid token without self-service claims falls back to CUSTOMERS lookup."""
        import jwt as pyjwt
        from datetime import datetime, timedelta, timezone

        priv, pub = rsa_keypair
        token = pyjwt.encode(
            {
                "sub": "phonepe-india",
                "iss": "https://auth.phonepe.com",
                "aud": "model-distribution-service",
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            priv,
            algorithm="RS256",
            headers={"kid": "test-kid-1"},
        )
        mock_customers = {
            "phonepe": {
                "issuer": "https://auth.phonepe.com",
                "models": ["*"],
                "registry_name": "phonepe-reg",
                "storage_account": "phonepe-sa",
            }
        }
        with (
            patch("mds.auth.get_jwks_key", return_value=pub),
            patch.dict("mds.customers.CUSTOMERS", mock_customers, clear=True),
        ):
            claims = require_auth(f"Bearer {token}")
            assert claims["registry_name"] == "phonepe-reg"
            assert claims["storage_account"] == "phonepe-sa"
            assert claims["entitlements"]["models"] == ["*"]

    def test_legacy_token_unknown_issuer(self, rsa_keypair):
        """A token without self-service claims AND unknown issuer should be rejected."""
        import jwt as pyjwt
        from datetime import datetime, timedelta, timezone

        priv, pub = rsa_keypair
        token = pyjwt.encode(
            {
                "sub": "x",
                "iss": "https://auth.unknown.com",
                "aud": "model-distribution-service",
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            priv,
            algorithm="RS256",
        )
        with (
            patch("mds.auth.get_jwks_key", return_value=pub),
            patch.dict("mds.customers.CUSTOMERS", {}, clear=True),
        ):
            with pytest.raises(HTTPException) as exc:
                require_auth(f"Bearer {token}")
            assert exc.value.status_code == 401

    def test_expired_token(self, rsa_keypair, expired_token):
        """An expired token should raise 401."""
        _, public_key = rsa_keypair
        with patch("mds.auth.get_jwks_key", return_value=public_key):
            with pytest.raises(HTTPException) as exc:
                require_auth(f"Bearer {expired_token}")
            assert exc.value.status_code == 401
