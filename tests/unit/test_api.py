"""Unit tests for API endpoints (FastAPI TestClient, all Azure calls mocked)."""

import io
import zipfile
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


# -- Fake Azure ML model for mocking ---------------------------------

class _FakeModel:
    def __init__(
        self,
        name="test-model",
        version="1",
        latest_version="1",
        tags=None,
        path="https://fake",
    ):
        self.name = name
        self.version = version
        self.latest_version = latest_version
        self.tags = tags or {}
        self.path = path


# -- Fixtures ---------------------------------------------------------

@pytest.fixture()
def client():
    """TestClient with all Azure clients mocked out."""
    with patch("mds.main.get_ml_client") as mock_ml, \
         patch("mds.main.upload_to_blob") as mock_upload, \
         patch("mds.main.generate_sas_url", return_value="https://fake-sas"), \
         patch("mds.main.generate_upload_sas_url", return_value="https://fake-upload-sas"), \
         patch("mds.main.list_blobs", return_value=["model/v1/file.onnx"]), \
         patch("mds.main.download_blob", return_value=b"\x00" * 64):
        fake_client = MagicMock()
        fake_client.models.list.return_value = [
            _FakeModel(name="test-model", version="1", latest_version="1"),
        ]
        fake_client.models.get.return_value = _FakeModel(
            tags={"blob_name": "test/v1_model.onnx"},
        )
        fake_client.models.create_or_update.return_value = _FakeModel(version="1")
        mock_ml.return_value = fake_client

        from mds.main import app
        yield TestClient(app)


@pytest.fixture()
def multi_file_client():
    """TestClient configured for multi-file model scenarios."""
    multi_blobs = [
        "my-model/v1/model.onnx",
        "my-model/v1/config.json",
        "my-model/v1/tokenizer.json",
    ]
    with patch("mds.main.get_ml_client") as mock_ml, \
         patch("mds.main.upload_to_blob") as mock_upload, \
         patch("mds.main.generate_sas_url", side_effect=lambda b, **kw: f"https://sas/{b}"), \
         patch("mds.main.generate_upload_sas_url", return_value="https://fake-upload-sas"), \
         patch("mds.main.list_blobs", return_value=multi_blobs), \
         patch("mds.main.download_blob", return_value=b"\x00" * 32):
        fake_client = MagicMock()
        fake_client.models.list.return_value = [
            _FakeModel(name="my-model", version="1", latest_version="1"),
        ]
        fake_client.models.get.return_value = _FakeModel(
            name="my-model",
            tags={
                "blob_prefix": "my-model/v1",
                "blob_files": ",".join(multi_blobs),
                "blob_count": "3",
            },
        )
        fake_client.models.create_or_update.return_value = _FakeModel(version="1")
        mock_ml.return_value = fake_client

        from mds.main import app
        yield TestClient(app)


# -- Helpers ----------------------------------------------------------

