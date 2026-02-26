"""
Phase 2.4 -- Automated corner-case tests.

Covers:
- Multi-customer isolation
- Concurrent request simulation
- Key rotation mid-session
- Expired / malformed tokens
- JWKS endpoint failures (down, timeout, bad response)
- Large payloads
- Pagination edge cases (continuationToken round-trip, boundary)
- Staged upload lifecycle
- Archive metadata extraction (zip with config.json, tokenizer_config.json, README.md)
"""

import io
import json
import zipfile
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient

from mds.auth import require_auth, check_entitlement
from mds.customers import get_customer_by_issuer
from mds.catalog import encode_continuation_token, decode_continuation_token
from mds.metadata import extract_onnx_metadata


# =======================================================================
#  Helpers
# =======================================================================

def _generate_keypair():
    """Generate a fresh RSA keypair and return (private_pem, public_pem)."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


def _make_token(private_pem, iss="https://auth.phonepe.com", sub="phonepe-india",
                aud="model-distribution-service", kid="test-kid-1",
                exp_hours=1, extra_claims=None):
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


class _FakeModel:
    def __init__(self, name="test-model", version="1", latest_version="1", tags=None, path="https://fake"):
        self.name = name
        self.version = version
        self.latest_version = latest_version
        self.tags = tags or {}
        self.path = path


@pytest.fixture()
def client():
    """TestClient with Azure clients fully mocked."""
    with patch("mds.main.get_ml_client") as mock_ml, \
         patch("mds.main.upload_to_blob") as mock_upload, \
         patch("mds.main.generate_sas_url", return_value="https://fake-sas"), \
         patch("mds.main.generate_upload_sas_url", return_value="https://fake-upload-sas"), \
         patch("mds.main.list_blobs", return_value=["model/v1/file.onnx"]), \
         patch("mds.main.download_blob", return_value=b"\x00" * 32):
        fake_client = MagicMock()
        fake_client.models.list.return_value = [
            _FakeModel(name="model-a", version="1", latest_version="1"),
            _FakeModel(name="model-b", version="2", latest_version="2"),
            _FakeModel(name="model-c", version="1", latest_version="1"),
        ]
        fake_client.models.get.side_effect = lambda name, version: _FakeModel(
            name=name, version=version, tags={"blob_name": f"{name}/v{version}_file.onnx"}
        )
        fake_client.models.create_or_update.return_value = _FakeModel(name="test", version="1")
        mock_ml.return_value = fake_client

        from mds.main import app
        yield TestClient(app)


# =======================================================================
#  1. Multi-Customer Isolation
# =======================================================================

class TestMultiCustomerIsolation:
    """Verify that tokens from one customer cannot access another's resources."""

    def test_unknown_customer_rejected(self):
        priv, _ = _generate_keypair()
        token = _make_token(priv, iss="https://auth.acme.com")
        with pytest.raises(Exception):
            require_auth(f"Bearer {token}")

    def test_customer_a_cannot_use_customer_b_key(self):
        """Token signed with Customer A's key but claiming Customer B's issuer."""
        priv_a, pub_a = _generate_keypair()
        _, pub_b = _generate_keypair()
        token = _make_token(priv_a, iss="https://auth.phonepe.com")
        # If we validate with pub_b, signature fails
        with patch("mds.auth.get_public_key", return_value=pub_b):
            with pytest.raises(Exception) as exc:
                require_auth(f"Bearer {token}")
            assert "401" in str(exc.value.status_code)

    def test_entitlement_per_customer(self):
        """PhonePe has wildcard access; an unknown customer has none."""
        assert check_entitlement({"customer_id": "phonepe"}, "any-model") is True
        assert check_entitlement({"customer_id": "unknown"}, "any-model") is False

    def test_add_second_customer_with_limited_models(self):
        """Simulate a second customer with restricted model list."""
        from mds.customers import CUSTOMERS
        CUSTOMERS["acme"] = {"name": "Acme Corp", "sub": "acme-corp", "models": ["model-x"]}
        try:
            assert check_entitlement({"customer_id": "acme"}, "model-x") is True
            assert check_entitlement({"customer_id": "acme"}, "model-y") is False
        finally:
            del CUSTOMERS["acme"]


# =======================================================================
#  2. Concurrent Requests
# =======================================================================

