"""
JWT authentication and entitlement checks.
"""

import logging

import jwt
from fastapi import HTTPException

from .customers import CUSTOMERS, get_customer_by_issuer, get_public_key

log = logging.getLogger("mds")


def require_auth(authorization: str) -> dict:
    """Validate JWT token, return claims with customer_id. Raises 401 on failure."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Invalid authorization header")

    token = authorization[7:]

    try:
        unverified_header = jwt.get_unverified_header(token)
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.DecodeError:
        raise HTTPException(401, "Invalid token format")

    issuer = unverified.get("iss")
    customer_id = get_customer_by_issuer(issuer) if issuer else None
    if not customer_id:
        raise HTTPException(401, "Unknown token issuer")

    kid = unverified_header.get("kid")

    try:
        claims = jwt.decode(
            token, get_public_key(customer_id, kid), algorithms=["RS256"], audience="model-distribution-service"
        )
        log.info(f"OK: Auth | customer={customer_id}")
        return {"customer_id": customer_id, **claims}
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.InvalidSignatureError:
        raise HTTPException(401, "Invalid signature")
    except jwt.InvalidAudienceError:
        raise HTTPException(401, "Invalid audience")


def check_entitlement(claims: dict, model: str) -> bool:
    """Check if customer is entitled to access a model."""
    customer_id = claims.get("customer_id")
    if not customer_id or customer_id not in CUSTOMERS:
        return False

    allowed = CUSTOMERS[customer_id].get("models", [])
    return model in allowed or "*" in allowed
