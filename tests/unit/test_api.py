"""Unit tests for API endpoints (FastAPI TestClient, all Azure calls mocked)."""

import os
from unittest.mock import MagicMock, patch

import pytest
from conftest import FakeModel
from fastapi.testclient import TestClient

# -- Fixtures ---------------------------------------------------------


@pytest.fixture()
def client():
    """TestClient with all Azure clients mocked out."""
    with (
        patch("mds.main.get_ml_client") as mock_ml,
        patch("mds.main.generate_sas_url", return_value="https://fake-sas"),
        patch("mds.main.list_blobs", return_value=["model/v1/file.onnx"]),
    ):
        fake_client = MagicMock()
        fake_client.models.list.return_value = [
            FakeModel(name="test-model", version="1", latest_version="1"),
        ]
        fake_client.models.get.return_value = FakeModel(
            tags={"blob_name": "test/v1_model.onnx", "foundryLocal": "true"},
        )
        mock_ml.return_value = fake_client

        from mds.main import _invalidate_model_caches, app

        _invalidate_model_caches()
        yield TestClient(app)


@pytest.fixture()
def multi_file_client():
    """TestClient configured for multi-file model scenarios."""
    multi_blobs = [
        "my-model/v1/model.onnx",
        "my-model/v1/config.json",
        "my-model/v1/tokenizer.json",
    ]
    with (
        patch("mds.main.get_ml_client") as mock_ml,
        patch("mds.main.generate_sas_url", side_effect=lambda b, **kw: f"https://sas/{b}"),
        patch("mds.main.list_blobs", return_value=multi_blobs),
    ):
        fake_client = MagicMock()
        fake_client.models.list.return_value = [
            FakeModel(name="my-model", version="1", latest_version="1"),
        ]
        fake_client.models.get.return_value = FakeModel(
            name="my-model",
            tags={
                "blob_prefix": "my-model/v1",
                "blob_files": ",".join(multi_blobs),
                "blob_count": "3",
            },
        )
        mock_ml.return_value = fake_client

        from mds.main import app

        yield TestClient(app)


# -- Helpers ----------------------------------------------------------

# -- Health -----------------------------------------------------------


class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


# -- List Models ------------------------------------------------------


class TestListModels:
    def test_returns_models(self, client):
        r = client.get("/models")
        assert r.status_code == 200
        models = r.json()["models"]
        assert len(models) == 1
        assert models[0]["name"] == "test-model"


# -- Single-file Download --------------------------------------------


