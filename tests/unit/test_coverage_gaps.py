"""Tests for previously untested endpoints.

Covers:
- DELETE /models/{name}
- GET /models/{name}/versions
- GET /status
- POST /download with API key auth
- 403 entitlement rejection
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import jwt as pyjwt
import pytest
from conftest import FakeModel
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    """TestClient with Azure clients fully mocked."""
    with (
        patch("mds.main.get_ml_client") as ml,
        patch("mds.main.generate_sas_url", return_value="https://sas"),
        patch("mds.main.list_blobs", return_value=["m/v1/f.onnx"]),
        patch("mds.main.list_blob_prefixes", return_value={"m": ["v1"]}),
        patch("mds.main.delete_blobs", return_value=1),
    ):
        fc = MagicMock()
        fc.models.list.return_value = [
            FakeModel(name="m", tags={"foundryLocal": "true", "task": "chat-completion", "device": "cpu"}),
            FakeModel(
                name="m2",
                version="2",
                latest_version="2",
                tags={"foundryLocal": "true", "task": "text-generation", "device": "gpu"},
            ),
        ]
        fc.models.get.side_effect = lambda name, version=None: FakeModel(
            name=name,
            version=version or "1",
            tags={"blob_name": f"{name}/v1.onnx", "foundryLocal": "true", "task": "chat-completion", "device": "cpu"},
        )
        ml.return_value = fc
        from mds.main import app

        yield TestClient(app), fc


@pytest.fixture()
def restricted_token(rsa_keypair):
    """A token entitled to only 'other-model' (not 'm' or 'restricted')."""
    priv, _ = rsa_keypair
    now = datetime.now(timezone.utc)
    return pyjwt.encode(
        {
            "sub": "phonepe-india",
            "iss": "https://auth.phonepe.com",
            "aud": "model-distribution-service",
            "iat": now,
            "exp": now + timedelta(hours=1),
            "registry_name": "phonepe-models-registry",
            "storage_account": "phonepemodelsstorage",
            "entitlements": {"models": ["other-model"], "versions": ["*"]},
        },
        priv,
        algorithm="RS256",
        headers={"kid": "test-kid-1"},
    )


# ── DELETE /models/{name} ────────────────────────────────────────


class TestDeleteModel:
    def test_rejects_no_auth(self, client):
        tc, _ = client
        r = tc.delete("/models/m")
        assert r.status_code in (401, 422)

    def test_rejects_unauthorized(self, client, rsa_keypair, restricted_token):
        """Customer without entitlement gets 403."""
        tc, _ = client
        _, pub = rsa_keypair
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = tc.delete("/models/m", headers={"Authorization": f"Bearer {restricted_token}"})
            assert r.status_code == 403

    def test_delete_success(self, client, rsa_keypair, valid_token):
        tc, fc = client
        _, pub = rsa_keypair
        fc.models.list.return_value = [FakeModel(name="m", version="1")]
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = tc.delete("/models/m", headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "deleted"
            assert data["model"] == "m"

    def test_delete_not_found(self, client, rsa_keypair, valid_token):
        tc, fc = client
        _, pub = rsa_keypair
        fc.models.list.side_effect = Exception("not found")
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = tc.delete("/models/missing", headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 404


# ── GET /models/{name}/versions ──────────────────────────────────


class TestModelVersions:
    def test_rejects_no_auth(self, client):
        tc, _ = client
        r = tc.get("/models/m/versions")
        assert r.status_code in (401, 422)

    def test_returns_versions(self, client, rsa_keypair, valid_token):
        tc, fc = client
        _, pub = rsa_keypair
        fc.models.list.return_value = [
            FakeModel(name="m", version="1"),
            FakeModel(name="m", version="2"),
        ]
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = tc.get("/models/m/versions", headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 200
            data = r.json()
            assert data["model"] == "m"
            assert set(data["versions"]) == {"1", "2"}

    def test_not_found(self, client, rsa_keypair, valid_token):
        tc, fc = client
        _, pub = rsa_keypair
        fc.models.list.return_value = []
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = tc.get("/models/missing/versions", headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 404

    def test_entitlement_rejection(self, client, rsa_keypair, restricted_token):
        tc, _ = client
        _, pub = rsa_keypair
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = tc.get("/models/m/versions", headers={"Authorization": f"Bearer {restricted_token}"})
            assert r.status_code == 403


# ── GET /status ──────────────────────────────────────────────────


class TestStatus:
    def test_rejects_no_auth(self, client):
        tc, _ = client
        r = tc.get("/status")
        assert r.status_code in (401, 422)

    def test_returns_status(self, client, rsa_keypair, valid_token):
        tc, _ = client
        _, pub = rsa_keypair
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = tc.get("/status", headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "ok"
            assert "uptime" in data
            assert "uptime_seconds" in data
            assert "model_count" in data
            assert "started_at" in data

    def test_model_count_error_handled(self, client, rsa_keypair, valid_token):
        tc, fc = client
        _, pub = rsa_keypair
        fc.models.list.side_effect = Exception("connection failed")
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = tc.get("/status", headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 200
            assert r.json()["model_count"] == -1


# ── POST /download with API key auth ────────────────────────────


class TestDownloadWithApiKey:
    def test_api_key_auth_success(self, client):
        tc, _ = client
        with patch("mds.main.get_customer_by_api_key", return_value="phonepe"):
            r = tc.post("/download?model=m&version=1", headers={"X-API-Key": "test-key"})
            assert r.status_code == 200
            assert "download_url" in r.json()

    def test_api_key_invalid(self, client):
        tc, _ = client
        with patch("mds.main.get_customer_by_api_key", return_value=None):
            r = tc.post("/download?model=m&version=1", headers={"X-API-Key": "bad-key"})
            assert r.status_code == 401

    def test_no_auth_at_all(self, client):
        tc, _ = client
        r = tc.post("/download?model=m&version=1")
        assert r.status_code == 401


# ── 403 Entitlement Rejection via API ────────────────────────────


class TestEntitlementRejection:
    def test_download_403(self, client, rsa_keypair, restricted_token):
        tc, _ = client
        _, pub = rsa_keypair
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = tc.post("/download?model=restricted&version=1", headers={"Authorization": f"Bearer {restricted_token}"})
            assert r.status_code == 403
            assert "Not entitled" in r.json()["detail"]

    def test_model_detail_403(self, client, rsa_keypair, restricted_token):
        tc, _ = client
        _, pub = rsa_keypair
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = tc.get("/models/restricted", headers={"Authorization": f"Bearer {restricted_token}"})
            assert r.status_code == 403
