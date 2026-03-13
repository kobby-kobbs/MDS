"""
Customer configuration for MDS.

Customers are loaded from ``customers.json`` (file-based config) so that
onboarding / offboarding can happen without touching Python code or
redeploying the server.

Self-service flow:
    Customer runs onboard.ps1 → script calls POST /admin/register →
    MDS generates API key, stores config, returns key to customer.
    No manual file edits, no redeployment.

Each customer entry defines:
    - name:            Human-readable name
    - sub:             Expected JWT 'sub' claim value
    - issuer:          JWT 'iss' claim — customer's Auth0 / Okta / Entra ID issuer URL
    - jwks_url:        JWKS endpoint (auto-discovered from issuer via OIDC)
    - models:          List of model names the customer can access ("*" = all)
    - registry_name:   (optional) Dedicated Azure ML Registry
    - storage_account: (optional) Dedicated Azure Blob Storage account
    - api_key_hash:    (optional) SHA-256 hash of API key (set by /admin/register)

API key lookup order:
    1. App Service env vars (CUSTOMER_<ID>_API_KEY) — backward compat
    2. SHA-256 hash in config (api_key_hash field) — self-service registrations

Config file search order:
    1. $MDS_CUSTOMERS_FILE env var (explicit path)
    2. customers/customers.json (relative to project root)
    3. /home/data/customers.json (Azure App Service persistent storage)
    4. /home/site/wwwroot/customers/customers.json (Azure App Service deploy dir)
"""

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

import requests

log = logging.getLogger("mds")

_CUSTOMERS_LOADED = False
_customers_lock = threading.Lock()
CUSTOMERS: dict = {}

_REQUIRED_FIELDS = {"issuer", "models"}


def _validate_customers():
    """Validate that all customer entries have required fields."""
    errors = []
    for cid, config in CUSTOMERS.items():
        missing = _REQUIRED_FIELDS - set(config.keys())
        if missing:
            errors.append(f"Customer '{cid}' missing required fields: {missing}")
        if not isinstance(config.get("models"), list):
            errors.append(f"Customer '{cid}': 'models' must be a list")
    if errors:
        for e in errors:
            log.error(e)
        raise ValueError(f"Customer config validation failed: {'; '.join(errors)}")


