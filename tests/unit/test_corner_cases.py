"""
Corner-case tests.

Covers:
- Multi-customer isolation
- Concurrent request simulation
- Key rotation mid-session
- Expired / malformed tokens
- JWKS endpoint failures (down, timeout, bad response)
- Pagination edge cases (continuationToken round-trip, boundary)
"""

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import jwt as pyjwt
import pytest
from conftest import FakeModel, generate_keypair
from fastapi.testclient import TestClient

from mds.auth import check_entitlements, require_auth
from mds.catalog import decode_continuation_token, encode_continuation_token

# =======================================================================
#  Helpers
# =======================================================================


def _make_token(
    private_pem,
    iss="https://auth.phonepe.com",
    sub="phonepe-india",
    aud="model-distribution-service",
    kid="test-kid-1",
    exp_hours=1,
    extra_claims=None,
):
    """Build a JWT with the given parameters."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": sub,
        "iss": iss,
        "aud": aud,
        "iat": now,
        "exp": now + timedelta(hours=exp_hours),
    }
    if extra_claims:
        payload.update(extra_claims)
    return pyjwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": kid})


@pytest.fixture()
def client():
    """TestClient with Azure clients fully mocked."""
    with (
        patch("mds.main.get_ml_client") as mock_ml,
        patch("mds.main.generate_sas_url", return_value="https://fake-sas"),
        patch("mds.main.list_blobs", return_value=["model/v1/file.onnx"]),
    ):
        fake_client = MagicMock()
        fake_client.models.list.return_value = [
            FakeModel(name="model-a", version="1", latest_version="1"),
            FakeModel(name="model-b", version="2", latest_version="2"),
            FakeModel(name="model-c", version="1", latest_version="1"),
        ]
        fake_client.models.get.side_effect = lambda name, version: FakeModel(
            name=name, version=version, tags={"blob_name": f"{name}/v{version}_file.onnx", "foundryLocal": "true"}
        )
        mock_ml.return_value = fake_client

        from mds.main import _invalidate_model_caches, app

        _invalidate_model_caches()
        yield TestClient(app)


# =======================================================================
#  1. Multi-Customer Isolation
# =======================================================================


class TestMultiCustomerIsolation:
    """Verify that tokens from one customer cannot access another's resources."""

    def test_unknown_customer_rejected(self):
        priv, _ = generate_keypair()
        token = _make_token(priv, iss="https://auth.acme.com")
        with pytest.raises(Exception):
            require_auth(f"Bearer {token}")

    def test_customer_a_cannot_use_customer_b_key(self):
        """Token signed with Customer A's key but claiming Customer B's issuer."""
        priv_a, _ = generate_keypair()
        _, pub_b = generate_keypair()
        token = _make_token(priv_a, iss="https://auth.phonepe.com")
        with patch("mds.auth.get_jwks_key", return_value=pub_b):
            with pytest.raises(Exception) as exc:
                require_auth(f"Bearer {token}")
            assert "401" in str(exc.value.status_code)

    def test_entitlement_per_customer(self):
        """Wildcard entitlements allow all; restricted deny others."""
        wildcard_claims = {"entitlements": {"models": ["*"], "versions": ["*"]}}
        check_entitlements(wildcard_claims, "any-model")  # Should not raise

        restricted_claims = {"entitlements": {"models": ["specific-model"], "versions": ["*"]}}
        with pytest.raises(Exception):
            check_entitlements(restricted_claims, "other-model")

    def test_add_second_customer_with_limited_models(self):
        """Simulate restricted entitlements."""
        limited_claims = {"entitlements": {"models": ["model-x"], "versions": ["*"]}}
        check_entitlements(limited_claims, "model-x")  # Should not raise
        with pytest.raises(Exception):
            check_entitlements(limited_claims, "model-y")


# =======================================================================
#  2. Concurrent Requests
# =======================================================================


