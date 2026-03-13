import logging
import os
import threading
import traceback
from datetime import datetime, timezone
from typing import Optional

from cachetools import TTLCache
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from starlette.requests import Request

from .auth import check_entitlements, require_auth
from .azure_clients import (
    REGISTRY_NAME,
    delete_blobs,
    generate_sas_url,
    get_ml_client,
    list_blob_prefixes,
    list_blobs,
)
from .catalog import (
    CatalogRequest,
    build_foundry_model,
    decode_continuation_token,
    encode_continuation_token,
)
from .customers import get_customer_by_api_key

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("mds")

_cache_lock = threading.Lock()
_model_list_cache = TTLCache(maxsize=32, ttl=60)
_model_info_cache = TTLCache(maxsize=128, ttl=120)
_model_versions_cache = TTLCache(maxsize=128, ttl=60)
_catalog_cache = TTLCache(maxsize=16, ttl=120)
_ALL_CACHES = (_model_list_cache, _model_info_cache, _model_versions_cache, _catalog_cache)


def _invalidate_model_caches():
    with _cache_lock:
        for c in _ALL_CACHES:
            c.clear()


def _get_client_ip(request: Request) -> str:
    """Extract real client IP, respecting X-Forwarded-For from Azure App Service."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


app = FastAPI(title="Model Distribution Service")

_rate_limit_disabled = os.environ.get("MDS_DISABLE_RATE_LIMIT", "").lower() in ("1", "true", "yes")

limiter = Limiter(
    key_func=_get_client_ip,
    enabled=not _rate_limit_disabled,
)
app.state.limiter = limiter
if _rate_limit_disabled:
    log.info("Rate limiting DISABLED via MDS_DISABLE_RATE_LIMIT")
_APP_START = datetime.now(timezone.utc)
_stats = {"downloads": 0, "last_download": None}
_stats_lock = threading.Lock()


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"detail": f"Rate limit exceeded: {exc.detail}"})


@app.exception_handler(Exception)
async def _unhandled(request, exc):
    if isinstance(exc, HTTPException):
        raise exc
    log.error(f"{request.method} {request.url.path}: {exc}\n{traceback.format_exc()}")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


def _client_for(claims: dict):
    """Get ML client and storage account from claims."""
    reg = claims.get("registry_name")
    sa = claims.get("storage_account")
    return get_ml_client(reg), sa


def _build_claims_from_customer(cid: str) -> dict:
    """Translate a CUSTOMERS dict entry into the unified claims format (for API key auth)."""
    from .customers import CUSTOMERS

    c = CUSTOMERS.get(cid, {})
    return {
        "registry_name": c.get("registry_name"),
        "storage_account": c.get("storage_account"),
        "entitlements": {"models": c.get("models", []), "versions": ["*"]},
        "iss": c.get("issuer", ""),
        "sub": c.get("sub", ""),
    }


def _cached(cache, key, fn):
    with _cache_lock:
        hit = cache.get(key)
    if hit is not None:
        return hit
    val = fn()
    with _cache_lock:
        cache[key] = val
    return val


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/models")
def list_models(detail: bool = Query(False)):
    def _fetch():
        models = []
        for m in get_ml_client().models.list():
            entry = {"name": m.name, "latest_version": m.latest_version}
            if detail:
                try:
                    info = get_ml_client().models.get(name=m.name, version=m.latest_version)
                    t = info.tags or {}
                    entry.update(
                        {
                            "task": t.get("task", ""),
                            "device": t.get("device", ""),
                            "model_type": t.get("modelType", ""),
                            "size_bytes": int(t.get("file_size_bytes", 0)),
                            "fl_ready": t.get("foundryLocal", "").lower() == "true",
                            "author": t.get("author", ""),
                        }
                    )
                except Exception as e:
                    log.warning(f"Skipped model detail {m.name}: {e}")
            models.append(entry)
        return {"registry": REGISTRY_NAME, "models": models}

    cache_key = "all_detail" if detail else "all"
    return _cached(_model_list_cache, cache_key, _fetch)


@app.delete("/models/{name}")
def delete_model(name: str, version: str = Query(None), authorization: str = Header(...)):
    """Delete a model from the registry and optionally its blobs from storage."""
    claims = require_auth(authorization)
    check_entitlements(claims, name)
    ml, sa = _client_for(claims)

    # Find versions to delete
    try:
        all_versions = list(ml.models.list(name=name))
    except Exception:
        raise HTTPException(404, f"Model not found: {name}")
    if not all_versions:
        raise HTTPException(404, f"Model not found: {name}")

    versions_to_delete = []
    if version:
        versions_to_delete = [version]
    else:
        versions_to_delete = [str(v.version) for v in all_versions]

    deleted_versions = []
    blob_errors = []
    for ver in versions_to_delete:
        # Delete from registry
        try:
            ml.models.archive(name=name, version=ver)
            deleted_versions.append(ver)
        except Exception as e:
            log.warning(f"Failed to archive {name} v{ver}: {e}")

        # Delete blobs
        prefix = f"{name}/v{ver}"
        try:
            blobs = list_blobs(prefix, storage_account=sa)
            if blobs:
                delete_blobs(blobs, storage_account=sa)
        except Exception as e:
            blob_errors.append(f"v{ver}: {e}")

    _invalidate_model_caches()
    log.info(f"Delete | {claims.get('iss', 'unknown')} | {name} | versions={deleted_versions}")
    result = {"status": "deleted", "model": name, "versions_deleted": deleted_versions}
    if blob_errors:
        result["blob_warnings"] = blob_errors
    return result


@app.get("/status")
def status(authorization: str = Header(...)):
    """Service status dashboard data."""
    require_auth(authorization)
    now = datetime.now(timezone.utc)
    uptime = now - _APP_START
    days, remainder = divmod(int(uptime.total_seconds()), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    try:
        models = list(get_ml_client().models.list())
        model_count = len(models)
    except Exception:
        model_count = -1

    with _stats_lock:
        stats_snapshot = dict(_stats)

    return {
        "status": "ok",
        "uptime": f"{days}d {hours}h {minutes}m",
        "uptime_seconds": int(uptime.total_seconds()),
        "started_at": _APP_START.isoformat(),
        "model_count": model_count,
        "stats": stats_snapshot,
    }


@app.get("/models/sync")
def sync_models(authorization: str = Header(...)):
    """Cross-reference registry with blob storage. Shows models that are:
    - in registry and blob (healthy)
    - in blob only (registered but blobs orphaned)
    - in registry only (blobs missing or deleted)
    """
    claims = require_auth(authorization)
    ml, sa = _client_for(claims)
    registry_models = {}
    for m in ml.models.list():
        registry_models[m.name] = {"latest_version": m.latest_version}
    blob_models = list_blob_prefixes(storage_account=sa)
    all_names = sorted(set(registry_models) | set(blob_models))
    results = []
    for name in all_names:
        in_reg = name in registry_models
        in_blob = name in blob_models
        entry = {"name": name, "in_registry": in_reg, "in_blob": in_blob}
        if in_reg:
            entry["latest_version"] = registry_models[name]["latest_version"]
        if in_blob:
            entry["blob_versions"] = blob_models[name]
        if in_reg and in_blob:
            entry["status"] = "synced"
        elif in_blob:
            entry["status"] = "blob_only"
        else:
            entry["status"] = "registry_only"
        results.append(entry)
    counts = {
        "synced": sum(1 for r in results if r["status"] == "synced"),
        "blob_only": sum(1 for r in results if r["status"] == "blob_only"),
        "registry_only": sum(1 for r in results if r["status"] == "registry_only"),
    }
    return {"total": len(results), "counts": counts, "models": results}


@app.post("/download")
@limiter.limit("30/minute")
def download(
    request: Request,
    model: str = Query(...),
    version: str = Query(...),
    format: str = Query("urls"),
    authorization: str = Header(None),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    # Support both JWT Bearer and API key auth
    if authorization:
        claims = require_auth(authorization)
    elif x_api_key:
        cid = get_customer_by_api_key(x_api_key)
        if not cid:
            raise HTTPException(401, "Invalid API key")
        claims = _build_claims_from_customer(cid)
    else:
        raise HTTPException(401, "Authorization or X-API-Key header required")
    check_entitlements(claims, model, version)
    ml, sa = _client_for(claims)
    try:
        info = ml.models.get(name=model, version=version)
    except Exception:
        raise HTTPException(404, f"Model not found: {model} v{version}")
    tags = info.tags or {}
    log.info(f"Download | {claims.get('iss', 'api-key')} | {model} v{version} | format={format}")
    with _stats_lock:
        _stats["downloads"] += 1
        _stats["last_download"] = datetime.now(timezone.utc).isoformat()
    bp, bf, bs = tags.get("blob_prefix", ""), tags.get("blob_files", ""), tags.get("blob_name", "")
    if bp or bf:
        blob_names = [b.strip() for b in bf.split(",") if b.strip()] if bf else list_blobs(bp, storage_account=sa)
    elif bs:
        blob_names = [bs]
    else:
        return {"model": model, "version": version, "download_url": info.path}
    if not blob_names:
        raise HTTPException(404, f"No files found for {model} v{version}")
    if len(blob_names) == 1 and not bp:
        url = generate_sas_url(blob_names[0], storage_account=sa)
        return {"model": model, "version": version, "download_url": url, "expires_in": "1 hour"}
    files = [{"file": n, "download_url": generate_sas_url(n, storage_account=sa)} for n in blob_names]
    return {"model": model, "version": version, "file_count": len(files), "files": files, "expires_in": "1 hour"}


@app.post("/catalog")
@limiter.limit("60/minute")
def catalog(request: Request, body: CatalogRequest = None, authorization: str = Header(...)):
    """Catalog endpoint with JWT Bearer auth. Returns indexEntitiesResponse."""
    claims = require_auth(authorization)
    return _fl_catalog_handler(claims, body)


# ─── FL-native catalog (AzureCatalogUri target) ─────────────────────────
# This is the endpoint FL calls when configured with:
#   additionalSettings: { "AzureCatalogUri": "https://mds.../catalog/foundrylocal/<api-key>" }
#
# Auth: API key in URL path or X-API-Key header or JWT Bearer fallback.
# Request/Response: standard FL indexEntitiesRequest/Response format.


def _fl_catalog_handler(claims: dict, request: CatalogRequest = None):
    """Shared FL catalog logic -- returns indexEntitiesResponse.

    Caches the full model list per registry, then filters by entitlements
    after cache retrieval so entitlement changes take effect immediately.
    """
    reg = claims.get("registry_name")
    entitlements = claims.get("entitlements", {})

    req = request.indexEntitiesRequest if request else None
    page_size = req.pageSize if req and req.pageSize else 100
    if req and req.continuationToken:
        skip, page_size = decode_continuation_token(req.continuationToken)
    else:
        skip = req.skip if req and req.skip else 0

    def _fetch_all_models():
        ml, _ = _client_for(claims)
        out = []
        for m in ml.models.list():
            try:
                info = ml.models.get(name=m.name, version=m.latest_version)
                tags = info.tags or {}
                if tags.get("foundryLocal", "").lower() not in ("true", "test"):
                    continue
                out.append(build_foundry_model(info, tags, registry_name=reg or REGISTRY_NAME))
            except Exception as e:
                log.warning(f"Skipped model {m.name}: {e}")
        return out

    cache_key = f"cat_{reg or 'default'}"
    all_models = _cached(_catalog_cache, cache_key, _fetch_all_models)

    # Filter by entitlements AFTER cache retrieval (so changes take effect immediately)
    allowed = entitlements.get("models", [])
    if "*" not in allowed:
        models = [m for m in all_models if m["annotations"]["name"] in allowed]
    else:
        models = all_models

    total = len(models)
    page = models[skip : skip + page_size]
    ns = skip + page_size
    resp = {
        "indexEntitiesResponse": {
            "totalCount": total,
            "value": page,
            "nextSkip": ns if ns < total else None,
            "continuationToken": encode_continuation_token(ns, page_size) if ns < total else None,
        }
    }
    log.info(f"Catalog | registry={reg} | {total} models | page {skip}-{skip + len(page)}")
    return resp


# DEPRECATED: API key in URL path is a security anti-pattern (keys appear in logs).
# Kept for backward compatibility with FL Core which uses AzureCatalogUri with key in path.
# Prefer /catalog/foundrylocal with X-API-Key header for new integrations.
@app.post("/catalog/foundrylocal/{api_key}")
@limiter.limit("60/minute")
def catalog_foundrylocal_keyed(
    request: Request,
    api_key: str,
    body: CatalogRequest = None,
):
    """FL catalog endpoint with API key in URL path.

    Use this as the AzureCatalogUri -- guaranteed to work since FL
    just POSTs to the URL without adding custom headers:
        AzureCatalogUri: https://mds.../catalog/foundrylocal/<api-key>
    """
    cid = get_customer_by_api_key(api_key)
    if not cid:
        raise HTTPException(401, "Invalid API key")
    return _fl_catalog_handler(_build_claims_from_customer(cid), body)


@app.post("/catalog/foundrylocal")
@limiter.limit("60/minute")
def catalog_foundrylocal(
    request: Request,
    body: CatalogRequest = None,
    x_api_key: str = Header(None, alias="X-API-Key"),
    authorization: str = Header(None),
):
    """FL catalog endpoint with header-based auth.

    Supports X-API-Key header or JWT Bearer token.
    """
    cid = None
    if x_api_key:
        cid = get_customer_by_api_key(x_api_key)
        if not cid:
            raise HTTPException(401, "Invalid API key")
    elif authorization:
        claims = require_auth(authorization)
        return _fl_catalog_handler(claims, body)
    else:
        raise HTTPException(401, "X-API-Key or Authorization header required")
    return _fl_catalog_handler(_build_claims_from_customer(cid), body)


@app.get("/models/{name}/versions")
def list_model_versions(name: str, authorization: str = Header(...)):
    claims = require_auth(authorization)
    check_entitlements(claims, name)
    reg = claims.get("registry_name")

    def _fetch():
        ml, _ = _client_for(claims)
        try:
            vers = list(ml.models.list(name=name))
        except Exception:
            vers = []
        if not vers:
            raise HTTPException(404, f"Not found: {name}")
        sorted_vers = sorted(
            [str(m.version) for m in vers],
            key=lambda v: int(v) if v.isdigit() else 0,
            reverse=True,
        )
        return {"model": name, "versions": sorted_vers}

    return _cached(_model_versions_cache, (name, reg), _fetch)


@app.get("/models/{name}")
def get_model(name: str, version: str = Query(None), authorization: str = Header(...)):
    claims = require_auth(authorization)
    check_entitlements(claims, name)
    reg = claims.get("registry_name")

    def _fetch():
        ml, _ = _client_for(claims)
        try:
            if version:
                info = ml.models.get(name=name, version=version)
            else:
                vs = list(ml.models.list(name=name))
                if not vs:
                    raise HTTPException(404, f"Not found: {name}")
                info = ml.models.get(name=name, version=vs[0].latest_version)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(404, f"Not found: {name}")
        return build_foundry_model(info, info.tags or {}, registry_name=reg or REGISTRY_NAME)

    return _cached(_model_info_cache, (name, version or "latest", reg), _fetch)


# ─── Admin Endpoint Models ──────────────────────────────────────


class RegisterRequest(BaseModel):
    customer_name: str
    issuer: str
    models: list[str] = Field(default=["*"])
    registry_name: str
    storage_account: str
    jwks_url: Optional[str] = None


class OffboardRequest(BaseModel):
    customer_id: str


def _require_admin(x_admin_key: str):
    """Validate admin key from X-Admin-Key header."""
    import hmac

    admin_key = os.environ.get("MDS_ADMIN_KEY", "")
    if not admin_key:
        raise HTTPException(500, "Admin endpoints not configured (MDS_ADMIN_KEY not set)")
    if not x_admin_key:
        raise HTTPException(401, "X-Admin-Key header required")
    if not hmac.compare_digest(admin_key, x_admin_key):
        raise HTTPException(403, "Invalid admin key")


# ─── Admin Endpoints ────────────────────────────────────────────


@app.post("/admin/register")
def admin_register(
    body: RegisterRequest,
    x_admin_key: str = Header(..., alias="X-Admin-Key"),
):
    """Register a new customer. Returns API key (shown only once)."""
    _require_admin(x_admin_key)

    from .customers import register_customer

    try:
        result = register_customer(
            customer_name=body.customer_name,
            issuer=body.issuer,
            models=body.models,
            registry_name=body.registry_name,
            storage_account=body.storage_account,
            jwks_url=body.jwks_url,
        )
    except ValueError as e:
        raise HTTPException(409, str(e)) from e

    app_name = os.environ.get("MDS_APP_NAME", "mds-model-distribution")
    catalog_url = f"https://{app_name}.azurewebsites.net/catalog/foundrylocal"

    log.info(f"Admin | Registered customer: {result['customer_id']}")
    return {
        "status": "registered",
        "customer_id": result["customer_id"],
        "api_key": result["api_key"],
        "jwks_url": result["jwks_url"],
        "catalog_url": catalog_url,
    }


@app.post("/admin/offboard")
def admin_offboard(
    body: OffboardRequest,
    x_admin_key: str = Header(..., alias="X-Admin-Key"),
):
    """Remove a customer. Revokes API key access immediately."""
    _require_admin(x_admin_key)

    from .customers import remove_customer
    from .jwks import invalidate_issuer_cache

    removed = remove_customer(body.customer_id)
    if not removed:
        raise HTTPException(404, f"Customer '{body.customer_id}' not found")

    # Invalidate JWKS cache for customer's issuer
    from .customers import CUSTOMERS

    customer = CUSTOMERS.get(body.customer_id, {})
    issuer = customer.get("issuer")
    if issuer:
        invalidate_issuer_cache(issuer)
    log.info(f"Admin | Offboarded customer: {body.customer_id}")
    return {"status": "offboarded", "customer_id": body.customer_id}


@app.post("/admin/refresh-jwks/{customer_id}")
def admin_refresh_jwks(
    customer_id: str,
    x_admin_key: str = Header(..., alias="X-Admin-Key"),
):
    """Force-refresh JWKS cache for a customer after key rotation."""
    _require_admin(x_admin_key)

    from .customers import CUSTOMERS
    from .jwks import fetch_jwks, invalidate_issuer_cache

    if customer_id not in CUSTOMERS:
        raise HTTPException(404, f"Customer '{customer_id}' not found")

    issuer = CUSTOMERS[customer_id].get("issuer", "")
    if issuer:
        invalidate_issuer_cache(issuer)
        keys = fetch_jwks(issuer)
        key_count = len(keys)
    else:
        key_count = 0

    log.info(f"Admin | Refreshed JWKS for {customer_id}: {key_count} keys")
    return {
        "status": "refreshed",
        "customer_id": customer_id,
        "keys_loaded": key_count,
    }
