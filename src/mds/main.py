import io, json, logging, os, tarfile, tempfile, threading, traceback, uuid, zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import List

from cachetools import TTLCache
from azure.ai.ml.entities import Model
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from .auth import check_entitlement, require_auth
from .azure_clients import (REGISTRY_NAME, STORAGE_ACCOUNT, download_blob, generate_sas_url,
                             generate_upload_sas_url, get_ml_client, list_blob_prefixes, list_blobs, upload_to_blob,
                             delete_blobs)
from .catalog import CatalogRequest, FLCatalogQuery, build_foundry_model, decode_continuation_token, encode_continuation_token
from .customers import get_customer_by_api_key, get_customer_registry, get_customer_storage
from .metadata import build_fl_description, extract_metadata_from_files, extract_onnx_metadata

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("mds")
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

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


app = FastAPI(title="Model Distribution Service")
_APP_START = datetime.now(timezone.utc)
_stats = {"uploads": 0, "downloads": 0, "last_upload": None, "last_download": None}

@app.exception_handler(Exception)
async def _unhandled(request, exc):
    log.error(f"{request.method} {request.url.path}: {exc}\n{traceback.format_exc()}")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})

def _client_for(cid: str = None):
    reg = get_customer_registry(cid) if cid else None
    sa = get_customer_storage(cid) if cid else None
    return get_ml_client(reg), sa

def _next_ver(name: str, cid: str = None) -> int:
    try:
        ml, _ = _client_for(cid)
        vers = list(ml.models.list(name=name))
        return max(int(m.version) for m in vers) + 1 if vers else 1
    except Exception:
        return 1


def _build_upload_tags(claims: dict, auto_tags: dict, **ov) -> dict:
    total_bytes, model_name = ov.pop("total_bytes", 0), ov.pop("model_name", "")
    tags = {**auto_tags, "uploaded_by": claims["customer_id"],
            "file_size_bytes": str(total_bytes), "upload_time": datetime.now(timezone.utc).isoformat(),
            # Core FL tags (always set)
            "alias": ov.pop("alias", "") or model_name,
            "author": ov.pop("author", "") or "Microsoft",
            "directoryPath": ov.pop("directory_path", "") or model_name,
            "task": ov.pop("task", "") or auto_tags.get("task", "custom"),
            "inputModalities": ov.pop("input_modalities", "text") or auto_tags.get("inputModalities", "text"),
            "outputModalities": ov.pop("output_modalities", "text") or auto_tags.get("outputModalities", "text"),
            "device": ov.pop("device", "cpu"),
            "executionProvider": ov.pop("execution_provider", "cpuexecutionprovider"),
            "modelType": ov.pop("model_type", "onnx") or auto_tags.get("modelType", "onnx"),
            "foundryLocal": "true",
            "disable-maap": ov.pop("disable_maap", "True")}
    # License tags
    for lk in ("license", "licenseDescription"):
        field = {"license": "license_id", "licenseDescription": "license_description"}.get(lk, lk)
        val = ov.pop(field, "") or auto_tags.get(lk, "")
        if val:
            tags[lk] = val
    # Optional FL tags (maxOutputTokens, promptTemplate, supportsToolCalling, tool* tags)
    _optional_fl = [
        ("max_output_tokens", "maxOutputTokens"), ("prompt_template", "promptTemplate"),
        ("supports_tool_calling", "supportsToolCalling"),
        ("tool_call_start", "toolCallStart"), ("tool_call_end", "toolCallEnd"),
        ("tool_register_start", "toolRegisterStart"), ("tool_register_end", "toolRegisterEnd"),
        ("tool_response_start", "toolResponseStart"), ("tool_response_end", "toolResponseEnd"),
    ]
    for field, tag in _optional_fl:
        val = ov.pop(field, "") or auto_tags.get(tag, "")
        if val:
            tags[tag] = val
    # Blob tracking tags
    for key in ("blob_name", "blob_prefix", "blob_files"):
        val = ov.pop(key, "")
        if val:
            tags[key] = val
    bc = ov.pop("blob_count", 0)
    if bc > 0:
        tags["blob_count"] = str(bc)
    # Pass-through any remaining overrides
    for k, v in ov.items():
        if v:
            tags[k] = str(v)
    return tags