class TestDownload:
    def test_rejects_no_auth(self, client):
        r = client.post("/download?model=m&version=1")
        assert r.status_code in (401, 422)

    def test_rejects_bad_token(self, client):
        r = client.post(
            "/download?model=m&version=1",
            headers={"Authorization": "Bearer bad"},
        )
        assert r.status_code == 401

    def test_download_success(self, client, rsa_keypair, valid_token):
        _, pub = rsa_keypair
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = client.post(
                "/download?model=test-model&version=1",
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            assert "download_url" in r.json()


# -- Multi-file Download ---------------------------------------------


class TestMultiFileDownload:
    def test_returns_files_array(self, multi_file_client, rsa_keypair, valid_token):
        """Multi-file model should return a files[] array with SAS URLs."""
        _, pub = rsa_keypair
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = multi_file_client.post(
                "/download?model=my-model&version=1",
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            data = r.json()
            assert "files" in data
            assert data["file_count"] == 3
            assert len(data["files"]) == 3
            for entry in data["files"]:
                assert "file" in entry
                assert "download_url" in entry
                assert entry["download_url"].startswith("https://sas/")

    def test_single_file_fallback(self, client, rsa_keypair, valid_token):
        """Single-file model should return flat download_url (no files array)."""
        _, pub = rsa_keypair
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = client.post(
                "/download?model=test-model&version=1",
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            data = r.json()
            assert "download_url" in data
            assert "files" not in data


# -- Catalog ----------------------------------------------------------


class TestCatalog:
    def test_catalog_format(self, client, rsa_keypair, valid_token):
        _, pub = rsa_keypair
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = client.post(
                "/catalog",
                json={},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            resp = r.json()["indexEntitiesResponse"]
            assert "totalCount" in resp
            assert "value" in resp


# -- FL Native Catalog (API key auth) ---------------------------------


class TestFLNativeCatalog:
    def test_rejects_no_auth(self, client):
        r = client.post("/catalog/foundrylocal", json={})
        assert r.status_code in (401, 422)

    def test_rejects_bad_api_key(self, client):
        r = client.post(
            "/catalog/foundrylocal",
            json={},
            headers={"X-API-Key": "bad-key-12345"},
        )
        assert r.status_code == 401

    def test_with_jwt_fallback(self, client, rsa_keypair, valid_token):
        """Falls back to JWT auth when no API key is provided."""
        _, pub = rsa_keypair
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = client.post(
                "/catalog/foundrylocal",
                json={},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            resp = r.json()["indexEntitiesResponse"]
            assert "totalCount" in resp
            assert "value" in resp

    def test_with_api_key(self, client, rsa_keypair):
        """API key auth returns indexEntitiesResponse format."""
        with patch("mds.main.get_customer_by_api_key", return_value="phonepe"):
            r = client.post(
                "/catalog/foundrylocal",
                json={},
                headers={"X-API-Key": "test-api-key"},
            )
            assert r.status_code == 200
            body = r.json()
            assert "indexEntitiesResponse" in body
            resp = body["indexEntitiesResponse"]
            assert "totalCount" in resp
            assert "value" in resp
            # Models should have uri field for FL download
            for model in resp["value"]:
                assert "uri" in model["properties"]
                assert model["properties"]["uri"].startswith("azureml://registries/")

    # ── URL-path API key variant ───────────────────────────────────────

    def test_keyed_rejects_bad_key(self, client):
        r = client.post("/catalog/foundrylocal/bad-key-12345", json={})
        assert r.status_code == 401

    def test_keyed_returns_catalog(self, client):
        """URL-path API key variant returns same indexEntitiesResponse."""
        with patch("mds.main.get_customer_by_api_key", return_value="phonepe"):
            r = client.post("/catalog/foundrylocal/test-api-key", json={})
            assert r.status_code == 200
            body = r.json()
            assert "indexEntitiesResponse" in body
            resp = body["indexEntitiesResponse"]
            assert "totalCount" in resp
            assert "value" in resp
            for model in resp["value"]:
                assert "uri" in model["properties"]
                assert model["properties"]["uri"].startswith("azureml://registries/")


# -- Model Detail -----------------------------------------------------


class TestModelDetail:
    def test_rejects_no_auth(self, client):
        r = client.get("/models/test-model")
        assert r.status_code in (401, 422)

    def test_model_detail_success(self, client, rsa_keypair, valid_token):
        _, pub = rsa_keypair
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = client.get(
                "/models/test-model",
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            data = r.json()
            assert "annotations" in data
            assert "properties" in data


# -- Admin Endpoints -----------------------------------------------------


class TestAdminRegister:
    """Tests for POST /admin/register."""

    @pytest.fixture()
    def client(self):
        with (
            patch("mds.main.get_ml_client") as mock_ml,
            patch("mds.main.generate_sas_url", return_value="https://fake-sas"),
            patch("mds.main.list_blobs", return_value=["model/v1/file.onnx"]),
        ):
            fake_client = MagicMock()
            fake_client.models.list.return_value = []
            mock_ml.return_value = fake_client
            from mds.main import app

            yield TestClient(app)

    def test_rejects_no_admin_key(self, client):
        with patch.dict(os.environ, {"MDS_ADMIN_KEY": "secret"}):
            r = client.post(
                "/admin/register",
                json={
                    "customer_name": "test",
                    "issuer": "https://test.com",
                    "registry_name": "r",
                    "storage_account": "s",
                },
            )
            assert r.status_code in (401, 422)

    def test_rejects_bad_admin_key(self, client):
        with patch.dict(os.environ, {"MDS_ADMIN_KEY": "correct-key"}):
            r = client.post(
                "/admin/register",
                json={
                    "customer_name": "t",
                    "issuer": "i",
                    "registry_name": "r",
                    "storage_account": "s",
                },
                headers={"X-Admin-Key": "wrong-key"},
            )
            assert r.status_code == 403

    def test_register_success(self, client):
        with (
            patch.dict(os.environ, {"MDS_ADMIN_KEY": "admin-secret"}),
            patch(
                "mds.customers.register_customer",
                return_value={
                    "customer_id": "acme",
                    "api_key": "generated-key-abc",
                    "jwks_url": "https://acme.com/jwks",
                },
            ),
        ):
            r = client.post(
                "/admin/register",
                json={
                    "customer_name": "Acme",
                    "issuer": "https://acme.com",
                    "models": ["*"],
                    "registry_name": "mds-acme-reg",
                    "storage_account": "mdsacmestor",
                },
                headers={"X-Admin-Key": "admin-secret"},
            )
            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "registered"
            assert data["api_key"] == "generated-key-abc"
            assert "catalog_url" in data

    def test_register_duplicate_409(self, client):
        with (
            patch.dict(os.environ, {"MDS_ADMIN_KEY": "admin-secret"}),
            patch(
                "mds.customers.register_customer",
                side_effect=ValueError("already exists"),
            ),
        ):
            r = client.post(
                "/admin/register",
                json={
                    "customer_name": "dup",
                    "issuer": "i",
                    "registry_name": "r",
                    "storage_account": "s",
                },
                headers={"X-Admin-Key": "admin-secret"},
            )
            assert r.status_code == 409


class TestAdminOffboard:
    """Tests for POST /admin/offboard."""

    @pytest.fixture()
    def client(self):
        with (
            patch("mds.main.get_ml_client") as mock_ml,
            patch("mds.main.generate_sas_url", return_value="https://fake-sas"),
            patch("mds.main.list_blobs", return_value=[]),
        ):
            fake_client = MagicMock()
            mock_ml.return_value = fake_client
            from mds.main import app

            yield TestClient(app)

    def test_offboard_success(self, client):
        with (
            patch.dict(os.environ, {"MDS_ADMIN_KEY": "admin-secret"}),
            patch("mds.customers.remove_customer", return_value=True),
            patch("mds.jwks.invalidate_issuer_cache"),
        ):
            r = client.post(
                "/admin/offboard",
                json={"customer_id": "acme"},
                headers={"X-Admin-Key": "admin-secret"},
            )
            assert r.status_code == 200
            assert r.json()["status"] == "offboarded"

    def test_offboard_not_found(self, client):
        with (
            patch.dict(os.environ, {"MDS_ADMIN_KEY": "admin-secret"}),
            patch("mds.customers.remove_customer", return_value=False),
        ):
            r = client.post(
                "/admin/offboard",
                json={"customer_id": "ghost"},
                headers={"X-Admin-Key": "admin-secret"},
            )
            assert r.status_code == 404


class TestAdminRefreshJwks:
    """Tests for POST /admin/refresh-jwks/{customer_id}."""

    @pytest.fixture()
    def client(self):
        with (
            patch("mds.main.get_ml_client") as mock_ml,
            patch("mds.main.generate_sas_url", return_value="https://fake-sas"),
            patch("mds.main.list_blobs", return_value=[]),
        ):
            fake_client = MagicMock()
            mock_ml.return_value = fake_client
            from mds.main import app

            yield TestClient(app)

    def test_refresh_success(self, client):
        with (
            patch.dict(os.environ, {"MDS_ADMIN_KEY": "admin-secret"}),
            patch("mds.jwks.invalidate_issuer_cache"),
            patch(
                "mds.jwks.fetch_jwks",
                return_value={"k1": "key1", "k2": "key2"},
            ),
        ):
            r = client.post(
                "/admin/refresh-jwks/phonepe",
                headers={"X-Admin-Key": "admin-secret"},
            )
            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "refreshed"
            assert data["keys_loaded"] == 2

    def test_refresh_unknown_customer(self, client):
        with patch.dict(os.environ, {"MDS_ADMIN_KEY": "admin-secret"}):
            r = client.post(
                "/admin/refresh-jwks/unknown",
                headers={"X-Admin-Key": "admin-secret"},
            )
            assert r.status_code == 404
