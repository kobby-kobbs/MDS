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
                             generate_upload_sas_url, get_ml_client, list_blobs, upload_to_blob)
from .catalog import CatalogRequest, build_foundry_model, decode_continuation_token, encode_continuation_token
from .customers import get_customer_registry, get_customer_storage
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
            "alias": ov.pop("alias", "") or model_name,
            "task": ov.pop("task", "") or auto_tags.get("task", "custom"),
            "inputModalities": ov.pop("input_modalities", "text") or auto_tags.get("inputModalities", "text"),
            "outputModalities": ov.pop("output_modalities", "text") or auto_tags.get("outputModalities", "text"),
            "device": ov.pop("device", "cpu"),
            "executionProvider": ov.pop("execution_provider", "cpuexecutionprovider"),
            "modelType": ov.pop("model_type", "onnx") or auto_tags.get("modelType", "onnx"),
            "foundryLocal": "true"}
    for key in ("blob_name", "blob_prefix", "blob_files"):
        val = ov.pop(key, "")
        if val:
            tags[key] = val
    bc = ov.pop("blob_count", 0)
    if bc > 0:
        tags["blob_count"] = str(bc)
    for k, v in ov.items():
        if v:
            tags[k] = str(v)
    return tags


def _register_model(model_name: str, description: str, tags: dict, *, customer_id: str = None) -> str:
    temp = UPLOAD_DIR / f"placeholder_{model_name}.txt"
    temp.write_text(f"Model: {model_name}")
    try:
        m = Model(path=str(temp), name=model_name, type="custom_model", description=description, tags=tags)
        ml, _ = _client_for(customer_id)
        reg = ml.models.create_or_update(m)
        _invalidate_model_caches()
        return str(reg.version)
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
    def _fetch():
        ml, _ = _client_for(cid)
        out = []
        for m in ml.models.list():
            if not check_entitlement(claims, m.name):
                continue
            try:
                info = ml.models.get(name=m.name, version=m.latest_version)
                out.append(build_foundry_model(info, info.tags or {}))
            except Exception:
                pass
        return out
    models = _cached(_catalog_cache, cid, _fetch)
    total, page, ns = len(models), models[skip:skip + page_size], skip + page_size
    resp = {"indexEntitiesResponse": {"totalCount": total, "value": page, "nextSkip": ns if ns < total else None}}
    if ns < total:
        resp["indexEntitiesResponse"]["continuationToken"] = encode_continuation_token(ns, page_size)
    return resp


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
        return build_foundry_model(info, info.tags or {})
    return _cached(_model_info_cache, (name, version or "latest", cid), _fetch)


_FIELD_TO_TAG = {
    "alias": "alias", "task": "task", "input_modalities": "inputModalities",
    "output_modalities": "outputModalities", "device": "device", "execution_provider": "executionProvider",
    "model_type": "modelType", "prompt_template": "promptTemplate", "license_id": "license",
    "license_description": "licenseDescription", "max_output_tokens": "maxOutputTokens",
    "supports_tool_calling": "supportsToolCalling", "tool_call_start": "toolCallStart",
    "tool_call_end": "toolCallEnd", "tool_register_start": "toolRegisterStart",
    "tool_register_end": "toolRegisterEnd", "tool_response_start": "toolResponseStart",
    "tool_response_end": "toolResponseEnd", "framework": "framework",
    "framework_version": "frameworkVersion", "model_hash": "modelHash",
    "training_data_version": "trainingDataVersion", "expiration_date": "expirationDate",
    "dependencies": "dependencies", "parent_model": "parentModel",
    "usage_guidelines": "usageGuidelines", "metrics": "metrics",
    "author": "author", "directory_path": "directoryPath",
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
    framework: str = Form(""), framework_version: str = Form(""),
    model_hash: str = Form(""), training_data_version: str = Form(""),
    expiration_date: str = Form(""), dependencies: str = Form(""),
    parent_model: str = Form(""), usage_guidelines: str = Form(""),
    metrics: str = Form(""), files: List[UploadFile] = File(...),
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
            "alias": meta.get("alias", mn), "task": req.task or meta.get("task", "custom"),
            "inputModalities": meta.get("inputModalities", "text"), "outputModalities": meta.get("outputModalities", "text"),
            "device": meta.get("device", "cpu"), "executionProvider": meta.get("executionProvider", "cpuexecutionprovider"),
            "modelType": req.model_type or meta.get("modelType", "onnx"), "foundryLocal": "true", "staged_upload": "true"}
    for k, v in meta.items():
        if v and k not in tags:
            tags[k] = v
    version = _register_model(mn, req.description or session["description"], tags, customer_id=cid)
    log.info(f"Staged complete | {cid} | {mn} v{version} | {len(blobs)} blobs")
    _delete_session(req.session_id)
    return {"status": "success", "model": mn, "version": version, "blobs_registered": len(blobs), "blob_prefix": session["blob_prefix"]}
