"""
JWKS key fetching and caching (1hr TTL, stale fallback).
"""

import logging
import time
from typing import Any, Dict

import requests
from jwt import PyJWK

log = logging.getLogger("mds.jwks")

JWKS_CACHE_TTL = 3600  # 1 hour in seconds

# In-memory cache: { customer_id: { "fetched_at": timestamp, "keys": { kid: key } } }
_jwks_cache: Dict[str, Dict[str, Any]] = {}


def fetch_jwks(customer_id: str, jwks_url: str) -> Dict[str, Any]:
    """Fetch and cache JWKS keys for a customer. Returns { kid: key }."""
    now = time.time()

    # Check cache
    if customer_id in _jwks_cache:
        cached = _jwks_cache[customer_id]
        age = now - cached["fetched_at"]

        # If cache is still fresh, use it
        if age < JWKS_CACHE_TTL:
            log.debug(f"JWKS cache hit for {customer_id} (age: {age:.0f}s)")
            return cached["keys"]
        else:
            log.debug(f"JWKS cache stale for {customer_id} (age: {age:.0f}s)")

    # Fetch fresh keys
    try:
        log.info(f"Fetching JWKS for {customer_id} from {jwks_url}")
        response = requests.get(jwks_url, timeout=10)
        response.raise_for_status()
        jwks_data = response.json()
    except Exception as e:
        log.warning(f"Failed to fetch JWKS for {customer_id}: {e}")
        # If fetch fails but we have stale cache, use it (better than failing)
        if customer_id in _jwks_cache:
            log.info(f"Using stale JWKS cache for {customer_id}")
            return _jwks_cache[customer_id]["keys"]
        return {}

    # Parse keys from JWKS
    keys = {}
    for jwk_data in jwks_data.get("keys", []):
        try:
            kid = jwk_data.get("kid")
            if not kid:
                log.warning(f"JWKS key without kid from {customer_id}, skipping")
                continue

            # Use PyJWT's built-in JWK parsing
            jwk = PyJWK.from_dict(jwk_data)
            keys[kid] = jwk.key
            log.debug(f"Loaded JWKS key {kid} for {customer_id}")
        except Exception as e:
            log.warning(f"Failed to parse JWKS key from {customer_id}: {e}")
            continue

    if keys:
        # Store in cache
        _jwks_cache[customer_id] = {"fetched_at": now, "keys": keys}
        log.info(f"Cached {len(keys)} JWKS keys for {customer_id}")
    else:
        log.warning(f"No valid keys found in JWKS for {customer_id}")

    return keys


def get_jwks_key(customer_id: str, jwks_url: str, kid: str = None):
    """Get a specific public key from JWKS, or None if not found."""
    keys = fetch_jwks(customer_id, jwks_url)

    if not keys:
        return None

    if kid and kid in keys:
        return keys[kid]

    # No kid specified or kid not found, return first key
    return list(keys.values())[0] if keys else None