def _register_model(model_name: str, description: str, tags: dict, *,
                    customer_id: str = None, model_path: str = None) -> str:
    """Register a model in the Azure ML registry.

    Args:
        model_path: Optional path to a directory or file containing the actual
                    model artifacts. When provided, the model is registered with
                    real files so the azureml:// URI resolves for FL downloads.
                    Falls back to a placeholder file when None.
    """
    ml, _ = _client_for(customer_id)
    cleanup_placeholder = False
    if model_path:
        reg_path = model_path
    else:
        temp = UPLOAD_DIR / f"placeholder_{model_name}.txt"
        temp.write_text(f"Model: {model_name}")
        reg_path = str(temp)
        cleanup_placeholder = True
    try:
        for attempt in range(1, 4):
            try:
                m = Model(path=reg_path, name=model_name, type="custom_model",
                          description=description, tags=tags)
                reg = ml.models.create_or_update(m)
                _invalidate_model_caches()
                return str(reg.version)
            except Exception as e:
                if attempt < 3:
                    log.warning(f"Registry write attempt {attempt}/3 failed: {e}")
                    import time as _t; _t.sleep(2 ** attempt)
                else:
                    raise
    finally:
        if cleanup_placeholder:
            temp.unlink(missing_ok=True)


def _is_archive(fn: str) -> bool:
    return fn.lower().endswith((".zip", ".tar.gz", ".tgz"))

def _skip(name: str) -> bool:
    return any(p.startswith((".", "__")) for p in PurePosixPath(name).parts)

def _extract_archive(content: bytes, filename: str) -> list[tuple[str, bytes]]:
    low, entries = filename.lower(), []
    if low.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            entries = [(i.filename, zf.read(i.filename)) for i in zf.infolist() if not i.is_dir() and not _skip(i.filename)]
    elif low.endswith((".tar.gz", ".tgz")):
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tf:
            for m in tf.getmembers():
                if m.isfile() and not _skip(m.name):
                    f = tf.extractfile(m)
                    if f:
                        entries.append((m.name, f.read()))
    return entries


def _strip_common_prefix(paths: list[str]) -> list[str]:
    if not paths:
        return paths
    parts = [PurePosixPath(p).parts for p in paths]
    if len(parts) == 1:
        return [parts[0][-1]]
    n = 0
    for lvl in zip(*parts):
        if len(set(lvl)) == 1:
            n += 1
        else:
            break
    return [str(PurePosixPath(*p[n:])) for p in parts]


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
    return {"status": "ok", "registry": REGISTRY_NAME, "storage": STORAGE_ACCOUNT}

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
                    entry.update({
                        "task": t.get("task", ""),
                        "device": t.get("device", ""),
                        "model_type": t.get("modelType", ""),
                        "size_bytes": int(t.get("file_size_bytes", 0)),
                        "fl_ready": t.get("foundryLocal", "").lower() == "true",
                        "author": t.get("author", ""),
                        "upload_time": t.get("upload_time", ""),
                    })
                except Exception:
                    pass
            models.append(entry)
        return {"registry": REGISTRY_NAME, "models": models}
    cache_key = "all_detail" if detail else "all"
    return _cached(_model_list_cache, cache_key, _fetch)


@app.delete("/models/{name}")
def delete_model(name: str, version: str = Query(None), authorization: str = Header(...)):
    """Delete a model from the registry and optionally its blobs from storage."""
    claims = require_auth(authorization)
    if not check_entitlement(claims, name):
        raise HTTPException(403, f"Not entitled to: {name}")
    cid = claims["customer_id"]
    ml, sa = _client_for(cid)

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
    log.info(f"Delete | {cid} | {name} | versions={deleted_versions}")
    result = {"status": "deleted", "model": name, "versions_deleted": deleted_versions}
    if blob_errors:
        result["blob_warnings"] = blob_errors
    return result


@app.get("/status")
def status():
    """Service status dashboard data."""
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

    return {
        "status": "ok",
        "uptime": f"{days}d {hours}h {minutes}m",
        "uptime_seconds": int(uptime.total_seconds()),
        "started_at": _APP_START.isoformat(),
        "registry": REGISTRY_NAME,
        "storage": STORAGE_ACCOUNT,
        "model_count": model_count,
        "stats": _stats,
    }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    """Live HTML dashboard — opens in any browser."""
    return HTMLResponse(_DASHBOARD_HTML)


