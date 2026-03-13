"""
JWKS key fetching and caching (1hr TTL, stale fallback).

Keys are cached by issuer URL. The JWKS endpoint is derived automatically
from the issuer: ``{issuer}/.well-known/jwks.json``.
"""

import logging
import threading
import time
from typing import Any

import requests
from jwt import PyJWK

log = logging.getLogger("mds.jwks")

JWKS_CACHE_TTL = 3600  # 1 hour in seconds

# In-memory cache: { issuer: { "fetched_at": timestamp, "keys": { kid: key } } }
_jwks_cache: dict[str, dict[str, Any]] = {}
_jwks_lock = threading.Lock()


def derive_jwks_url(issuer: str) -> str:
    """Derive JWKS URL from issuer using OIDC standard convention.

    Examples:
        https://phonepe.auth0.com/     → https://phonepe.auth0.com/.well-known/jwks.json
        https://login.okta.com/oauth2  → https://login.okta.com/oauth2/.well-known/jwks.json
    """
    base = issuer.rstrip("/")
    return f"{base}/.well-known/jwks.json"


def fetch_jwks(issuer: str) -> dict[str, Any]:
    """Fetch and cache JWKS keys for an issuer. Returns { kid: key }."""
    now = time.time()

    with _jwks_lock:
        if issuer in _jwks_cache:
            cached = _jwks_cache[issuer]
            age = now - cached["fetched_at"]
            if age < JWKS_CACHE_TTL:
                log.debug(f"JWKS cache hit for {issuer} (age: {age:.0f}s)")
                return cached["keys"]
            else:
                log.debug(f"JWKS cache stale for {issuer} (age: {age:.0f}s)")

    jwks_url = derive_jwks_url(issuer)

    try:
        log.info(f"Fetching JWKS for {issuer} from {jwks_url}")
        response = requests.get(jwks_url, timeout=10)
        response.raise_for_status()
        jwks_data = response.json()
    except Exception as e:
        log.warning(f"Failed to fetch JWKS for {issuer}: {e}")
        with _jwks_lock:
            if issuer in _jwks_cache:
                log.info(f"Using stale JWKS cache for {issuer}")
                return _jwks_cache[issuer]["keys"]
        return {}

    keys = {}
    for jwk_data in jwks_data.get("keys", []):
        try:
            kid = jwk_data.get("kid")
            if not kid:
                log.warning(f"JWKS key without kid from {issuer}, skipping")
                continue

            jwk = PyJWK.from_dict(jwk_data)
            keys[kid] = jwk.key
            log.debug(f"Loaded JWKS key {kid} for {issuer}")
        except Exception as e:
            log.warning(f"Failed to parse JWKS key from {issuer}: {e}")
            continue

    if keys:
        with _jwks_lock:
            _jwks_cache[issuer] = {"fetched_at": now, "keys": keys}
        log.info(f"Cached {len(keys)} JWKS keys for {issuer}")
    else:
        log.warning(f"No valid keys found in JWKS for {issuer}")

    return keys


def get_jwks_key(issuer: str, kid: str = None):
    """Get a specific public key from JWKS for an issuer, or None if not found."""
    keys = fetch_jwks(issuer)

    if not keys:
        return None

    if kid and kid in keys:
        return keys[kid]

    if len(keys) == 1:
        return list(keys.values())[0]
    log.warning(f"kid '{kid}' not found in {len(keys)} JWKS keys for {issuer}")
    return None


def invalidate_issuer_cache(issuer: str) -> bool:
    """Force-invalidate JWKS cache for an issuer.

    Use after key rotation to force a fresh fetch on next auth request.
    Returns True if cache entry was removed, False if not found.
    """
    with _jwks_lock:
        if issuer in _jwks_cache:
            del _jwks_cache[issuer]
            log.info(f"Invalidated JWKS cache for {issuer}")
            return True
    log.debug(f"No JWKS cache entry to invalidate for {issuer}")
    return False