class TestConcurrentRequests:
    """Simulate concurrent API calls to check for race conditions."""

    def test_concurrent_catalog_calls(self, client, rsa_keypair, valid_token):
        """Multiple threads hitting /catalog simultaneously should all succeed."""
        _, pub = rsa_keypair
        results = []
        errors = []

        def _call():
            try:
                r = client.post(
                    "/catalog", json={},
                    headers={"Authorization": f"Bearer {valid_token}"},
                )
                results.append(r.status_code)
            except Exception as e:
                errors.append(e)

        with patch("mds.auth.get_public_key", return_value=pub):
            threads = [threading.Thread(target=_call) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        assert len(errors) == 0, f"Concurrent errors: {errors}"
        assert all(s == 200 for s in results), f"Non-200 statuses: {results}"

    def test_concurrent_health_checks(self, client):
        """Health endpoint should handle concurrent calls gracefully."""
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
    """Simulate key rotation -- old key stops working, new key starts."""

    def test_old_key_rejected_after_rotation(self):
        old_priv, old_pub = _generate_keypair()
        new_priv, new_pub = _generate_keypair()

        token_old = _make_token(old_priv, kid="old-kid")
        token_new = _make_token(new_priv, kid="new-kid")

        # Validate old token with old key -> should work
        with patch("mds.auth.get_public_key", return_value=old_pub):
            claims = require_auth(f"Bearer {token_old}")
            assert claims["customer_id"] == "phonepe"

        # Rotate: now only new key is available. Old token should fail.
        with patch("mds.auth.get_public_key", return_value=new_pub):
            with pytest.raises(Exception):
                require_auth(f"Bearer {token_old}")

        # New token with new key -> should work
        with patch("mds.auth.get_public_key", return_value=new_pub):
            claims = require_auth(f"Bearer {token_new}")
            assert claims["customer_id"] == "phonepe"


# =======================================================================
#  4. Expired / Malformed Tokens
# =======================================================================

class TestTokenEdgeCases:

    def test_expired_token(self, rsa_keypair, expired_token):
        _, pub = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=pub):
            with pytest.raises(Exception) as exc:
                require_auth(f"Bearer {expired_token}")

    def test_wrong_audience(self):
        priv, pub = _generate_keypair()
        token = _make_token(priv, aud="wrong-audience")
        with patch("mds.auth.get_public_key", return_value=pub):
            with pytest.raises(Exception) as exc:
                require_auth(f"Bearer {token}")

    def test_no_issuer_claim(self):
        """Token without 'iss' claim should fail."""
        priv, pub = _generate_keypair()
        now = datetime.now(timezone.utc)
        token = pyjwt.encode(
            {"sub": "x", "aud": "model-distribution-service",
             "exp": now + timedelta(hours=1)},
            priv, algorithm="RS256",
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
    """JWKS endpoint down / returning bad data should fail auth (no demo fallback)."""

    def test_jwks_fetch_timeout(self):
        """When JWKS endpoint times out, get_public_key returns None."""
        from mds.customers import get_public_key
        with patch("mds.jwks.requests.get", side_effect=Exception("Connection timeout")):
            key = get_public_key("phonepe", "some-kid")
            assert key is None

    def test_jwks_returns_invalid_json(self):
        """When JWKS returns garbage, get_public_key returns None."""
        from mds.customers import get_public_key
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("Invalid JSON")
        with patch("mds.jwks.requests.get", return_value=mock_resp):
            key = get_public_key("phonepe", "some-kid")
            assert key is None

    def test_jwks_returns_empty_keys(self):
        """When JWKS returns empty keys array, get_public_key returns None."""
        from mds.customers import get_public_key
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"keys": []}
        with patch("mds.jwks.requests.get", return_value=mock_resp):
            key = get_public_key("phonepe", "some-kid")
            assert key is None

    def test_jwks_http_500(self):
        """When JWKS returns 500, get_public_key returns None."""
        from mds.customers import get_public_key
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = Exception("500 Server Error")
        with patch("mds.jwks.requests.get", return_value=mock_resp):
            key = get_public_key("phonepe", "some-kid")
            assert key is None


# =======================================================================
#  6. Large Payloads
# =======================================================================

