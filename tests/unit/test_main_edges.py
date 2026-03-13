"""Edge-case tests for main.py -- covers uncovered lines."""

from unittest.mock import MagicMock, patch

import pytest
from conftest import FakeModel
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    with (
        patch("mds.main.get_ml_client") as ml,
        patch("mds.main.generate_sas_url", return_value="https://sas"),
        patch("mds.main.list_blobs", return_value=["m/v1/f.onnx"]),
    ):
        fc = MagicMock()
        fc.models.list.return_value = [FakeModel(name="m")]
        fc.models.get.return_value = FakeModel(tags={"blob_name": "m/v1.onnx"})
        ml.return_value = fc
        from mds.main import _invalidate_model_caches, app

        _invalidate_model_caches()
        yield TestClient(app), fc


# -- download edge cases ----------------------------------------------


class TestDownloadEdges:
    def test_model_not_found_404(self, client, rsa_keypair, valid_token):
        tc, fc = client
        _, pub = rsa_keypair
        fc.models.get.side_effect = Exception("not found")
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = tc.post("/download?model=m&version=1", headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 404

    def test_no_blobs_fallback(self, client, rsa_keypair, valid_token):
        """No blob_name/prefix/files -> returns ML Registry path."""
        tc, fc = client
        _, pub = rsa_keypair
        fc.models.get.return_value = FakeModel(tags={}, path="https://registry/model")
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = tc.post("/download?model=m&version=1", headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 200
            assert r.json()["download_url"] == "https://registry/model"

    def test_blob_prefix_only_resolves_via_list(self, client, rsa_keypair, valid_token):
        """blob_prefix without blob_files -> uses list_blobs."""
        tc, fc = client
        _, pub = rsa_keypair
        fc.models.get.return_value = FakeModel(tags={"blob_prefix": "m/v1"})
        with (
            patch("mds.auth.get_jwks_key", return_value=pub),
            patch("mds.main.list_blobs", return_value=["m/v1/a.onnx", "m/v1/b.json"]),
        ):
            r = tc.post("/download?model=m&version=1", headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 200
            assert r.json()["file_count"] == 2

    def test_empty_blob_list_404(self, client, rsa_keypair, valid_token):
        """blob_prefix set but list_blobs returns [] -> 404."""
        tc, fc = client
        _, pub = rsa_keypair
        fc.models.get.return_value = FakeModel(tags={"blob_prefix": "m/v1"})
        with patch("mds.auth.get_jwks_key", return_value=pub), patch("mds.main.list_blobs", return_value=[]):
            r = tc.post("/download?model=m&version=1", headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 404

    def test_single_blob_no_prefix(self, client, rsa_keypair, valid_token):
        """Single blob_name without prefix -> flat download_url."""
        tc, fc = client
        _, pub = rsa_keypair
        fc.models.get.return_value = FakeModel(tags={"blob_name": "m/v1.onnx"})
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = tc.post("/download?model=m&version=1", headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 200
            assert "download_url" in r.json()
            assert "files" not in r.json()


# -- model detail edge cases ------------------------------------------


class TestModelDetailEdges:
    def test_with_explicit_version(self, client, rsa_keypair, valid_token):
        tc, fc = client
        _, pub = rsa_keypair
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = tc.get("/models/m?version=1", headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 200

    def test_not_found_empty_list(self, client, rsa_keypair, valid_token):
        tc, fc = client
        _, pub = rsa_keypair
        fc.models.list.return_value = []
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = tc.get("/models/missing", headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 404

    def test_not_found_exception(self, client, rsa_keypair, valid_token):
        tc, fc = client
        _, pub = rsa_keypair
        fc.models.list.side_effect = Exception("err")
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = tc.get("/models/m", headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 404


# -- catalog edge cases -----------------------------------------------


class TestCatalogEdges:
    def test_model_get_exception_skipped(self, client, rsa_keypair, valid_token):
        """Exception in ml.models.get during catalog should be silently skipped."""
        tc, fc = client
        _, pub = rsa_keypair
        fc.models.get.side_effect = Exception("broken")
        with patch("mds.auth.get_jwks_key", return_value=pub):
            r = tc.post("/catalog", json={}, headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 200
            assert r.json()["indexEntitiesResponse"]["totalCount"] == 0
