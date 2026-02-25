import io, json, logging, os, tarfile, tempfile, threading, traceback, uuid, zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import List

from cachetools import TTLCache
from azure.ai.ml.entities import Model
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .auth import check_entitlement, require_auth
from .azure_clients import (REGISTRY_NAME, STORAGE_ACCOUNT, download_blob, generate_sas_url,
                             generate_upload_sas_url, get_ml_client, list_blob_prefixes, list_blobs, upload_to_blob)
from .catalog import CatalogRequest, FLCatalogQuery, build_foundry_model, decode_continuation_token, encode_continuation_token
from .customers import get_customer_by_api_key, get_customer_registry, get_customer_storage
from .metadata import extract_onnx_metadata

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

@app.exception_handler(Exception)
async def _unhandled(request, exc):
    log.error(f"{request.method} {request.url.path}: {exc}\n{traceback.format_exc()}")
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})

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


def _register_model(model_name: str, description: str, tags: dict, *, customer_id: str = None) -> str:
    ml, _ = _client_for(customer_id)
    temp = UPLOAD_DIR / f"placeholder_{model_name}.txt"
    temp.write_text(f"Model: {model_name}")
    try:
        for attempt in range(1, 4):
            try:
                m = Model(path=str(temp), name=model_name, type="custom_model",
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
def list_models():
    return _cached(_model_list_cache, "all", lambda: {
        "registry": REGISTRY_NAME,
        "models": [{"name": m.name, "latest_version": m.latest_version} for m in get_ml_client().models.list()],
    })


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
    for f in files:
        content = await f.read()
        fname = f.filename or "file"
        if _is_archive(fname):
            entries = _extract_archive(content, fname)
            if entries:
                stripped = _strip_common_prefix([e[0] for e in entries])
                file_entries.extend(zip(stripped, [e[1] for e in entries]))
            else:
                file_entries.append((fname, content))
        else:
            file_entries.append((fname, content))
    first_content, first_filename = b"", ""
    for rel, data in file_entries:
        bn = f"{blob_prefix}/{rel}"
        upload_to_blob(data, bn, storage_account=sa)
        uploaded.append(bn)
        total_bytes += len(data)
        if not first_content:
            first_content, first_filename = data, rel
    auto_tags = extract_onnx_metadata(first_content, first_filename)
    _l = locals()
    meta = {k: _l.get(k, "") for k in _FIELD_TO_TAG if k in _l}
    multi = len(uploaded) > 1
    tags = _build_upload_tags(
        claims, auto_tags, blob_name=uploaded[0] if not multi else "",
        blob_prefix=blob_prefix if multi else "", blob_files=",".join(uploaded) if multi else "",
        blob_count=len(uploaded), total_bytes=total_bytes, model_name=model_name, **meta)
    version = _register_model(model_name, description, tags, customer_id=cid)
    log.info(f"Upload | {cid} | {model_name} v{version} | {len(uploaded)} file(s), {total_bytes} bytes")
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
    version = _register_model(mn, req.description or session["description"], tags, customer_id=cid)
    log.info(f"Staged complete | {cid} | {mn} v{version} | {len(blobs)} blobs")
    _delete_session(req.session_id)
    return {"status": "success", "model": mn, "version": version, "blobs_registered": len(blobs), "blob_prefix": session["blob_prefix"]}