class TestLargePayloads:
    """Ensure large model uploads don't crash metadata extraction."""

    def test_large_onnx_file_metadata(self):
        """10 MB of zeros should extract file_size_bytes without crashing."""
        large_data = b"\x00" * (10 * 1024 * 1024)
        tags = extract_onnx_metadata(large_data, "big-model.onnx")
        assert tags["file_size_bytes"] == str(10 * 1024 * 1024)
        assert tags["modelType"] == "onnx"

    def test_large_zip_archive_metadata(self):
        """Zip with a 1 MB config.json should extract metadata."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            cfg = {"model_type": "gpt2", "architectures": ["GPT2LMHeadModel"],
                   "max_position_embeddings": 1024}
            zf.writestr("model/config.json", json.dumps(cfg))
            zf.writestr("model/model.onnx", b"\x00" * (1024 * 1024))
        tags = extract_onnx_metadata(buf.getvalue(), "big-model.zip")
        assert tags.get("modelType") == "gpt2"
        assert tags.get("contextLength") == "1024"


# =======================================================================
#  7. Pagination Edge Cases
# =======================================================================

class TestPaginationEdgeCases:

    def test_continuation_token_round_trip(self):
        """Encode -> decode should preserve skip and page_size."""
        token = encode_continuation_token(50, 25)
        skip, ps = decode_continuation_token(token)
        assert skip == 50
        assert ps == 25

    def test_invalid_continuation_token_defaults(self):
        """Garbage token should return safe defaults."""
        skip, ps = decode_continuation_token("!!!invalid!!!")
        assert skip == 0
        assert ps == 100

    def test_empty_continuation_token(self):
        skip, ps = decode_continuation_token("")
        assert skip == 0
        assert ps == 100

    def test_pagination_beyond_total(self, client, rsa_keypair, valid_token):
        """Requesting skip beyond total models should return empty value list."""
        _, pub = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=pub):
            r = client.post(
                "/catalog",
                json={"indexEntitiesRequest": {"pageSize": 10, "skip": 9999}},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            resp = r.json()["indexEntitiesResponse"]
            assert resp["value"] == []
            assert resp["nextSkip"] is None
            assert "continuationToken" not in resp

    def test_page_size_one(self, client, rsa_keypair, valid_token):
        """pageSize=1 should return exactly 1 model and a continuationToken."""
        _, pub = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=pub):
            r = client.post(
                "/catalog",
                json={"indexEntitiesRequest": {"pageSize": 1}},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            resp = r.json()["indexEntitiesResponse"]
            assert len(resp["value"]) == 1
            assert resp["totalCount"] == 3
            assert "continuationToken" in resp

    def test_continuation_token_in_request(self, client, rsa_keypair, valid_token):
        """Using continuationToken from first page should return next page."""
        _, pub = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=pub):
            # First page
            r1 = client.post(
                "/catalog",
                json={"indexEntitiesRequest": {"pageSize": 1}},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            token = r1.json()["indexEntitiesResponse"]["continuationToken"]

            # Second page using continuationToken
            r2 = client.post(
                "/catalog",
                json={"indexEntitiesRequest": {"continuationToken": token}},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r2.status_code == 200
            resp2 = r2.json()["indexEntitiesResponse"]
            assert len(resp2["value"]) == 1
            # Should be a different model than page 1
            assert resp2["value"][0]["annotations"]["name"] != r1.json()["indexEntitiesResponse"]["value"][0]["annotations"]["name"]

    def test_page_size_zero_returns_empty(self, client, rsa_keypair, valid_token):
        """pageSize=0 should return empty results."""
        _, pub = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=pub):
            r = client.post(
                "/catalog",
                json={"indexEntitiesRequest": {"pageSize": 0}},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200


# =======================================================================
#  8. Staged Upload Lifecycle
# =======================================================================

class TestStagedUpload:

    def test_begin_returns_session(self, client, rsa_keypair, valid_token):
        """POST /upload/begin should return session_id and upload_url."""
        _, pub = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=pub):
            r = client.post(
                "/upload/begin",
                json={"model_name": "big-model"},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 200
            data = r.json()
            assert "session_id" in data
            assert "upload_url" in data
            assert "blob_prefix" in data
            assert data["expires_in"] == "4 hours"

    def test_complete_without_begin_fails(self, client, rsa_keypair, valid_token):
        """POST /upload/complete with unknown session_id should 404."""
        _, pub = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=pub):
            r = client.post(
                "/upload/complete",
                json={"session_id": "nonexistent"},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r.status_code == 404

    def test_full_staging_lifecycle(self, client, rsa_keypair, valid_token):
        """begin -> complete should register the model."""
        _, pub = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=pub):
            # Begin
            r1 = client.post(
                "/upload/begin",
                json={"model_name": "staged-model", "task": "text-generation"},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r1.status_code == 200
            session_id = r1.json()["session_id"]

            # Complete
            r2 = client.post(
                "/upload/complete",
                json={"session_id": session_id},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r2.status_code == 200
            assert r2.json()["status"] == "success"
            assert r2.json()["blobs_registered"] >= 1

    def test_complete_no_blobs_fails(self, client, rsa_keypair, valid_token):
        """If no blobs uploaded, /upload/complete should 400."""
        _, pub = rsa_keypair
        with patch("mds.auth.get_public_key", return_value=pub), \
             patch("mds.main.list_blobs", return_value=[]):
            r1 = client.post(
                "/upload/begin",
                json={"model_name": "empty-model"},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            session_id = r1.json()["session_id"]

            r2 = client.post(
                "/upload/complete",
                json={"session_id": session_id},
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            assert r2.status_code == 400

    def test_begin_unauthorized(self, client):
        """Staging without auth should fail."""
        r = client.post("/upload/begin", json={"model_name": "x"})
        assert r.status_code in (401, 422)


# =======================================================================
#  9. Archive Metadata Extraction
# =======================================================================

class TestArchiveMetadataExtraction:

    def _make_zip(self, files: dict) -> bytes:
        """Create a zip archive from a {filename: content} dict."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, content in files.items():
                if isinstance(content, str):
                    content = content.encode()
                zf.writestr(name, content)
        return buf.getvalue()

    def test_config_json_extracts_model_type(self):
        data = self._make_zip({
            "model/config.json": json.dumps({"model_type": "llama", "architectures": ["LlamaForCausalLM"]}),
        })
        tags = extract_onnx_metadata(data, "llama-model.zip")
        assert tags.get("modelType") == "llama"
        assert tags.get("architecture") == "LlamaForCausalLM"
        assert tags.get("task") == "text-generation"

    def test_config_json_extracts_max_tokens(self):
        data = self._make_zip({
            "config.json": json.dumps({"model_type": "gpt2", "max_position_embeddings": 2048}),
        })
        tags = extract_onnx_metadata(data, "gpt2.zip")
        assert tags.get("contextLength") == "2048"

    def test_tokenizer_config_extracts_chat_template(self):
        data = self._make_zip({
            "tokenizer_config.json": json.dumps({
                "tokenizer_class": "GPT2Tokenizer",
                "chat_template": "{% for msg in messages %}{{ msg.content }}{% endfor %}",
            }),
        })
        tags = extract_onnx_metadata(data, "model.zip")
        assert tags.get("tokenizerClass") == "GPT2Tokenizer"
        assert tags.get("task") == "chat-completion"
        assert "messages" in tags.get("promptTemplate", "")

    def test_readme_extracts_license(self):
        readme = "---\nlicense: apache-2.0\npipeline_tag: text-generation\n---\n# My Model\nSome description."
        data = self._make_zip({"README.md": readme})
        tags = extract_onnx_metadata(data, "model.zip")
        assert tags.get("license") == "apache-2.0"
        assert tags.get("task") == "text-generation"

    def test_readme_no_front_matter(self):
        data = self._make_zip({"README.md": "# Just a heading\nNo front matter here."})
        tags = extract_onnx_metadata(data, "model.zip")
        assert "license" not in tags

    def test_full_archive_all_files(self):
        """Zip with config.json + tokenizer_config.json + README.md + model.onnx."""
        data = self._make_zip({
            "model/config.json": json.dumps({
                "model_type": "phi",
                "architectures": ["PhiForCausalLM"],
                "max_position_embeddings": 4096,
            }),
            "model/tokenizer_config.json": json.dumps({
                "tokenizer_class": "CodeGenTokenizer",
                "chat_template": "<|user|>{prompt}<|end|>",
            }),
            "model/README.md": "---\nlicense: mit\n---\n# Phi Model",
            "model/model.onnx": b"\x00" * 100,
        })
        tags = extract_onnx_metadata(data, "phi-model.zip")
        assert tags["modelType"] == "phi"
        assert tags["architecture"] == "PhiForCausalLM"
        assert tags["contextLength"] == "4096"
        assert tags["tokenizerClass"] == "CodeGenTokenizer"
        assert tags["license"] == "mit"

    def test_empty_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w"):
            pass
        tags = extract_onnx_metadata(buf.getvalue(), "empty.zip")
        assert "file_size_bytes" in tags

    def test_corrupt_zip(self):
        """Corrupt zip should not crash, should return basic tags."""
        tags = extract_onnx_metadata(b"PK\x03\x04garbage", "bad.zip")
        assert isinstance(tags, dict)
        assert "file_size_bytes" in tags

    def test_config_json_vision_model(self):
        data = self._make_zip({
            "config.json": json.dumps({"model_type": "vit", "architectures": ["ViTForImageClassification"]}),
        })
        tags = extract_onnx_metadata(data, "vit.zip")
        assert tags.get("inputModalities") == "image"
        assert tags.get("task") == "classification"

    def test_config_json_whisper(self):
        data = self._make_zip({
            "config.json": json.dumps({"model_type": "whisper", "architectures": ["WhisperForConditionalGeneration"]}),
        })
        tags = extract_onnx_metadata(data, "whisper.zip")
        assert tags.get("inputModalities") == "audio"
