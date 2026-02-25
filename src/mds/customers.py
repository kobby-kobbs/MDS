"""
Customer configuration for MDS.

Each customer entry defines:
    - name:            Human-readable name
    - sub:             Expected JWT 'sub' claim value
    - issuer:          Expected JWT 'iss' claim (used to map tokens -> customers)
    - jwks_url:        JWKS endpoint for public key retrieval
    - models:          List of model names the customer can access ("*" = all)
    - registry_name:   (optional) Dedicated Azure ML Registry for this customer
    - storage_account: (optional) Dedicated Azure Blob Storage account

When registry_name or storage_account are omitted, the shared defaults
from azure_clients.py (REGISTRY_NAME / STORAGE_ACCOUNT) are used.

Add new customers by appending entries to CUSTOMERS below.
In production, this could be loaded from a database or external config.
"""

import logging
import os

log = logging.getLogger("mds")

# Customer Registry
# Add new customers here. Each key is a short customer ID used internally.
#
# Required fields:
#   name     - Display name
#   sub      - Expected 'sub' claim in JWT
#   issuer   - Expected 'iss' claim in JWT (must match exactly)
#   jwks_url - URL to fetch public keys for token verification
#   models   - List of allowed model names, or ["*"] for unrestricted access
#
# Optional fields (per-customer isolation):
#   registry_name   - Dedicated ML Registry (default: shared REGISTRY_NAME env var)
#   storage_account - Dedicated Blob Storage (default: shared STORAGE_ACCOUNT env var)

CUSTOMERS = {
    # Customer: PhonePe
    "phonepe": {
        "name": "PhonePe India",
        "sub": "phonepe-india",
        "issuer": os.getenv("CUSTOMER_PHONEPE_ISSUER", "https://auth.phonepe.com"),
        "jwks_url": os.getenv(
            "CUSTOMER_PHONEPE_JWKS_URL",
            "https://auth.phonepe.com/.well-known/jwks.json",
        ),
        "models": ["*"],  # Full access - restrict when needed
        # Per-customer registry & storage (created by onboard.ps1)
        "registry_name": os.getenv("CUSTOMER_PHONEPE_REGISTRY", ""),
        "storage_account": os.getenv("CUSTOMER_PHONEPE_STORAGE", ""),
    },
    # Add more customers below
    # "acme": {
    #     "name": "Acme Corp",
    #     "sub": "acme-corp",
    #     "issuer": os.getenv("CUSTOMER_ACME_ISSUER", "https://auth.acme.com"),
    #     "jwks_url": os.getenv(
    #         "CUSTOMER_ACME_JWKS_URL",
    #         "https://auth.acme.com/.well-known/jwks.json",
    #     ),
    #     "models": ["model-x", "model-y"],
    #     "registry_name": os.getenv("CUSTOMER_ACME_REGISTRY", ""),
    #     "storage_account": os.getenv("CUSTOMER_ACME_STORAGE", ""),
    # },
}


def get_customer_by_issuer(issuer: str) -> str | None:
    """Look up customer ID by JWT issuer claim.

    Returns the internal customer ID (e.g. 'phonepe') or None if unknown.
    """
    if not issuer:
        return None
    for customer_id, config in CUSTOMERS.items():
        if config.get("issuer") == issuer:
            return customer_id
    return None


def get_customer_registry(customer_id: str) -> str | None:
    """Return the dedicated registry name for a customer, or None to use default."""
    if customer_id and customer_id in CUSTOMERS:
        return CUSTOMERS[customer_id].get("registry_name") or None
    return None


def get_customer_storage(customer_id: str) -> str | None:
    """Return the dedicated storage account for a customer, or None to use default."""
    if customer_id and customer_id in CUSTOMERS:
        return CUSTOMERS[customer_id].get("storage_account") or None
    return None


def get_public_key(customer_id: str = None, kid: str = None):
    """Get public key for a customer.

    Resolution order:
        1. CUSTOMER_<ID>_PUBLIC_KEY env var (PEM string) - fastest, no network
        2. JWKS URL fetch (production path)

    The env-var path is ideal for testing and demo scenarios where we
    generate the keypair ourselves.  In production each customer provides
    a JWKS endpoint and this env var is left unset.
    """
    if not customer_id or customer_id not in CUSTOMERS:
        log.warning(f"Public key requested for unknown customer: {customer_id}")
        return None

    # --- Fast path: PEM from environment variable ---
    env_key = f"CUSTOMER_{customer_id.upper()}_PUBLIC_KEY"
    pem_str = os.getenv(env_key, "").strip()
    if pem_str:
        # Support both literal newlines and escaped \n in env vars
        pem_str = pem_str.replace("\\n", "\n")
        log.info(f"Using public key from env var {env_key} for {customer_id}")
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        try:
            return load_pem_public_key(pem_str.encode())
        except Exception as exc:
            log.error(f"Failed to load PEM from {env_key}: {exc}")
            # Fall through to JWKS

    # --- Standard path: JWKS URL ---
    customer = CUSTOMERS[customer_id]
    jwks_url = customer.get("jwks_url")
    if not jwks_url:
        log.warning(f"No JWKS URL configured for customer: {customer_id}")
        return None

    from .jwks import get_jwks_key

    return get_jwks_key(customer_id, jwks_url, kid)