def _find_customers_file() -> Path | None:
    """Locate customers.json using search order."""
    explicit = os.getenv("MDS_CUSTOMERS_FILE")
    if explicit and Path(explicit).is_file():
        return Path(explicit)

    candidates = [
        Path(__file__).resolve().parent.parent.parent / "customers" / "customers.json",
        Path("/home/data/customers.json"),
        Path("/home/site/wwwroot/customers/customers.json"),
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def _get_writable_config_path() -> Path:
    """Return the deterministic path for writing customer config.

    Priority:
        1. $MDS_CUSTOMERS_FILE env var
        2. /home/data/customers.json  (Azure App Service persistent storage)
        3. customers/customers.json   (local dev, relative to project root)
    """
    explicit = os.getenv("MDS_CUSTOMERS_FILE")
    if explicit:
        p = Path(explicit)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    azure_path = Path("/home/data/customers.json")
    if azure_path.parent.exists():
        return azure_path

    local_path = Path(__file__).resolve().parent.parent.parent / "customers" / "customers.json"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    return local_path


def _write_customers_file(data: dict) -> Path:
    """Atomically write customers config to disk. Returns the written path."""
    path = _get_writable_config_path()
    content = json.dumps(data, indent=2, ensure_ascii=False)

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        fd = -1
        if os.name == "nt" and path.exists():
            path.unlink()
        os.rename(tmp, str(path))
    except Exception:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    log.info(f"Wrote {len(data)} customer(s) to {path}")
    return path


def discover_jwks_url(issuer: str) -> str:
    """Auto-discover JWKS URL via OIDC discovery.

    Tries:
        1. {issuer}/.well-known/openid-configuration → jwks_uri field
        2. Fallback: {issuer}/.well-known/jwks.json

    Raises ValueError if both fail.
    """
    base = issuer.rstrip("/")

    oidc_url = f"{base}/.well-known/openid-configuration"
    try:
        resp = requests.get(oidc_url, timeout=10)
        resp.raise_for_status()
        jwks_uri = resp.json().get("jwks_uri")
        if jwks_uri:
            log.info(f"OIDC discovery for {issuer}: jwks_uri={jwks_uri}")
            return jwks_uri
    except Exception as e:
        log.warning(f"OIDC discovery failed for {issuer}: {e}")

    fallback = f"{base}/.well-known/jwks.json"
    try:
        resp = requests.get(fallback, timeout=10)
        resp.raise_for_status()
        log.info(f"JWKS fallback for {issuer}: {fallback}")
        return fallback
    except Exception as e:
        raise ValueError(
            f"Could not discover JWKS URL for issuer '{issuer}'. "
            f"OIDC discovery ({oidc_url}) and fallback ({fallback}) both failed: {e}"
        ) from e


def register_customer(
    customer_name: str,
    issuer: str,
    models: list[str],
    registry_name: str,
    storage_account: str,
    jwks_url: str | None = None,
) -> dict:
    """Register a new customer. Returns { customer_id, api_key, jwks_url }.

    Generates a crypto-secure API key, stores its SHA-256 hash in config,
    and returns the plaintext key once (never stored).
    """
    import hashlib
    import secrets

    customer_id = customer_name.lower().replace(" ", "-")

    if not jwks_url:
        jwks_url = discover_jwks_url(issuer)

    api_key = secrets.token_urlsafe(32)
    api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    customer_config = {
        "name": customer_name,
        "sub": f"{customer_id}-client",
        "issuer": issuer,
        "jwks_url": jwks_url,
        "models": models,
        "registry_name": registry_name,
        "storage_account": storage_account,
        "api_key_hash": api_key_hash,
    }

    with _customers_lock:
        if customer_id in CUSTOMERS:
            raise ValueError(f"Customer '{customer_id}' already exists")

        current = {}
        config_path = _find_customers_file()
        if config_path:
            current = json.loads(config_path.read_text(encoding="utf-8"))

        current[customer_id] = customer_config
        _write_customers_file(current)
        CUSTOMERS[customer_id] = customer_config

    log.info(f"Registered customer: {customer_id}")
    return {
        "customer_id": customer_id,
        "api_key": api_key,
        "jwks_url": jwks_url,
    }


def remove_customer(customer_id: str) -> bool:
    """Remove a customer from config. Returns True if removed, False if not found."""
    customer_id = customer_id.lower()

    with _customers_lock:
        if customer_id not in CUSTOMERS:
            return False

        current = {}
        config_path = _find_customers_file()
        if config_path:
            current = json.loads(config_path.read_text(encoding="utf-8"))

        current.pop(customer_id, None)
        _write_customers_file(current)
        CUSTOMERS.pop(customer_id, None)

    log.info(f"Removed customer: {customer_id}")
    return True


def _load_customers():
    """Load customers from JSON config file, falling back to env vars."""
    global CUSTOMERS, _CUSTOMERS_LOADED
    if _CUSTOMERS_LOADED:
        return

    config_path = _find_customers_file()
    if config_path:
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            CUSTOMERS.update(raw)
            _validate_customers()
            log.info(f"Loaded {len(CUSTOMERS)} customer(s) from {config_path}")
            _CUSTOMERS_LOADED = True
            return
        except Exception as exc:
            log.error(f"Failed to load {config_path}: {exc}")

    # Fallback: env-var-based config (backward compat)
    log.info("No customers.json found, using env-var fallback")
    _upper = os.getenv("CUSTOMER_PHONEPE_ISSUER", "")
    if _upper:
        CUSTOMERS["phonepe"] = {
            "name": "PhonePe India",
            "sub": "phonepe-india",
            "issuer": os.getenv("CUSTOMER_PHONEPE_ISSUER", ""),
            "jwks_url": os.getenv("CUSTOMER_PHONEPE_JWKS_URL", ""),
            "models": ["*"],
            "registry_name": os.getenv("CUSTOMER_PHONEPE_REGISTRY", ""),
            "storage_account": os.getenv("CUSTOMER_PHONEPE_STORAGE", ""),
        }
    _CUSTOMERS_LOADED = True


def reload_customers():
    """Force reload of customer config. Thread-safe."""
    global _CUSTOMERS_LOADED
    config_path = _find_customers_file()
    new_data = {}
    if config_path:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        new_data.update(raw)
    with _customers_lock:
        CUSTOMERS.clear()
        CUSTOMERS.update(new_data)
        _CUSTOMERS_LOADED = True
    log.info(f"Reloaded {len(CUSTOMERS)} customer(s)")


# Auto-load on import
_load_customers()



def get_customer_by_api_key(api_key: str) -> str | None:
    """Look up customer ID by API key.

    Check order (first match wins):
        1. App Service env vars: CUSTOMER_<ID>_API_KEY (backward compat)
        2. SHA-256 hash stored in customer config (api_key_hash field)
    """
    if not api_key:
        return None
    import hashlib
    import hmac

    for customer_id in CUSTOMERS:
        env_key = f"CUSTOMER_{customer_id.upper()}_API_KEY"
        expected = os.getenv(env_key, "")
        if expected and hmac.compare_digest(expected, api_key):
            return customer_id

    incoming_hash = hashlib.sha256(api_key.encode()).hexdigest()
    for customer_id, config in CUSTOMERS.items():
        stored_hash = config.get("api_key_hash", "")
        if stored_hash and hmac.compare_digest(stored_hash, incoming_hash):
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



