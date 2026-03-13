"""
Azure ML Registry and Blob Storage clients.

Required environment variables:
    REGISTRY_NAME    - Azure ML Registry name (e.g. 'fl_private_model')
    STORAGE_ACCOUNT  - Azure Blob Storage account name
    STORAGE_CONTAINER - Blob container for model files (default: 'models')
"""

import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from azure.ai.ml import MLClient
from azure.identity import DefaultAzureCredential
from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    generate_blob_sas,
)

log = logging.getLogger("mds")

# Default (shared) registry & storage
# Used when a customer has no dedicated registry/storage configured.
REGISTRY_NAME = os.environ.get("REGISTRY_NAME", "fl_private_model")
STORAGE_ACCOUNT = os.environ.get("STORAGE_ACCOUNT", "customermodelstorage")
STORAGE_CONTAINER = os.environ.get("STORAGE_CONTAINER", "models")

# Client caches (keyed by registry/account name for multi-tenant)
_ml_clients: dict[str, MLClient] = {}
_blob_clients: dict[str, BlobServiceClient] = {}
_clients_lock = threading.Lock()

# Delegation key cache: { account: (key, expiry_datetime) }
_delegation_cache: dict[str, tuple] = {}


def get_ml_client(registry_name: str | None = None) -> MLClient:
    """Get or create Azure ML Registry client.

    Args:
        registry_name: Override registry. Defaults to REGISTRY_NAME.
    """
    name = registry_name or REGISTRY_NAME
    with _clients_lock:
        if name not in _ml_clients:
            _ml_clients[name] = MLClient(credential=DefaultAzureCredential(), registry_name=name)
            log.info(f"Connected to registry: {name}")
        return _ml_clients[name]


def get_blob_client(storage_account: str | None = None) -> BlobServiceClient:
    """Get or create Azure Blob Storage client.

    Args:
        storage_account: Override account. Defaults to STORAGE_ACCOUNT.
    """
    acct = storage_account or STORAGE_ACCOUNT
    with _clients_lock:
        if acct not in _blob_clients:
            _blob_clients[acct] = BlobServiceClient(
                f"https://{acct}.blob.core.windows.net",
                DefaultAzureCredential(),
            )
            log.info(f"Connected to storage: {acct}")
        return _blob_clients[acct]


def generate_sas_url(
    blob_name: str,
    hours: int = 1,
    *,
    storage_account: str | None = None,
) -> str:
    """Generate a time-limited SAS download URL."""
    acct = storage_account or STORAGE_ACCOUNT
    client = get_blob_client(acct)
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(hours=hours)

    # Use cached delegation key if still valid
    with _clients_lock:
        cached = _delegation_cache.get(acct)
        if cached and cached[1] > now:
            key = cached[0]
        else:
            key = None

    if not key:
        key = client.get_user_delegation_key(now, expiry)
        with _clients_lock:
            _delegation_cache[acct] = (key, now + timedelta(minutes=50))

    sas = generate_blob_sas(
        acct,
        STORAGE_CONTAINER,
        blob_name,
        user_delegation_key=key,
        permission=BlobSasPermissions(read=True),
        expiry=expiry,
        start=now,
    )
    return f"https://{acct}.blob.core.windows.net/{STORAGE_CONTAINER}/{blob_name}?{sas}"


def list_blobs(prefix: str, *, storage_account: str | None = None) -> list[str]:
    """List blob names under a given prefix."""
    acct = storage_account or STORAGE_ACCOUNT
    container = get_blob_client(acct).get_container_client(STORAGE_CONTAINER)
    return [b.name for b in container.list_blobs(name_starts_with=prefix)]


def list_blob_prefixes(*, storage_account: str | None = None) -> dict[str, list[str]]:
    """List all top-level model prefixes in blob storage.

    Returns dict mapping model_name -> list of version prefixes (e.g. {"mnist": ["v1","v2"]}).
    Blob structure: <model_name>/v<N>/...
    """
    acct = storage_account or STORAGE_ACCOUNT
    container = get_blob_client(acct).get_container_client(STORAGE_CONTAINER)
    models: dict[str, set[str]] = {}
    for blob in container.list_blobs():
        parts = blob.name.split("/", 2)
        if len(parts) >= 2:
            name, ver = parts[0], parts[1]
            models.setdefault(name, set()).add(ver)
    return {k: sorted(v) for k, v in models.items()}


def download_blob(blob_name: str, *, storage_account: str | None = None) -> bytes:
    """Download blob content as bytes."""
    acct = storage_account or STORAGE_ACCOUNT
    container = get_blob_client(acct).get_container_client(STORAGE_CONTAINER)
    return container.get_blob_client(blob_name).download_blob().readall()


def delete_blobs(blob_names: list[str], *, storage_account: str | None = None) -> int:
    """Delete a list of blobs from storage. Returns count of successfully deleted blobs."""
    acct = storage_account or STORAGE_ACCOUNT
    container = get_blob_client(acct).get_container_client(STORAGE_CONTAINER)
    deleted = 0
    for name in blob_names:
        try:
            container.get_blob_client(name).delete_blob()
            deleted += 1
        except Exception as e:
            log.warning(f"Failed to delete blob {name}: {e}")
    return deleted