@app.get("/models/sync")
def sync_models(authorization: str = Header(...)):
    """Cross-reference registry with blob storage. Shows models that are:
    - in registry and blob (healthy)
    - in blob only (upload succeeded but registration failed)
    - in registry only (blobs missing or deleted)
    """
    claims = require_auth(authorization)
    cid = claims["customer_id"]
    ml, sa = _client_for(cid)
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
    counts = {"synced": sum(1 for r in results if r["status"] == "synced"),
              "blob_only": sum(1 for r in results if r["status"] == "blob_only"),
              "registry_only": sum(1 for r in results if r["status"] == "registry_only")}
    return {"total": len(results), "counts": counts, "models": results}


@app.post("/download")
def download(model: str = Query(...), version: str = Query(...),
             format: str = Query("urls", pattern="^(urls|zip)$"), authorization: str = Header(...)):
    claims = require_auth(authorization)
    if not check_entitlement(claims, model):
        raise HTTPException(403, f"Not entitled to: {model}")
    cid = claims["customer_id"]
    ml, sa = _client_for(cid)
    try:
        info = ml.models.get(name=model, version=version)
    except Exception:
        raise HTTPException(404, f"Model not found: {model} v{version}")
    tags = info.tags or {}
    log.info(f"Download | {cid} | {model} v{version} | format={format}")
    _stats["downloads"] += 1; _stats["last_download"] = datetime.now(timezone.utc).isoformat()
    bp, bf, bs = tags.get("blob_prefix", ""), tags.get("blob_files", ""), tags.get("blob_name", "")
    if bp or bf:
        blob_names = [b.strip() for b in bf.split(",") if b.strip()] if bf else list_blobs(bp, storage_account=sa)
    elif bs:
        blob_names = [bs]
    else:
        return {"model": model, "version": version, "download_url": info.path}
    if not blob_names:
        raise HTTPException(404, f"No files found for {model} v{version}")
    if format == "zip":
        return _build_zip_response(model, version, blob_names, bp, storage_account=sa)
    if len(blob_names) == 1 and not bp:
        return {"model": model, "version": version, "download_url": generate_sas_url(blob_names[0], storage_account=sa), "expires_in": "1 hour"}
    files = [{"file": n, "download_url": generate_sas_url(n, storage_account=sa)} for n in blob_names]
    return {"model": model, "version": version, "file_count": len(files), "files": files, "expires_in": "1 hour"}

