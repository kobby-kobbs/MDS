"""Shared fixtures for MDS tests."""

import pytest
import jwt as pyjwt
from datetime import datetime, timedelta, timezone
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization


@pytest.fixture(scope="session")
def rsa_keypair():
    """Generate a fresh RSA keypair for tests."""
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


@pytest.fixture()
def valid_token(rsa_keypair):
    """Create a valid JWT for the demo 'phonepe' customer."""
    private_pem, _ = rsa_keypair
    now = datetime.now(timezone.utc)
    return pyjwt.encode(
        {
            "sub": "phonepe-india",
            "iss": "https://auth.phonepe.com",
            "aud": "model-distribution-service",
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        private_pem,
        algorithm="RS256",
        headers={"kid": "test-kid-1"},
    )


@pytest.fixture()
def expired_token(rsa_keypair):
    """Create an expired JWT."""
    private_pem, _ = rsa_keypair
    now = datetime.now(timezone.utc)
    return pyjwt.encode(
        {
            "sub": "phonepe-india",
            "iss": "https://auth.phonepe.com",
            "aud": "model-distribution-service",
            "iat": now - timedelta(hours=2),
            "exp": now - timedelta(hours=1),
        },
        private_pem,
        algorithm="RS256",
        headers={"kid": "test-kid-1"},
    )
