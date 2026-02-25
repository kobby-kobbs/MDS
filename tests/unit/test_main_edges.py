"""Edge-case tests for main.py -- covers uncovered lines."""

import io
import tarfile
import zipfile
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


class _FM:
    """Fake model."""
    def __init__(self, name="m", version="1", latest_version="1", tags=None, path="https://x"):
        self.name, self.version, self.latest_version = name, version, latest_version
        self.tags, self.path = tags or {}, path


def _make_tar_gz(file_map: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in file_map.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf.getvalue()


def _make_zip(file_map: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, d in file_map.items():
            zf.writestr(n, d)
    return buf.getvalue()


@pytest.fixture()
def client():
    with patch("mds.main.get_ml_client") as ml, \
         patch("mds.main.upload_to_blob"), \
         patch("mds.main.generate_sas_url", return_value="https://sas"), \
         patch("mds.main.generate_upload_sas_url", return_value="https://up"), \
         patch("mds.main.list_blobs", return_value=["m/v1/f.onnx"]), \
         patch("mds.main.download_blob", return_value=b"\x00" * 16):
        fc = MagicMock()
        fc.models.list.return_value = [_FM(name="m")]
        fc.models.get.return_value = _FM(tags={"blob_name": "m/v1.onnx"})
        fc.models.create_or_update.return_value = _FM(version="1")
        ml.return_value = fc
        from mds.main import app
        yield TestClient(app), fc


# -- download edge cases ----------------------------------------------

class TestDownloadEdges:
    def test_model_not_found_404(self, client, rsa_keypair, valid_token):
        tc, fc = client
        _, pub = rsa_keypair
        fc.models.get.side_effect = Exception("not found")
        with patch("mds.auth.get_public_key", return_value=pub):
            r = tc.post("/download?model=m&version=1",
                        headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 404

    def test_no_blobs_fallback(self, client, rsa_keypair, valid_token):
        """No blob_name/prefix/files -> returns ML Registry path."""
        tc, fc = client
        _, pub = rsa_keypair
        fc.models.get.return_value = _FM(tags={}, path="https://registry/model")
        with patch("mds.auth.get_public_key", return_value=pub):
            r = tc.post("/download?model=m&version=1",
                        headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 200
            assert r.json()["download_url"] == "https://registry/model"

    def test_blob_prefix_only_resolves_via_list(self, client, rsa_keypair, valid_token):
        """blob_prefix without blob_files -> uses list_blobs."""
        tc, fc = client
        _, pub = rsa_keypair
        fc.models.get.return_value = _FM(tags={"blob_prefix": "m/v1"})
        with patch("mds.auth.get_public_key", return_value=pub), \
             patch("mds.main.list_blobs", return_value=["m/v1/a.onnx", "m/v1/b.json"]):
            r = tc.post("/download?model=m&version=1",
                        headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 200
            assert r.json()["file_count"] == 2

    def test_empty_blob_list_404(self, client, rsa_keypair, valid_token):
        """blob_prefix set but list_blobs returns [] -> 404."""
        tc, fc = client
        _, pub = rsa_keypair
        fc.models.get.return_value = _FM(tags={"blob_prefix": "m/v1"})
        with patch("mds.auth.get_public_key", return_value=pub), \
             patch("mds.main.list_blobs", return_value=[]):
            r = tc.post("/download?model=m&version=1",
                        headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 404

    def test_single_blob_no_prefix(self, client, rsa_keypair, valid_token):
        """Single blob_name without prefix -> flat download_url."""
        tc, fc = client
        _, pub = rsa_keypair
        fc.models.get.return_value = _FM(tags={"blob_name": "m/v1.onnx"})
        with patch("mds.auth.get_public_key", return_value=pub):
            r = tc.post("/download?model=m&version=1",
                        headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 200
            assert "download_url" in r.json()
            assert "files" not in r.json()


# -- model detail edge cases ------------------------------------------

class TestModelDetailEdges:
    def test_with_explicit_version(self, client, rsa_keypair, valid_token):
        tc, fc = client
        _, pub = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=pub):
            r = tc.get("/models/m?version=1",
                       headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 200

    def test_not_found_empty_list(self, client, rsa_keypair, valid_token):
        tc, fc = client
        _, pub = rsa_keypair
        fc.models.list.return_value = []
        with patch("mds.auth.get_public_key", return_value=pub):
            r = tc.get("/models/missing",
                       headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 404

    def test_not_found_exception(self, client, rsa_keypair, valid_token):
        tc, fc = client
        _, pub = rsa_keypair
        fc.models.list.side_effect = Exception("err")
        with patch("mds.auth.get_public_key", return_value=pub):
            r = tc.get("/models/m",
                       headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 404


# -- catalog edge cases -----------------------------------------------

class TestCatalogEdges:
    def test_model_get_exception_skipped(self, client, rsa_keypair, valid_token):
        """Exception in ml.models.get during catalog should be silently skipped."""
        tc, fc = client
        _, pub = rsa_keypair
        fc.models.get.side_effect = Exception("broken")
        with patch("mds.auth.get_public_key", return_value=pub):
            r = tc.post("/catalog", json={},
                        headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 200
            assert r.json()["indexEntitiesResponse"]["totalCount"] == 0


# -- upload: tar.gz extraction ----------------------------------------

class TestTarGzUpload:
    def test_upload_tar_gz(self, client, rsa_keypair, valid_token):
        tc, fc = client
        _, pub = rsa_keypair
        tar = _make_tar_gz({"model/f.onnx": b"\x00" * 30, "model/config.json": b"{}"})
        with patch("mds.auth.get_public_key", return_value=pub):
            r = tc.post(
                "/upload",
                data={"model_name": "tar-model"},
                files=[("files", ("model.tar.gz", tar, "application/gzip"))],
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            assert r.json()["files_uploaded"] == 2

    def test_upload_empty_archive_stored_as_is(self, client, rsa_keypair, valid_token):
        """Empty zip -> stored as a single blob."""
        tc, fc = client
        _, pub = rsa_keypair
        empty_zip = _make_zip({})
        with patch("mds.auth.get_public_key", return_value=pub):
            r = tc.post(
                "/upload",
                data={"model_name": "empty-zip-model"},
                files=[("files", ("empty.zip", empty_zip, "application/zip"))],
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            assert r.json()["files_uploaded"] == 1  # stored as-is

    def test_upload_not_entitled_403(self, client, rsa_keypair, valid_token):
        tc, _ = client
        _, pub = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=pub), \
             patch("mds.main.check_entitlement", return_value=False):
            r = tc.post(
                "/upload",
                data={"model_name": "x"},
                files=[("files", ("f.bin", b"x", "application/octet-stream"))],
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 403


# -- staged upload edge cases -----------------------------------------

class TestStagedEdges:
    def test_complete_wrong_customer_403(self, client, rsa_keypair, valid_token):
        tc, _ = client
        _, pub = rsa_keypair
        # Inject a session owned by a different customer
        from mds.main import _save_session, _delete_session
        _save_session("test-sid", {
            "customer_id": "other-customer",
            "storage_account": None,
            "model_name": "m",
            "description": "",
            "next_version": 1,
            "blob_prefix": "m/v1",
            "metadata": {},
            "created": "2025-01-01",
        })
        with patch("mds.auth.get_public_key", return_value=pub):
            r = tc.post("/upload/complete", json={"session_id": "test-sid"},
                        headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 403
        _delete_session("test-sid")

    def test_complete_no_blobs_400(self, client, rsa_keypair, valid_token):
        tc, _ = client
        _, pub = rsa_keypair
        from mds.main import _save_session, _delete_session
        _save_session("sid2", {
            "customer_id": "phonepe",
            "storage_account": None,
            "model_name": "m",
            "description": "",
            "next_version": 1,
            "blob_prefix": "m/v1",
            "metadata": {"alias": "m", "task": "custom", "inputModalities": "text",
                         "outputModalities": "text", "device": "cpu",
                         "executionProvider": "cpuexecutionprovider", "modelType": "onnx"},
            "created": "2025-01-01",
        })
        with patch("mds.auth.get_public_key", return_value=pub), \
             patch("mds.main.list_blobs", return_value=[]):
            r = tc.post("/upload/complete", json={"session_id": "sid2"},
                        headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 400
        _delete_session("sid2")

    def test_begin_not_entitled_403(self, client, rsa_keypair, valid_token):
        tc, _ = client
        _, pub = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=pub), \
             patch("mds.main.check_entitlement", return_value=False):
            r = tc.post("/upload/begin", json={"model_name": "x"},
                        headers={"Authorization": f"Bearer {valid_token}"})
            assert r.status_code == 403
