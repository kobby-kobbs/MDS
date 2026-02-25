"""Unit tests for azure_clients — all Azure SDK calls mocked."""

import pytest
from unittest.mock import patch, MagicMock

from mds.azure_clients import (
    get_ml_client, get_blob_client,
    upload_to_blob, generate_sas_url, generate_upload_sas_url,
    list_blobs, download_blob,
    _ml_clients, _blob_clients,
    REGISTRY_NAME, STORAGE_ACCOUNT, STORAGE_CONTAINER,
)


@pytest.fixture(autouse=True)
def _clear_caches():
    _ml_clients.clear()
    _blob_clients.clear()
    yield
    _ml_clients.clear()
    _blob_clients.clear()


class TestGetMlClient:
    @patch("mds.azure_clients.DefaultAzureCredential")
    @patch("mds.azure_clients.MLClient")
    def test_default_registry(self, ml, cred):
        get_ml_client()
        ml.assert_called_once_with(credential=cred.return_value, registry_name=REGISTRY_NAME)

    @patch("mds.azure_clients.DefaultAzureCredential")
    @patch("mds.azure_clients.MLClient")
    def test_custom_registry(self, ml, cred):
        get_ml_client("custom-reg")
        ml.assert_called_once_with(credential=cred.return_value, registry_name="custom-reg")

    @patch("mds.azure_clients.DefaultAzureCredential")
    @patch("mds.azure_clients.MLClient")
    def test_caches(self, ml, cred):
        a = get_ml_client()
        b = get_ml_client()
        assert a is b
        assert ml.call_count == 1


class TestGetBlobClient:
    @patch("mds.azure_clients.DefaultAzureCredential")
    @patch("mds.azure_clients.BlobServiceClient")
    def test_default_account(self, blob, cred):
        get_blob_client()
        blob.assert_called_once()
        assert STORAGE_ACCOUNT in blob.call_args[0][0]

    @patch("mds.azure_clients.DefaultAzureCredential")
    @patch("mds.azure_clients.BlobServiceClient")
    def test_custom_account(self, blob, cred):
        get_blob_client("acme")
        assert "acme" in blob.call_args[0][0]

    @patch("mds.azure_clients.DefaultAzureCredential")
    @patch("mds.azure_clients.BlobServiceClient")
    def test_caches(self, blob, cred):
        a = get_blob_client()
        b = get_blob_client()
        assert a is b


class TestUploadToBlob:
    @patch("mds.azure_clients.get_blob_client")
    def test_returns_url(self, gc):
        url = upload_to_blob(b"data", "m/v1/f.onnx")
        assert STORAGE_ACCOUNT in url and "f.onnx" in url

    @patch("mds.azure_clients.get_blob_client")
    def test_container_already_exists(self, gc):
        gc.return_value.get_container_client.return_value.create_container.side_effect = Exception
        url = upload_to_blob(b"x", "blob")
        assert isinstance(url, str)


class TestGenerateSasUrl:
    @patch("mds.azure_clients.generate_blob_sas", return_value="sig=1")
    @patch("mds.azure_clients.get_blob_client")
    def test_returns_sas_url(self, gc, gen):
        gc.return_value.get_user_delegation_key.return_value = MagicMock()
        url = generate_sas_url("m/f.onnx")
        assert "sig=1" in url and STORAGE_ACCOUNT in url

    @patch("mds.azure_clients.generate_blob_sas", return_value="sig=2")
    @patch("mds.azure_clients.get_blob_client")
    def test_custom_storage(self, gc, gen):
        gc.return_value.get_user_delegation_key.return_value = MagicMock()
        assert "acme" in generate_sas_url("f", storage_account="acme")


class TestGenerateUploadSasUrl:
    @patch("mds.azure_clients.generate_container_sas", return_value="sig=u")
    @patch("mds.azure_clients.get_blob_client")
    def test_returns_url(self, gc, gen):
        gc.return_value.get_user_delegation_key.return_value = MagicMock()
        url = generate_upload_sas_url("prefix/v1")
        assert "sig=u" in url
        assert "prefix/v1" in url


class TestListBlobs:
    @patch("mds.azure_clients.get_blob_client")
    def test_lists(self, gc):
        b1, b2 = MagicMock(), MagicMock()
        b1.name, b2.name = "a.onnx", "b.onnx"
        gc.return_value.get_container_client.return_value.list_blobs.return_value = [b1, b2]
        assert list_blobs("pfx/") == ["a.onnx", "b.onnx"]


class TestDownloadBlob:
    @patch("mds.azure_clients.get_blob_client")
    def test_downloads(self, gc):
        bc = gc.return_value.get_container_client.return_value.get_blob_client.return_value
        bc.download_blob.return_value.readall.return_value = b"model"
        assert download_blob("f.onnx") == b"model"
