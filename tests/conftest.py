"""Shared fixtures for MDS tests."""

from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


class FakeModel:
    """Fake Azure ML Model for testing. Shared across all test files."""

    def __init__(self, name="test-model", version="1", latest_version="1", tags=None, path="https://fake"):
        self.name = name
        self.version = version
        self.latest_version = latest_version
        self.tags = tags or {}
        self.path = path


def generate_keypair():
    """Generate a fresh RSA keypair.

    Returns (private_pem_str, public_key_object) where:
    - private_pem_str: PEM string for signing tokens with PyJWT
    - public_key_object: RSAPublicKey object matching what get_public_key returns
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return private_pem, private_key.public_key()


@pytest.fixture(scope="session")
def rsa_keypair():
    """Generate a fresh RSA keypair for tests.

    Returns (private_pem_str, public_key_object) where:
    - private_pem_str: PEM string for signing tokens with PyJWT
    - public_key_object: RSAPublicKey object matching what get_public_key returns
    """
    return generate_keypair()


@pytest.fixture()
def valid_token(rsa_keypair):
    """Create a valid JWT with self-service claims."""
    private_pem, _ = rsa_keypair
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
            "entitlements": {"models": ["*"], "versions": ["*"]},
        },
        private_pem,
        algorithm="RS256",
        headers={"kid": "test-kid-1"},
    )


@pytest.fixture()
def expired_token(rsa_keypair):
    """Create an expired JWT with self-service claims."""
    private_pem, _ = rsa_keypair
    now = datetime.now(timezone.utc)
    return pyjwt.encode(
        {
            "sub": "phonepe-india",
            "iss": "https://auth.phonepe.com",
            "aud": "model-distribution-service",
            "iat": now - timedelta(hours=2),
            "exp": now - timedelta(hours=1),
            "registry_name": "phonepe-models-registry",
            "storage_account": "phonepemodelsstorage",
            "entitlements": {"models": ["*"], "versions": ["*"]},
        },
        private_pem,
        algorithm="RS256",
        headers={"kid": "test-kid-1"},
    )