def _make_zip(file_map: dict[str, bytes]) -> bytes:
    """Create an in-memory zip archive from {path: content} dict."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in file_map.items():
            zf.writestr(name, data)
    return buf.getvalue()


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
        with patch("mds.auth.get_public_key", return_value=pub):
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
        with patch("mds.auth.get_public_key", return_value=pub):
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
        with patch("mds.auth.get_public_key", return_value=pub):
            r = client.post(
                "/download?model=test-model&version=1",
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            data = r.json()
            assert "download_url" in data
            assert "files" not in data


# -- Zip Download -----------------------------------------------------

class TestZipDownload:
    def test_zip_download_returns_archive(self, multi_file_client, rsa_keypair, valid_token):
        """format=zip should return a zip file."""
        _, pub = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=pub):
            r = multi_file_client.post(
                "/download?model=my-model&version=1&format=zip",
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            assert "application/zip" in r.headers.get("content-type", "")
            assert "attachment" in r.headers.get("content-disposition", "")
            # Verify it's a valid zip
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            names = zf.namelist()
            assert len(names) == 3

    def test_zip_download_single_file(self, client, rsa_keypair, valid_token):
        """format=zip on a single-file model should also return a zip."""
        _, pub = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=pub):
            r = client.post(
                "/download?model=test-model&version=1&format=zip",
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            assert "application/zip" in r.headers.get("content-type", "")


# -- Catalog ----------------------------------------------------------

class TestCatalog:
    def test_catalog_format(self, client, rsa_keypair, valid_token):
        _, pub = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=pub):
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
        with patch("mds.auth.get_public_key", return_value=pub):
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
        with patch("mds.auth.get_public_key", return_value=pub):
            r = client.get(
                "/models/test-model",
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            data = r.json()
            assert "annotations" in data
            assert "properties" in data


# -- Single-file Upload ----------------------------------------------

class TestSingleFileUpload:
    def test_upload_single_file(self, client, rsa_keypair, valid_token):
        _, pub = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=pub):
            r = client.post(
                "/upload",
                data={"model_name": "new-model", "task": "classification"},
                files=[("files", ("model.onnx", b"\x00" * 100, "application/octet-stream"))],
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "success"
            assert data["files_uploaded"] == 1
            assert data["total_bytes"] == 100

    def test_upload_rejects_no_auth(self, client):
        r = client.post(
            "/upload",
            data={"model_name": "x"},
            files=[("files", ("f.onnx", b"\x00", "application/octet-stream"))],
        )
        assert r.status_code in (401, 422)


# -- Multi-file Upload -----------------------------------------------

class TestMultiFileUpload:
    def test_upload_multiple_files(self, client, rsa_keypair, valid_token):
        """Uploading multiple files should succeed and report file count."""
        _, pub = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=pub):
            r = client.post(
                "/upload",
                data={"model_name": "multi-model", "task": "text-generation"},
                files=[
                    ("files", ("model.onnx", b"\x00" * 200, "application/octet-stream")),
                    ("files", ("config.json", b'{"model_type":"gpt2"}', "application/json")),
                    ("files", ("tokenizer.json", b'{"tokenizer_class":"GPT2"}', "application/json")),
                ],
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "success"
            assert data["files_uploaded"] == 3

    def test_upload_folder_structure(self, client, rsa_keypair, valid_token):
        """Files with path separators should preserve directory structure."""
        _, pub = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=pub):
            r = client.post(
                "/upload",
                data={"model_name": "folder-model"},
                files=[
                    ("files", ("weights/model.onnx", b"\x00" * 50, "application/octet-stream")),
                    ("files", ("weights/config.json", b'{}', "application/json")),
                    ("files", ("tokenizer/vocab.txt", b"hello\nworld", "text/plain")),
                ],
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            assert r.json()["files_uploaded"] == 3


# -- Zip Upload -------------------------------------------------------

class TestZipUpload:
    def test_upload_zip_archive(self, client, rsa_keypair, valid_token):
        """Uploading a zip should auto-extract and store individual files."""
        _, pub = rsa_keypair
        zip_content = _make_zip({
            "my-model/model.onnx": b"\x00" * 100,
            "my-model/config.json": b'{"model_type":"gpt2"}',
        })
        with patch("mds.auth.get_public_key", return_value=pub):
            r = client.post(
                "/upload",
                data={"model_name": "zip-model"},
                files=[("files", ("my-model.zip", zip_content, "application/zip"))],
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "success"
            # Zip had 2 files inside, so 2 should be uploaded
            assert data["files_uploaded"] == 2

    def test_upload_zip_strips_common_prefix(self, client, rsa_keypair, valid_token):
        """Common directory prefix inside zip should be stripped."""
        _, pub = rsa_keypair
        zip_content = _make_zip({
            "root/sub/a.txt": b"aaa",
            "root/sub/b.txt": b"bbb",
        })
        with patch("mds.auth.get_public_key", return_value=pub):
            r = client.post(
                "/upload",
                data={"model_name": "zip-prefix-model"},
                files=[("files", ("archive.zip", zip_content, "application/zip"))],
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            assert r.json()["files_uploaded"] == 2

    def test_upload_zip_skips_hidden_files(self, client, rsa_keypair, valid_token):
        """Hidden files (.__MACOSX, .DS_Store) in zip should be skipped."""
        _, pub = rsa_keypair
        zip_content = _make_zip({
            "model/weights.onnx": b"\x00" * 50,
            "model/.DS_Store": b"junk",
            "__MACOSX/model/._weights.onnx": b"junk",
        })
        with patch("mds.auth.get_public_key", return_value=pub):
            r = client.post(
                "/upload",
                data={"model_name": "zip-hidden-model"},
                files=[("files", ("model.zip", zip_content, "application/zip"))],
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            # Only weights.onnx should be extracted (hidden files skipped)
            assert r.json()["files_uploaded"] == 1

    def test_upload_mixed_zip_and_files(self, client, rsa_keypair, valid_token):
        """Upload a zip + a plain file together in one request."""
        _, pub = rsa_keypair
        zip_content = _make_zip({
            "model/model.onnx": b"\x00" * 50,
        })
        with patch("mds.auth.get_public_key", return_value=pub):
            r = client.post(
                "/upload",
                data={"model_name": "mixed-model"},
                files=[
                    ("files", ("model.zip", zip_content, "application/zip")),
                    ("files", ("extra_config.json", b'{"key":"val"}', "application/json")),
                ],
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            # 1 from zip + 1 plain = 2
            assert r.json()["files_uploaded"] == 2


# -- Staged Upload (begin / complete) --------------------------------

class TestStagedUpload:
    def test_begin_returns_session(self, client, rsa_keypair, valid_token):
        _, pub = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=pub):
            r = client.post(
                "/upload/begin",
                json={"model_name": "staged-model"},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            data = r.json()
            assert "session_id" in data
            assert "upload_url" in data
            assert "blob_prefix" in data

    def test_begin_complete_lifecycle(self, client, rsa_keypair, valid_token):
        _, pub = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=pub):
            r1 = client.post(
                "/upload/begin",
                json={"model_name": "lifecycle-model"},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            sid = r1.json()["session_id"]

            r2 = client.post(
                "/upload/complete",
                json={"session_id": sid},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r2.status_code == 200
            assert r2.json()["status"] == "success"
            assert r2.json()["blobs_registered"] >= 1

    def test_complete_unknown_session(self, client, rsa_keypair, valid_token):
        _, pub = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=pub):
            r = client.post(
                "/upload/complete",
                json={"session_id": "does-not-exist"},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 404
