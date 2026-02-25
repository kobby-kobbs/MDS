"""Unit tests for auth module."""

import pytest
from unittest.mock import patch
from fastapi import HTTPException

from mds.auth import require_auth, check_entitlement
from mds.customers import get_customer_by_issuer


class TestGetCustomerByIssuer:
    def test_known_customer(self):
        assert get_customer_by_issuer("https://auth.phonepe.com") == "phonepe"

    def test_unknown_issuer(self):
        assert get_customer_by_issuer("https://auth.unknown.com") is None

    def test_malformed_issuer(self):
        assert get_customer_by_issuer("not-a-url") is None

    def test_empty_issuer(self):
        assert get_customer_by_issuer("") is None


class TestCheckEntitlement:
    def test_wildcard_allows_all(self):
        claims = {"customer_id": "phonepe"}
        assert check_entitlement(claims, "any-model") is True

    def test_missing_customer_id(self):
        assert check_entitlement({}, "model") is False

    def test_unknown_customer(self):
        assert check_entitlement({"customer_id": "unknown"}, "model") is False


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

    def test_unknown_issuer(self):
        """A structurally valid JWT but with an unknown issuer should be rejected."""
        import jwt as pyjwt
        from datetime import datetime, timedelta, timezone
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        key = rsa.generate_private_key(65537, 2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        token = pyjwt.encode(
            {"sub": "x", "iss": "https://auth.nobody.com", "aud": "model-distribution-service",
             "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
            pem, algorithm="RS256",
        )
        with pytest.raises(HTTPException) as exc:
            require_auth(f"Bearer {token}")
        assert exc.value.status_code == 401

    def test_valid_token(self, rsa_keypair, valid_token):
        """A valid token should return claims with customer_id."""
        _, public_pem = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=public_pem):
            claims = require_auth(f"Bearer {valid_token}")
            assert claims["customer_id"] == "phonepe"
            assert claims["sub"] == "phonepe-india"

    def test_expired_token(self, rsa_keypair, expired_token):
        """An expired token should raise 401."""
        _, public_pem = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=public_pem):
            with pytest.raises(HTTPException) as exc:
                require_auth(f"Bearer {expired_token}")
            assert exc.value.status_code == 401