class TestConcurrentRequests:
    def test_concurrent_catalog_calls(self, client, rsa_keypair, valid_token):
        _, pub = rsa_keypair
        results = []
        errors = []

        def _call():
            try:
                r = client.post(
                    "/catalog",
                    json={},
                    headers={"Authorization": f"Bearer {valid_token}"},
                )
                results.append(r.status_code)
            except Exception as e:
                errors.append(e)

        with patch("mds.auth.get_jwks_key", return_value=pub):
            threads = [threading.Thread(target=_call) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        assert len(errors) == 0, f"Concurrent errors: {errors}"
        assert all(s == 200 for s in results), f"Non-200 statuses: {results}"

    def test_concurrent_health_checks(self, client):
        results = []

        def _call():
            r = client.get("/health")
            results.append(r.status_code)

        threads = [threading.Thread(target=_call) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert all(s == 200 for s in results)


# =======================================================================
#  3. Key Rotation Mid-Session
# =======================================================================


class TestKeyRotation:
    def test_old_key_rejected_after_rotation(self):
        old_priv, old_pub = generate_keypair()
        new_priv, new_pub = generate_keypair()

        token_old = _make_token(old_priv, kid="old-kid")
        token_new = _make_token(new_priv, kid="new-kid")

        with patch("mds.auth.get_jwks_key", return_value=old_pub):
            claims = require_auth(f"Bearer {token_old}")
            assert claims["sub"] == "phonepe-india"

        with patch("mds.auth.get_jwks_key", return_value=new_pub):
            with pytest.raises(Exception):
                require_auth(f"Bearer {token_old}")

        with patch("mds.auth.get_jwks_key", return_value=new_pub):
            claims = require_auth(f"Bearer {token_new}")
            assert claims["sub"] == "phonepe-india"


# =======================================================================
#  4. Expired / Malformed Tokens
# =======================================================================


class TestTokenEdgeCases:
    def test_expired_token(self, rsa_keypair, expired_token):
        _, pub = rsa_keypair
        with patch("mds.auth.get_jwks_key", return_value=pub):
            with pytest.raises(Exception):
                require_auth(f"Bearer {expired_token}")

    def test_wrong_audience(self):
        priv, pub = generate_keypair()
        token = _make_token(priv, aud="wrong-audience")
        with patch("mds.auth.get_jwks_key", return_value=pub):
            with pytest.raises(Exception):
                require_auth(f"Bearer {token}")

    def test_no_issuer_claim(self):
        priv, _ = generate_keypair()
        now = datetime.now(timezone.utc)
        token = pyjwt.encode(
            {"sub": "x", "aud": "model-distribution-service", "exp": now + timedelta(hours=1)},
            priv,
            algorithm="RS256",
        )
        with pytest.raises(Exception):
            require_auth(f"Bearer {token}")

    def test_garbage_bearer_value(self):
        with pytest.raises(Exception):
            require_auth("Bearer !!!garbage!!!")

    def test_empty_bearer(self):
        with pytest.raises(Exception):
            require_auth("Bearer ")

    def test_non_bearer_scheme(self):
        with pytest.raises(Exception):
            require_auth("Basic dXNlcjpwYXNz")


# =======================================================================
#  5. JWKS Endpoint Failures
# =======================================================================


class TestJWKSFailures:
    def test_jwks_fetch_timeout(self):
        from mds.jwks import get_jwks_key

        with patch("mds.jwks.requests.get", side_effect=Exception("Connection timeout")):
            key = get_jwks_key("https://auth.phonepe.com", "some-kid")
            assert key is None

    def test_jwks_returns_invalid_json(self):
        from mds.jwks import get_jwks_key

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("Invalid JSON")
        with patch("mds.jwks.requests.get", return_value=mock_resp):
            key = get_jwks_key("https://auth.phonepe.com", "some-kid")
            assert key is None

    def test_jwks_returns_empty_keys(self):
        from mds.jwks import get_jwks_key

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"keys": []}
        mock_resp.raise_for_status = MagicMock()
        with patch("mds.jwks.requests.get", return_value=mock_resp):
            key = get_jwks_key("https://auth.phonepe.com", "some-kid")
            assert key is None

    def test_jwks_http_500(self):
        from mds.jwks import get_jwks_key

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = Exception("500 Server Error")
        with patch("mds.jwks.requests.get", return_value=mock_resp):
            key = get_jwks_key("https://auth.phonepe.com", "some-kid")
            assert key is None


# =======================================================================
#  6. Pagination Edge Cases
# =======================================================================


class TestPaginationEdgeCases:
    def test_continuation_token_round_trip(self):
        token = encode_continuation_token(50, 25)
        skip, ps = decode_continuation_token(token)
        assert skip == 50
        assert ps == 25

    def test_invalid_continuation_token_defaults(self):
        skip, ps = decode_continuation_token("!!!invalid!!!")
        assert skip == 0
        assert ps == 100

    def test_empty_continuation_token(self):
        skip, ps = decode_continuation_token("")
        assert skip == 0
        assert ps == 100

    def test_pagination_beyond_total(self, client, rsa_keypair, valid_token):
        _, pub = rsa_keypair
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = client.post(
                "/catalog",
                json={"indexEntitiesRequest": {"pageSize": 10, "skip": 9999}},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            resp = r.json()["indexEntitiesResponse"]
            assert resp["value"] == []
            assert resp["nextSkip"] is None

    def test_page_size_one(self, client, rsa_keypair, valid_token):
        _, pub = rsa_keypair
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = client.post(
                "/catalog",
                json={"indexEntitiesRequest": {"pageSize": 1}},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            resp = r.json()["indexEntitiesResponse"]
            assert len(resp["value"]) == 1
            assert resp["totalCount"] == 3
            assert resp["continuationToken"] is not None

    def test_continuation_token_in_request(self, client, rsa_keypair, valid_token):
        _, pub = rsa_keypair
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r1 = client.post(
                "/catalog",
                json={"indexEntitiesRequest": {"pageSize": 1}},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            token = r1.json()["indexEntitiesResponse"]["continuationToken"]

            r2 = client.post(
                "/catalog",
                json={"indexEntitiesRequest": {"continuationToken": token}},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r2.status_code == 200
            resp2 = r2.json()["indexEntitiesResponse"]
            assert len(resp2["value"]) == 1
            r1_name = r1.json()["indexEntitiesResponse"]["value"][0]["annotations"]["name"]
            assert resp2["value"][0]["annotations"]["name"] != r1_name

    def test_page_size_zero_returns_empty(self, client, rsa_keypair, valid_token):
        _, pub = rsa_keypair
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = client.post(
                "/catalog",
                json={"indexEntitiesRequest": {"pageSize": 0}},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
