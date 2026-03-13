"""
JWT authentication and entitlement checks.

Supports two flows:
1. **Self-service JWT**: Token's ``iss`` claim derives the JWKS URL automatically.
   Token carries ``registry_name``, ``storage_account``, ``entitlements`` claims.
   No pre-registration needed.
2. **Legacy JWT**: Token is verified via issuer-derived JWKS, but customer-specific
   config (registry, storage, models) comes from the CUSTOMERS dict.
"""

import json
import logging

import jwt
from fastapi import HTTPException

from .jwks import get_jwks_key

log = logging.getLogger("mds")

# Claims that mark a token as self-service (registry + storage must be present)
_SELF_SERVICE_REQUIRED = {"registry_name", "storage_account"}


def require_auth(authorization: str) -> dict:
    """Validate JWT token, return unified claims dict. Raises 401 on failure.

    Returns a dict that always contains:
        - ``registry_name``: Azure ML Registry name
        - ``storage_account``: Azure Blob Storage account
        - ``entitlements``: ``{"models": [...], "versions": [...]}``
        - all standard JWT claims (``iss``, ``sub``, ``aud``, ``exp``, etc.)
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Invalid authorization header")

    token = authorization[7:]

    try:
        unverified_header = jwt.get_unverified_header(token)
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.DecodeError:
        raise HTTPException(401, "Invalid token format")

    issuer = unverified.get("iss")
    if not issuer:
        raise HTTPException(401, "Token missing 'iss' claim")

    kid = unverified_header.get("kid")
    public_key = get_jwks_key(issuer, kid)
    if not public_key:
        raise HTTPException(401, "Unable to resolve signing key")

    try:
        claims = jwt.decode(
            token,
            public_key,
            algorithms=["RS256", "RS384", "RS512"],
            audience="model-distribution-service",
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.InvalidSignatureError:
        raise HTTPException(401, "Invalid signature")
    except jwt.InvalidAudienceError:
        raise HTTPException(401, "Invalid audience")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")

    # Self-service path: token carries registry + storage claims
    if _SELF_SERVICE_REQUIRED.issubset(claims):
        # Normalise entitlements: check plain and namespaced keys; may be
        # a JSON string, dict, or absent entirely.
        _NS = "https://mds.microsoft.com/entitlements"
        ent = claims.get("entitlements") or claims.get(_NS)
        if isinstance(ent, str):
            try:
                ent = json.loads(ent)
            except (json.JSONDecodeError, TypeError):
                ent = None
        if not isinstance(ent, dict):
            ent = {"models": ["*"], "versions": ["*"]}
        claims["entitlements"] = ent
        log.info(f"OK: Auth (self-service) | iss={issuer}")
        return claims

    # Legacy path: look up customer config by issuer
    from .customers import CUSTOMERS

    customer_id = None
    for cid, config in CUSTOMERS.items():
        if config.get("issuer") == issuer:
            customer_id = cid
            break

    if not customer_id:
        raise HTTPException(401, "Token missing required claims and issuer not registered")

    customer = CUSTOMERS[customer_id]
    claims["registry_name"] = customer.get("registry_name")
    claims["storage_account"] = customer.get("storage_account")
    claims["entitlements"] = {
        "models": customer.get("models", []),
        "versions": ["*"],
    }
    log.info(f"OK: Auth (legacy) | customer={customer_id}")
    return claims


def check_entitlements(claims: dict, model: str, version: str = None):
    """Check if user is entitled to access the requested model/version.

    Raises HTTP 403 on denial.
    """
    entitlements = claims.get("entitlements", {})
    allowed_models = entitlements.get("models", [])
    allowed_versions = entitlements.get("versions", [])

    if "*" not in allowed_models and model not in allowed_models:
        raise HTTPException(403, f"Not entitled to model: {model}")

    if version and allowed_versions and "*" not in allowed_versions and version not in allowed_versions:
        raise HTTPException(403, f"Not entitled to version: {version}")
