"""
MDS Integration Test Harness
End-to-end: health -> models -> auth -> catalog -> download -> upload -> staged upload

Requires a running MDS server and valid JWT credentials.
The private key used here must correspond to a public key available
at the customer's JWKS endpoint.

Usage:
    1. Set PRIVATE_KEY_PATH env var to your RSA private key PEM file
    2. Start server: uvicorn mds.main:app --reload --port 8000
    3. Run tests:    python -m pytest tests/integration/ -m integration -v
       or manually:  python tests/integration/test_e2e.py
"""

import io
import os
import sys
import zipfile
import requests
import jwt
from datetime import datetime, timedelta, timezone

BASE = os.getenv("MDS_BASE_URL", "http://localhost:8000")

# Load private key from file or env var for token signing.
# In CI, set MDS_TEST_PRIVATE_KEY or MDS_TEST_PRIVATE_KEY_PATH.
_PRIVATE_KEY = None


def _get_private_key() -> str:
    """Load the RSA private key for test token signing."""
    global _PRIVATE_KEY
    if _PRIVATE_KEY:
        return _PRIVATE_KEY

    # Try env var first (inline PEM)
    key_pem = os.getenv("MDS_TEST_PRIVATE_KEY", "")
    if key_pem:
        _PRIVATE_KEY = key_pem
        return _PRIVATE_KEY

    # Try file path
    key_path = os.getenv("MDS_TEST_PRIVATE_KEY_PATH", "")
    if key_path and os.path.exists(key_path):
        with open(key_path) as f:
            _PRIVATE_KEY = f.read()
        return _PRIVATE_KEY

    # Generate ephemeral keypair for local testing (not valid against production JWKS)
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _PRIVATE_KEY = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    print("[WARN]  Using ephemeral key -- auth tests will only pass if server mocks JWKS")
    return _PRIVATE_KEY


def make_token(
    sub: str = "phonepe-india",
    iss: str = "https://auth.phonepe.com",
    aud: str = "model-distribution-service",
):
    now = datetime.now(timezone.utc)
    return jwt.encode({
        "sub": sub,
        "iss": iss,
        "aud": aud,
        "iat": now,
        "exp": now + timedelta(hours=1),
    }, _get_private_key(), algorithm="RS256")


def auth_header():
    return {"Authorization": f"Bearer {make_token()}"}