def _build_zip_response(model, version, blob_names, blob_prefix, *, storage_account=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in blob_names:
            data = download_blob(name, storage_account=storage_account)
            arc = name[len(blob_prefix):].lstrip("/") if blob_prefix and name.startswith(blob_prefix) else PurePosixPath(name).name
            zf.writestr(arc or name, data)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{model}_v{version}.zip"'})


@app.post("/catalog")
def catalog(request: CatalogRequest, authorization: str = Header(...)):
    claims = require_auth(authorization)
    req = request.indexEntitiesRequest
    page_size = req.pageSize if req and req.pageSize else 100
    if req and req.continuationToken:
        skip, page_size = decode_continuation_token(req.continuationToken)
    else:
        skip = req.skip if req and req.skip else 0
    cid = claims["customer_id"]
    reg = get_customer_registry(cid)
    def _fetch():
        ml, _ = _client_for(cid)
        out = []
        for m in ml.models.list():
            if not check_entitlement(claims, m.name):
                continue
            try:
                info = ml.models.get(name=m.name, version=m.latest_version)
                out.append(build_foundry_model(info, info.tags or {}, registry_name=reg or REGISTRY_NAME))
            except Exception:
                pass
        return out
    models = _cached(_catalog_cache, cid, _fetch)
    total, page, ns = len(models), models[skip:skip + page_size], skip + page_size
    resp = {"indexEntitiesResponse": {"totalCount": total, "value": page, "nextSkip": ns if ns < total else None}}
    if ns < total:
        resp["indexEntitiesResponse"]["continuationToken"] = encode_continuation_token(ns, page_size)
    return resp


@app.post("/catalog/fl")
def catalog_fl(request: FLCatalogQuery = None, authorization: str = Header(...)):
    """Foundry Local native catalog endpoint.

    Returns models in the exact format FL expects, with optional filtering
    by task, device, or modality. Only models tagged foundryLocal=true are included.
    """
    claims = require_auth(authorization)
    cid = claims["customer_id"]
    req = request or FLCatalogQuery()
    page_size = req.pageSize or 50
    if req.continuationToken:
        skip, page_size = decode_continuation_token(req.continuationToken)
    else:
        skip = 0

    def _fetch_fl():
        ml, _ = _client_for(cid)
        reg = get_customer_registry(cid)
        out = []
        for m in ml.models.list():
            if not check_entitlement(claims, m.name):
                continue
            try:
                info = ml.models.get(name=m.name, version=m.latest_version)
                tags = info.tags or {}
                # Only include FL-tagged models
                if tags.get("foundryLocal", "").lower() not in ("true", "test"):
                    continue
                out.append(build_foundry_model(info, tags, registry_name=reg or REGISTRY_NAME))
            except Exception:
                pass
        return out

    models = _cached(_catalog_cache, f"fl_{cid}", _fetch_fl)

    # Apply filters
    filtered = models
    if req.task:
        filtered = [m for m in filtered if m["annotations"]["tags"].get("task", "").lower() == req.task.lower()]
    if req.device:
        filtered = [m for m in filtered if m["properties"]["variantInfo"]["variantMetadata"].get("device", "").lower() == req.device.lower()]
    if req.modality:
        filtered = [m for m in filtered if req.modality.lower() in m["annotations"]["tags"].get("inputModalities", "").lower()]

    total = len(filtered)
    page = filtered[skip:skip + page_size]
    ns = skip + page_size
    resp = {
        "totalCount": total,
        "models": page,
        "nextSkip": ns if ns < total else None,
    }
    if ns < total:
        resp["continuationToken"] = encode_continuation_token(ns, page_size)
    return resp


# ─── FL-native catalog (AzureCatalogUri target) ─────────────────────────
# This is the endpoint FL calls when configured with:
#   additionalSettings: { "AzureCatalogUri": "https://mds.../catalog/foundrylocal/<api-key>" }
#
# Auth: API key in URL path (preferred -- works with all FL versions),
#        or X-API-Key header, or JWT Bearer fallback.
# Request/Response: standard FL indexEntitiesRequest/Response format.
# Ref: https://learn.microsoft.com/azure/ai-foundry/foundry-local/reference/reference-catalog-api


def _fl_catalog_handler(cid: str, request: CatalogRequest = None):
    """Shared FL catalog logic -- returns indexEntitiesResponse for a customer."""
    from .customers import CUSTOMERS
    customer = CUSTOMERS.get(cid, {})
    claims = {"customer_id": cid, "sub": customer.get("sub", cid)}

    req = request.indexEntitiesRequest if request else None
    page_size = req.pageSize if req and req.pageSize else 100
    if req and req.continuationToken:
        skip, page_size = decode_continuation_token(req.continuationToken)
    else:
        skip = req.skip if req and req.skip else 0

    def _fetch_fl_native():
        ml, _ = _client_for(cid)
        reg = get_customer_registry(cid)
        out = []
        for m in ml.models.list():
            if not check_entitlement(claims, m.name):
                continue
            try:
                info = ml.models.get(name=m.name, version=m.latest_version)
                tags = info.tags or {}
                if tags.get("foundryLocal", "").lower() not in ("true", "test"):
                    continue
                out.append(build_foundry_model(info, tags, registry_name=reg or REGISTRY_NAME))
            except Exception:
                pass
        return out

    models = _cached(_catalog_cache, f"flnat_{cid}", _fetch_fl_native)

    total = len(models)
    page = models[skip:skip + page_size]
    ns = skip + page_size
    resp = {
        "indexEntitiesResponse": {
            "totalCount": total,
            "value": page,
            "nextSkip": ns if ns < total else None,
            "continuationToken": encode_continuation_token(ns, page_size) if ns < total else None,
        }
    }
    log.info(f"FL catalog | customer={cid} | {total} models | page {skip}-{skip+len(page)}")
    return resp


@app.post("/catalog/foundrylocal/{api_key}")
def catalog_foundrylocal_keyed(
    api_key: str,
    request: CatalogRequest = None,
):
    """FL catalog endpoint with API key in URL path.

    Use this as the AzureCatalogUri -- guaranteed to work since FL
    just POSTs to the URL without adding custom headers:
        AzureCatalogUri: https://mds.../catalog/foundrylocal/<api-key>
    """
    cid = get_customer_by_api_key(api_key)
    if not cid:
        raise HTTPException(401, "Invalid API key")
    return _fl_catalog_handler(cid, request)


@app.post("/catalog/foundrylocal")
def catalog_foundrylocal(
    request: CatalogRequest = None,
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
        cid = claims["customer_id"]
    else:
        raise HTTPException(401, "X-API-Key or Authorization header required")
    return _fl_catalog_handler(cid, request)


@app.get("/models/{name}/versions")
def list_model_versions(name: str, authorization: str = Header(...)):
    claims = require_auth(authorization)
    if not check_entitlement(claims, name):
        raise HTTPException(403, f"Not entitled to: {name}")
    cid = claims["customer_id"]
    def _fetch():
        ml, _ = _client_for(cid)
        try:
            vers = list(ml.models.list(name=name))
        except Exception:
            vers = []
        if not vers:
            raise HTTPException(404, f"Not found: {name}")
        return {"model": name, "versions": sorted([str(m.version) for m in vers], key=lambda v: int(v) if v.isdigit() else 0, reverse=True)}
    return _cached(_model_versions_cache, (name, cid), _fetch)


@app.get("/models/{name}")
def get_model(name: str, version: str = Query(None), authorization: str = Header(...)):
    claims = require_auth(authorization)
    if not check_entitlement(claims, name):
        raise HTTPException(403, f"Not entitled to: {name}")
    cid = claims["customer_id"]
    reg = get_customer_registry(cid)
    def _fetch():
        ml, _ = _client_for(cid)
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
    return _cached(_model_info_cache, (name, version or "latest", cid), _fetch)


_FIELD_TO_TAG = {
    "alias": "alias", "task": "task", "input_modalities": "inputModalities",
    "output_modalities": "outputModalities", "device": "device", "execution_provider": "executionProvider",
    "model_type": "modelType", "prompt_template": "promptTemplate", "license_id": "license",
    "license_description": "licenseDescription", "max_output_tokens": "maxOutputTokens",
    "supports_tool_calling": "supportsToolCalling", "tool_call_start": "toolCallStart",
    "tool_call_end": "toolCallEnd", "tool_register_start": "toolRegisterStart",
    "tool_register_end": "toolRegisterEnd", "tool_response_start": "toolResponseStart",
    "tool_response_end": "toolResponseEnd", "author": "author",
    "directory_path": "directoryPath", "disable_maap": "disable-maap",
}


@app.post("/upload")
async def upload(
    model_name: str = Form(...), description: str = Form(""),
    alias: str = Form(""), task: str = Form(""),
    input_modalities: str = Form("text"), output_modalities: str = Form("text"),
    device: str = Form("cpu"), execution_provider: str = Form("cpuexecutionprovider"),
    model_type: str = Form("onnx"), prompt_template: str = Form(""),
    license_id: str = Form(""), license_description: str = Form(""),
    max_output_tokens: str = Form(""), supports_tool_calling: str = Form(""),
    tool_call_start: str = Form(""), tool_call_end: str = Form(""),
    tool_register_start: str = Form(""), tool_register_end: str = Form(""),
    tool_response_start: str = Form(""), tool_response_end: str = Form(""),
    disable_maap: str = Form("True"),
    files: List[UploadFile] = File(...),
    authorization: str = Header(...),
):
    claims = require_auth(authorization)
    if not check_entitlement(claims, model_name):
        raise HTTPException(403, f"Not authorized: {model_name}")
    cid = claims["customer_id"]
    _, sa = _client_for(cid)
    nv = _next_ver(model_name, cid)
    blob_prefix = f"{model_name}/v{nv}"
    uploaded, total_bytes, file_entries = [], 0, []
    archive_tags: dict = {}  # metadata from archive-level extraction
    for f in files:
        content = await f.read()
        fname = f.filename or "file"
        if _is_archive(fname):
            # Extract rich metadata from the archive BEFORE unpacking
            archive_tags.update(extract_onnx_metadata(content, fname))
            entries = _extract_archive(content, fname)
            if entries:
                stripped = _strip_common_prefix([e[0] for e in entries])
                file_entries.extend(zip(stripped, [e[1] for e in entries]))
            else:
                file_entries.append((fname, content))
        else:
            file_entries.append((fname, content))
    # For multi-file (non-archive) uploads, scan config files for metadata
    if not archive_tags:
        archive_tags = extract_metadata_from_files(
            {name: data for name, data in file_entries})
    # Stage files to temp directory for registry artifacts + upload to blob
    staging_dir = Path(tempfile.mkdtemp(prefix="mds_reg_"))
    try:
        first_content, first_filename = b"", ""
        for rel, data in file_entries:
            # Upload to blob storage
            bn = f"{blob_prefix}/{rel}"
            upload_to_blob(data, bn, storage_account=sa)
            uploaded.append(bn)
            total_bytes += len(data)
            if not first_content:
                first_content, first_filename = data, rel
            # Stage to temp directory for registry artifacts
            staged = staging_dir / rel
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(data)
        auto_tags = archive_tags or extract_onnx_metadata(first_content, first_filename)
        _l = locals()
        meta = {k: _l.get(k, "") for k in _FIELD_TO_TAG if k in _l}
        multi = len(uploaded) > 1
        tags = _build_upload_tags(
            claims, auto_tags, blob_name=uploaded[0] if not multi else "",
            blob_prefix=blob_prefix if multi else "", blob_files=",".join(uploaded) if multi else "",
            blob_count=len(uploaded), total_bytes=total_bytes, model_name=model_name, **meta)
        # Auto-generate FL description if none provided
        desc = description or build_fl_description(model_name, tags)
        version = _register_model(model_name, desc, tags, customer_id=cid,
                                  model_path=str(staging_dir))
    finally:
        import shutil
        shutil.rmtree(staging_dir, ignore_errors=True)
    log.info(f"Upload | {cid} | {model_name} v{version} | {len(uploaded)} file(s), {total_bytes} bytes")
    _stats["uploads"] += 1; _stats["last_upload"] = datetime.now(timezone.utc).isoformat()
    return {"status": "success", "model": model_name, "version": version,
            "files_uploaded": len(uploaded), "total_bytes": total_bytes}


_SESSION_DIR = os.path.join(tempfile.gettempdir(), "mds_staging_sessions")
os.makedirs(_SESSION_DIR, exist_ok=True)
_sp = lambda sid: os.path.join(_SESSION_DIR, f"{sid}.json")

def _save_session(sid: str, data: dict):
    with open(_sp(sid), "w") as f:
        json.dump(data, f)

def _load_session(sid: str) -> dict | None:
    if not os.path.exists(_sp(sid)):
        return None
    try:
        with open(_sp(sid)) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

def _delete_session(sid: str):
    try:
        os.remove(_sp(sid))
    except OSError:
        pass


class UploadBeginRequest(BaseModel):
    model_name: str; description: str = ""; alias: str = ""; task: str = ""
    input_modalities: str = "text"; output_modalities: str = "text"
    device: str = "cpu"; execution_provider: str = "cpuexecutionprovider"; model_type: str = "onnx"
    author: str = ""; directory_path: str = ""; prompt_template: str = ""
    license_id: str = ""; license_description: str = ""; max_output_tokens: str = ""
    supports_tool_calling: str = ""; tool_call_start: str = ""; tool_call_end: str = ""
    tool_register_start: str = ""; tool_register_end: str = ""
    tool_response_start: str = ""; tool_response_end: str = ""
    disable_maap: str = "True"

class UploadCompleteRequest(BaseModel):
    session_id: str; description: str | None = None; task: str | None = None; model_type: str | None = None


@app.post("/upload/begin")
def upload_begin(req: UploadBeginRequest, authorization: str = Header(...)):
    claims = require_auth(authorization)
    if not check_entitlement(claims, req.model_name):
        raise HTTPException(403, f"Not authorized: {req.model_name}")
    cid = claims["customer_id"]
    _, sa = _client_for(cid)
    nv = _next_ver(req.model_name, cid)
    sid = uuid.uuid4().hex[:16]
    bp = f"{req.model_name}/v{nv}"
    url = generate_upload_sas_url(bp, hours=4, storage_account=sa)
    rd = req.model_dump()
    meta = {tag: rd.get(field, "") for field, tag in _FIELD_TO_TAG.items() if field in rd}
    meta["alias"] = meta.get("alias", "") or req.model_name
    meta["directoryPath"] = meta.get("directoryPath", "") or req.model_name
    meta["author"] = meta.get("author", "") or "Microsoft"
    meta["disable-maap"] = meta.pop("disable-maap", "") or rd.get("disable_maap", "True")
    _save_session(sid, {"customer_id": cid, "storage_account": sa, "model_name": req.model_name,
                        "description": req.description, "next_version": nv, "blob_prefix": bp,
                        "metadata": meta, "created": datetime.now(timezone.utc).isoformat()})
    log.info(f"Staging | {cid} | {req.model_name} v{nv} | session={sid}")
    return {"session_id": sid, "upload_url": url, "blob_prefix": bp, "expires_in": "4 hours",
            "instructions": f'Upload model files using: azcopy copy ./model_folder "{url}" --recursive'}


@app.post("/upload/complete")
def upload_complete(req: UploadCompleteRequest, authorization: str = Header(...)):
    claims = require_auth(authorization)
    session = _load_session(req.session_id)
    if not session:
        raise HTTPException(404, "Staging session not found or expired")
    cid = claims["customer_id"]
    if session["customer_id"] != cid:
        raise HTTPException(403, "Session belongs to a different customer")
    mn = session["model_name"]
    if not check_entitlement(claims, mn):
        raise HTTPException(403, f"Not authorized: {mn}")
    sa = session.get("storage_account")
    blobs = list_blobs(session["blob_prefix"], storage_account=sa)
    if not blobs:
        raise HTTPException(400, f"No files found under {session['blob_prefix']}. Upload files before calling /upload/complete.")
    meta = session["metadata"]
    tags = {"uploaded_by": cid, "blob_prefix": session["blob_prefix"], "blob_files": ",".join(blobs),
            "blob_count": str(len(blobs)), "upload_time": datetime.now(timezone.utc).isoformat(),
            "alias": meta.get("alias", mn), "author": meta.get("author", "Microsoft"),
            "directoryPath": meta.get("directoryPath", mn),
            "task": req.task or meta.get("task", "custom"),
            "inputModalities": meta.get("inputModalities", "text"), "outputModalities": meta.get("outputModalities", "text"),
            "device": meta.get("device", "cpu"), "executionProvider": meta.get("executionProvider", "cpuexecutionprovider"),
            "modelType": req.model_type or meta.get("modelType", "onnx"),
            "foundryLocal": "true", "disable-maap": meta.get("disable-maap", "True"), "staged_upload": "true"}
    for k, v in meta.items():
        if v and k not in tags:
            tags[k] = v
    desc = req.description or session["description"] or build_fl_description(mn, tags)
    # Download blobs to a temp directory so registry gets real artifacts
    staging_dir = Path(tempfile.mkdtemp(prefix="mds_staged_"))
    try:
        for blob_name in blobs:
            # Strip the blob_prefix to get the relative file path
            rel = blob_name[len(session["blob_prefix"]):].lstrip("/")
            if not rel:
                rel = blob_name.split("/")[-1]
            staged = staging_dir / rel
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(download_blob(blob_name, storage_account=sa))
        # Extract metadata from staged files for auto-tag population
        staged_files = {}
        for p in staging_dir.rglob("*"):
            if p.is_file():
                staged_files[str(p.relative_to(staging_dir))] = p.read_bytes()
        if staged_files:
            file_tags = extract_metadata_from_files(staged_files)
            for k, v in file_tags.items():
                if v and k not in tags:
                    tags[k] = v
        version = _register_model(mn, desc, tags, customer_id=cid,
                                  model_path=str(staging_dir))
    finally:
        import shutil
        shutil.rmtree(staging_dir, ignore_errors=True)
    log.info(f"Staged complete | {cid} | {mn} v{version} | {len(blobs)} blobs")
    _stats["uploads"] += 1; _stats["last_upload"] = datetime.now(timezone.utc).isoformat()
    _delete_session(req.session_id)
    return {"status": "success", "model": mn, "version": version, "blobs_registered": len(blobs), "blob_prefix": session["blob_prefix"]}


# ─── Dashboard HTML ─────────────────────────────────────────────────────
_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MDS Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:#0f1117;color:#e4e4e7}
.header{background:linear-gradient(135deg,#1e3a5f,#0d9488);padding:2rem;text-align:center}
.header h1{font-size:1.8rem;font-weight:600;letter-spacing:-.02em}
.header p{color:#94a3b8;margin-top:.3rem;font-size:.9rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:1rem;padding:1.5rem;max-width:1200px;margin:0 auto}
.card{background:#1e1e2e;border-radius:12px;padding:1.5rem;border:1px solid #2e2e3e;transition:border-color .2s}
.card:hover{border-color:#0d9488}
.card .label{font-size:.75rem;text-transform:uppercase;letter-spacing:.08em;color:#71717a;margin-bottom:.5rem}
.card .value{font-size:2rem;font-weight:700;color:#f4f4f5}
.card .sub{font-size:.8rem;color:#a1a1aa;margin-top:.3rem}
.card.ok .value{color:#34d399} .card.warn .value{color:#fbbf24}
.models-section{max-width:1200px;margin:0 auto;padding:0 1.5rem 2rem}
.models-section h2{font-size:1.1rem;margin-bottom:1rem;color:#a1a1aa}
table{width:100%;border-collapse:collapse;background:#1e1e2e;border-radius:12px;overflow:hidden;border:1px solid #2e2e3e}
th{background:#16161e;padding:.75rem 1rem;text-align:left;font-size:.75rem;text-transform:uppercase;letter-spacing:.08em;color:#71717a}
td{padding:.65rem 1rem;border-top:1px solid #2e2e3e;font-size:.85rem}
tr:hover td{background:#262636}
.badge{display:inline-block;padding:2px 8px;border-radius:9999px;font-size:.7rem;font-weight:600}
.badge-green{background:#064e3b;color:#34d399} .badge-blue{background:#1e3a5f;color:#60a5fa}
.badge-gray{background:#27272a;color:#a1a1aa}
.refresh{text-align:center;padding:1rem;color:#52525b;font-size:.8rem}
#error{display:none;text-align:center;padding:1rem;color:#f87171}
</style></head><body>
<div class="header"><h1>Model Distribution Service</h1><p>Private Model Registry &amp; Distribution</p></div>
<div class="grid">
  <div class="card ok" id="c-status"><div class="label">Status</div><div class="value" id="v-status">—</div><div class="sub" id="v-uptime"></div></div>
  <div class="card" id="c-models"><div class="label">Models</div><div class="value" id="v-models">—</div><div class="sub" id="v-registry"></div></div>
  <div class="card" id="c-uploads"><div class="label">Uploads</div><div class="value" id="v-uploads">—</div><div class="sub" id="v-last-upload">—</div></div>
  <div class="card" id="c-downloads"><div class="label">Downloads</div><div class="value" id="v-downloads">—</div><div class="sub" id="v-last-download">—</div></div>
</div>
<div class="models-section"><h2>Registered Models</h2>
  <table><thead><tr><th>Name</th><th>Version</th><th>Task</th><th>Device</th><th>Type</th><th>Size</th><th>FL</th></tr></thead>
  <tbody id="model-rows"><tr><td colspan="7" style="text-align:center;color:#52525b">Loading…</td></tr></tbody></table>
</div>
<div class="refresh">Auto-refreshes every 30s · <span id="last-refresh"></span></div>
<div id="error"></div>
<script>
const BASE=window.location.origin;
function fmt(b){if(!b||b<=0)return'—';if(b>1e9)return(b/1e9).toFixed(1)+'GB';if(b>1e6)return(b/1e6).toFixed(1)+'MB';if(b>1e3)return(b/1e3).toFixed(1)+'KB';return b+'B'}
function ago(iso){if(!iso)return'—';const d=new Date(iso),n=Date.now(),s=Math.floor((n-d)/1000);if(s<60)return s+'s ago';if(s<3600)return Math.floor(s/60)+'m ago';if(s<86400)return Math.floor(s/3600)+'h ago';return Math.floor(s/86400)+'d ago'}
async function refresh(){
  try{
    const[st,ml]=await Promise.all([fetch(BASE+'/status').then(r=>r.json()),fetch(BASE+'/models?detail=true').then(r=>r.json())]);
    document.getElementById('v-status').textContent=st.status==='ok'?'Healthy':'Degraded';
    document.getElementById('c-status').className='card '+(st.status==='ok'?'ok':'warn');
    document.getElementById('v-uptime').textContent='Uptime: '+st.uptime;
    document.getElementById('v-models').textContent=st.model_count>=0?st.model_count:'?';
    document.getElementById('v-registry').textContent=st.registry;
    document.getElementById('v-uploads').textContent=st.stats.uploads;
    document.getElementById('v-last-upload').textContent='Last: '+ago(st.stats.last_upload);
    document.getElementById('v-downloads').textContent=st.stats.downloads;
    document.getElementById('v-last-download').textContent='Last: '+ago(st.stats.last_download);
    const rows=ml.models.map(m=>`<tr><td><strong>${m.name}</strong></td><td>v${m.latest_version}</td>`
      +`<td>${m.task||'—'}</td><td>${m.device||'—'}</td><td>${m.model_type||'—'}</td>`
      +`<td>${fmt(m.size_bytes)}</td>`
      +`<td>${m.fl_ready?'<span class="badge badge-green">✓ Ready</span>':'<span class="badge badge-gray">—</span>'}</td></tr>`).join('');
    document.getElementById('model-rows').innerHTML=rows||'<tr><td colspan="7" style="text-align:center;color:#52525b">No models</td></tr>';
    document.getElementById('last-refresh').textContent=new Date().toLocaleTimeString();
    document.getElementById('error').style.display='none';
  }catch(e){document.getElementById('error').textContent='Failed to load: '+e;document.getElementById('error').style.display='block'}
}
refresh();setInterval(refresh,30000);
</script></body></html>"""