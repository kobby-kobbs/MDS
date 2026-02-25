"""
Azure ML Registry and Blob Storage clients.

Required environment variables:
    REGISTRY_NAME    - Azure ML Registry name (e.g. 'fl_private_model')
    STORAGE_ACCOUNT  - Azure Blob Storage account name
    STORAGE_CONTAINER - Blob container for model files (default: 'models')
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from azure.ai.ml import MLClient
from azure.identity import DefaultAzureCredential
from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    ContainerSasPermissions,
    generate_blob_sas,
    generate_container_sas,
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


def get_ml_client(registry_name: str | None = None) -> MLClient:
    """Get or create Azure ML Registry client.

    Args:
        registry_name: Override registry. Defaults to REGISTRY_NAME.
    """
    name = registry_name or REGISTRY_NAME
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
    if acct not in _blob_clients:
        _blob_clients[acct] = BlobServiceClient(
            f"https://{acct}.blob.core.windows.net",
            DefaultAzureCredential(),
        )
        log.info(f"Connected to storage: {acct}")
    return _blob_clients[acct]


def upload_to_blob(
    content: bytes,
    blob_name: str,
    *,
    storage_account: str | None = None,
) -> str:
    """Upload file to blob storage, returns URL."""
    acct = storage_account or STORAGE_ACCOUNT
    container = get_blob_client(acct).get_container_client(STORAGE_CONTAINER)
    try:
        container.create_container()
    except Exception:
        pass  # Already exists
    container.get_blob_client(blob_name).upload_blob(content, overwrite=True)
    return f"https://{acct}.blob.core.windows.net/{STORAGE_CONTAINER}/{blob_name}"


def generate_sas_url(
    blob_name: str,
    hours: int = 1,
    *,
    storage_account: str | None = None,
) -> str:
    """Generate a time-limited SAS download URL."""
    acct = storage_account or STORAGE_ACCOUNT
    client = get_blob_client(acct)
    start = datetime.now(timezone.utc)
    expiry = start + timedelta(hours=hours)

    key = client.get_user_delegation_key(start, expiry)
    sas = generate_blob_sas(
        acct,
        STORAGE_CONTAINER,
        blob_name,
        user_delegation_key=key,
        permission=BlobSasPermissions(read=True),
        expiry=expiry,
        start=start,
    )
    return f"https://{acct}.blob.core.windows.net/{STORAGE_CONTAINER}/{blob_name}?{sas}"


def generate_upload_sas_url(
    blob_prefix: str,
    hours: int = 4,
    *,
    storage_account: str | None = None,
) -> str:
    """Generate a time-limited *container*-level SAS URL with write+create permissions.

    Returns a URL of the form:
        https://<acct>.blob.core.windows.net/<container>/<blob_prefix>?<container_sas>

    Because the SAS is scoped to the **container** (not a single blob), the
    client can PUT to any blob path under the prefix, e.g.
        <url_base>/<filename>?<sas>
    """
    acct = storage_account or STORAGE_ACCOUNT
    client = get_blob_client(acct)
    start = datetime.now(timezone.utc)
    expiry = start + timedelta(hours=hours)

    key = client.get_user_delegation_key(start, expiry)
    sas = generate_container_sas(
        acct,
        STORAGE_CONTAINER,
        user_delegation_key=key,
        permission=ContainerSasPermissions(read=True, write=True, create=True, list=True),
        expiry=expiry,
        start=start,
    )
    return f"https://{acct}.blob.core.windows.net/{STORAGE_CONTAINER}/{blob_prefix}?{sas}"


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