def run_tests():
    passed = 0
    failed = 0

    def test(name, fn):
        nonlocal passed, failed
        try:
            fn()
            print(f"  [OK] {name}")
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {name}: {e}")
            failed += 1

    # -- 1. Health ------------------------------------------------
    print("\n--- Health ---")

    def t_health():
        r = requests.get(f"{BASE}/health", timeout=5)
        assert r.status_code == 200, f"status {r.status_code}"
        assert r.json()["status"] == "ok"
    test("GET /health", t_health)

    # -- 2. Models (no auth) -------------------------------------
    print("\n--- Models ---")
    models = []

    def t_models():
        nonlocal models
        r = requests.get(f"{BASE}/models", timeout=15)
        assert r.status_code == 200, f"status {r.status_code}"
        models = r.json().get("models", [])
        assert len(models) > 0, "no models in registry"
    test("GET /models", t_models)

    # -- 3. Auth rejection ---------------------------------------
    print("\n--- Auth ---")

    def t_no_token():
        r = requests.post(f"{BASE}/catalog", json={}, timeout=5)
        assert r.status_code in (401, 422), f"expected 401/422, got {r.status_code}"
    test("No token -> rejected", t_no_token)

    def t_bad_token():
        r = requests.post(f"{BASE}/catalog", json={},
                          headers={"Authorization": "Bearer garbage"}, timeout=5)
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
    test("Bad token -> 401", t_bad_token)

    def t_good_token():
        r = requests.post(f"{BASE}/catalog", json={},
                          headers=auth_header(), timeout=15)
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
    test("Valid token -> 200", t_good_token)

    # -- 4. Catalog (Foundry Local format) -----------------------
    print("\n--- Catalog ---")

    def t_catalog():
        r = requests.post(f"{BASE}/catalog",
                          json={"resourceIds": [], "indexEntitiesRequest": {"pageSize": 5}},
                          headers=auth_header(), timeout=30)
        assert r.status_code == 200
        data = r.json()
        resp = data.get("indexEntitiesResponse", {})
        assert "totalCount" in resp, "missing totalCount"
        assert "value" in resp, "missing value"
        if resp["value"]:
            m = resp["value"][0]
            assert "annotations" in m, "missing annotations"
            assert "properties" in m, "missing properties"
    test("POST /catalog format", t_catalog)

    def t_catalog_pagination():
        """Request page size 1, check for continuationToken if >1 model."""
        r = requests.post(f"{BASE}/catalog",
                          json={"indexEntitiesRequest": {"pageSize": 1}},
                          headers=auth_header(), timeout=30)
        assert r.status_code == 200
        resp = r.json()["indexEntitiesResponse"]
        assert len(resp["value"]) <= 1, "page too large"
        if resp["totalCount"] > 1:
            assert "continuationToken" in resp, "missing continuationToken for multi-page"
    test("POST /catalog pagination", t_catalog_pagination)

    # -- 5. Download (single-file) -------------------------------
    print("\n--- Download ---")

    def t_download():
        if not models:
            return
        m = models[0]
        r = requests.post(f"{BASE}/download",
                          params={"model": m["name"], "version": m["latest_version"]},
                          headers=auth_header(), timeout=30)
        assert r.status_code == 200, f"status {r.status_code}: {r.text[:100]}"
        data = r.json()
        # Either single-file or multi-file response
        assert "download_url" in data or "files" in data, "no download_url or files"
    test("POST /download", t_download)

    def t_download_multi_file():
        """If a model has blob_prefix, download should return files array."""
        if not models:
            return
        m = models[0]
        r = requests.post(f"{BASE}/download",
                          params={"model": m["name"], "version": m["latest_version"]},
                          headers=auth_header(), timeout=30)
        assert r.status_code == 200
        data = r.json()
        if "files" in data:
            assert "file_count" in data, "missing file_count"
            assert isinstance(data["files"], list), "files is not a list"
            for entry in data["files"]:
                assert "file" in entry, "missing file key"
                assert "download_url" in entry, "missing download_url in file entry"
    test("POST /download multi-file check", t_download_multi_file)

    # -- 6. Model detail -----------------------------------------
    print("\n--- Model Detail ---")

    def t_model_detail():
        if not models:
            return
        m = models[0]
        r = requests.get(f"{BASE}/models/{m['name']}",
                         headers=auth_header(), timeout=15)
        assert r.status_code == 200, f"status {r.status_code}"
        data = r.json()
        assert "annotations" in data, "missing annotations"
        assert "properties" in data, "missing properties"
    test("GET /models/{name}", t_model_detail)

    # -- 7. Single-file upload -----------------------------------
    print("\n--- Upload ---")

    def t_upload_single():
        r = requests.post(
            f"{BASE}/upload",
            data={"model_name": "e2e-test-model", "task": "classification"},
            files=[("files", ("test.onnx", b"\x00" * 64, "application/octet-stream"))],
            headers=auth_header(),
            timeout=60,
        )
        assert r.status_code == 200, f"status {r.status_code}: {r.text[:200]}"
        data = r.json()
        assert data["status"] == "success"
        assert data["files_uploaded"] == 1
        assert data["total_bytes"] == 64
    test("POST /upload single file", t_upload_single)

    # -- 8. Multi-file upload ------------------------------------

    def t_upload_multi():
        r = requests.post(
            f"{BASE}/upload",
            data={"model_name": "e2e-multi-model", "task": "text-generation"},
            files=[
                ("files", ("model.onnx", b"\x00" * 128, "application/octet-stream")),
                ("files", ("config.json", b'{"model_type":"gpt2"}', "application/json")),
                ("files", ("tokenizer.json", b'{"version":"1.0"}', "application/json")),
            ],
            headers=auth_header(),
            timeout=60,
        )
        assert r.status_code == 200, f"status {r.status_code}: {r.text[:200]}"
        data = r.json()
        assert data["status"] == "success"
        assert data["files_uploaded"] == 3
        assert data["total_bytes"] > 128
    test("POST /upload multi-file", t_upload_multi)

    # -- 9. Zip upload ------------------------------------------
    print("\n--- Zip Upload ---")

    def t_upload_zip():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("my-model/model.onnx", b"\x00" * 64)
            zf.writestr("my-model/config.json", b'{"model_type":"gpt2"}')
        r = requests.post(
            f"{BASE}/upload",
            data={"model_name": "e2e-zip-model", "task": "text-generation"},
            files=[("files", ("my-model.zip", buf.getvalue(), "application/zip"))],
            headers=auth_header(),
            timeout=60,
        )
        assert r.status_code == 200, f"status {r.status_code}: {r.text[:200]}"
        data = r.json()
        assert data["status"] == "success"
        assert data["files_uploaded"] == 2, f"expected 2, got {data['files_uploaded']}"
    test("POST /upload zip archive", t_upload_zip)

    # -- 10. Zip download ----------------------------------------
    print("\n--- Zip Download ---")

    def t_download_zip():
        if not models:
            return
        m = models[0]
        r = requests.post(
            f"{BASE}/download",
            params={"model": m["name"], "version": m["latest_version"], "format": "zip"},
            headers=auth_header(),
            timeout=60,
        )
        assert r.status_code == 200, f"status {r.status_code}: {r.text[:100]}"
        assert "application/zip" in r.headers.get("content-type", ""), "not a zip"
        # Verify the content is a valid zip
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert len(zf.namelist()) >= 1, "zip is empty"
    test("POST /download format=zip", t_download_zip)

    # -- 11. Folder-style upload ---------------------------------

    def t_upload_folders():
        r = requests.post(
            f"{BASE}/upload",
            data={"model_name": "e2e-folder-model"},
            files=[
                ("files", ("weights/model.onnx", b"\x00" * 32, "application/octet-stream")),
                ("files", ("weights/config.json", b'{"type":"gpt"}', "application/json")),
                ("files", ("tokenizer/vocab.txt", b"hello\nworld", "text/plain")),
            ],
            headers=auth_header(),
            timeout=60,
        )
        assert r.status_code == 200, f"status {r.status_code}: {r.text[:200]}"
        assert r.json()["files_uploaded"] == 3
    test("POST /upload folder structure", t_upload_folders)

    # -- 12. Staged upload lifecycle -----------------------------
    print("\n--- Staged Upload ---")

    def t_staged_begin():
        r = requests.post(
            f"{BASE}/upload/begin",
            json={"model_name": "e2e-staged-model"},
            headers=auth_header(),
            timeout=30,
        )
        assert r.status_code == 200, f"status {r.status_code}: {r.text[:200]}"
        data = r.json()
        assert "session_id" in data, "missing session_id"
        assert "upload_url" in data, "missing upload_url"
        assert "blob_prefix" in data, "missing blob_prefix"
    test("POST /upload/begin", t_staged_begin)

    def t_staged_complete():
        r1 = requests.post(
            f"{BASE}/upload/begin",
            json={"model_name": "e2e-staged-complete"},
            headers=auth_header(),
            timeout=30,
        )
        assert r1.status_code == 200
        sid = r1.json()["session_id"]

        r2 = requests.post(
            f"{BASE}/upload/complete",
            json={"session_id": sid},
            headers=auth_header(),
            timeout=60,
        )
        # Might be 200 or 400 depending on whether blobs exist
        assert r2.status_code in (200, 400), f"unexpected status {r2.status_code}"
        if r2.status_code == 200:
            assert r2.json()["status"] == "success"
    test("POST /upload/complete lifecycle", t_staged_complete)

    def t_staged_unknown_session():
        r = requests.post(
            f"{BASE}/upload/complete",
            json={"session_id": "nonexistent-session-id"},
            headers=auth_header(),
            timeout=15,
        )
        assert r.status_code == 404, f"expected 404, got {r.status_code}"
    test("POST /upload/complete unknown session", t_staged_unknown_session)

    # -- Summary -------------------------------------------------
    total = passed + failed
    print(f"\n{'='*40}")
    print(f"  {passed}/{total} passed" + (" [OK]" if failed == 0 else f"  ({failed} failed) [FAIL]"))
    print(f"{'='*40}\n")
    return failed == 0


if __name__ == "__main__":
    try:
        success = run_tests()
        sys.exit(0 if success else 1)
    except requests.ConnectionError:
        print("\n[FAIL] Cannot connect. Is the server running?")
        print("   uvicorn mds.main:app --reload --port 8000")
        sys.exit(1)
