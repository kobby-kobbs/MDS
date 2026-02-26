"""
MDS SDK Client -- Interactive & CLI modes.
Interactive (demo): python mds_cli.py        CLI: python mds_cli.py list|download|upload|...
Env: MDS_BASE_URL, MDS_TOKEN, MDS_PRIVATE_KEY_PATH
"""
__version__ = "0.5.0"

import argparse, io, json, os, sys, time, zipfile
from pathlib import Path, PurePosixPath
import requests

BASE_URL = os.getenv("MDS_BASE_URL", "https://mds-model-distribution.azurewebsites.net")
FL_URL = os.getenv("FL_SERVICE_URL", "http://localhost:60076")
_TOKEN = os.getenv("MDS_TOKEN", "")

def _set_base_url(url: str):
    global BASE_URL
    BASE_URL = url

def _get_token() -> str:
    if _TOKEN: return _TOKEN
    key_path = os.getenv("MDS_PRIVATE_KEY_PATH", "")
    if not key_path or not os.path.exists(key_path):
        return ""
    import jwt; from datetime import datetime, timedelta, timezone
    with open(key_path) as f: private_key = f.read()
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": os.getenv("MDS_CUSTOMER_ID", "phonepe-india"),
        "iss": os.getenv("MDS_ISSUER", "https://auth.phonepe.com"),
        "aud": "model-distribution-service", "iat": now, "exp": now + timedelta(hours=1),
    }, private_key, algorithm="RS256")

def _headers():
    t = _get_token()
    return {"Authorization": f"Bearer {t}"} if t else {}

# -- Shared download helpers ------------------------------------------------------

def _human(n):
    for u in ["", "KB", "MB", "GB"]: 
        if n < 1024: return f"{n:.1f} {u}" if u else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"

def _download_zip(model, version, out_dir):
    print("  Requesting ZIP from server (server-side packaging)...")
    r = requests.post(f"{BASE_URL}/download", params={"model": model, "version": version, "format": "zip"},
                      headers=_headers(), timeout=600, stream=True)
    r.raise_for_status()
    total = int(r.headers.get("content-length", 0))
    downloaded, buf = 0, io.BytesIO()
    t0 = time.time()
    for chunk in r.iter_content(chunk_size=1*1024*1024):
        buf.write(chunk); downloaded += len(chunk)
        elapsed = time.time() - t0
        speed = _human(downloaded / elapsed) if elapsed > 0 else "?"
        if total > 0:
            pct = downloaded * 100 // total; bar = "#" * (pct // 5) + "-" * (20 - pct // 5)
            print(f"\r  [{bar}] {pct:>3}% {_human(downloaded)}/{_human(total)} @ {speed}/s", end="", flush=True)
        else:
            print(f"\r  Downloading... {_human(downloaded)} @ {speed}/s", end="", flush=True)
    print(f"\r  ZIP received: {_human(downloaded)}{' '*30}")
    buf.seek(0); out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(buf) as zf:
        entries = [i for i in zf.infolist() if not i.is_dir()]
        for idx, info in enumerate(entries, 1):
            target = out_dir / info.filename; target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info.filename))
            print(f"  [{idx}/{len(entries)}] {info.filename} ({_human(info.file_size)})")

def _download_urls(model, version, out_dir):
    print("  Fetching download URLs...")
    r = requests.post(f"{BASE_URL}/download", params={"model": model, "version": version},
                      headers=_headers(), timeout=60)
    r.raise_for_status(); data = r.json()
    if "download_url" in data and "files" not in data:
        out_dir.mkdir(parents=True, exist_ok=True); url = data["download_url"]
        _download_file(url, out_dir / (PurePosixPath(url.split("?")[0]).name or f"{model}_v{version}"), 1, 1)
        return
    files = data.get("files", [])
    if not files: print("  [WARN] No files returned."); return
    print(f"  {len(files)} file(s) to download"); out_dir.mkdir(parents=True, exist_ok=True)
    for idx, entry in enumerate(files, 1):
        parts = PurePosixPath(entry["file"]).parts
        rel = str(PurePosixPath(*parts[2:])) if len(parts) > 2 and parts[1][:1] == "v" and parts[1][1:].isdigit() else PurePosixPath(entry["file"]).name
        target = out_dir / rel; target.parent.mkdir(parents=True, exist_ok=True)
        _download_file(entry["download_url"], target, idx, len(files))

def _download_file(url, target, file_num=0, file_total=0):
    r = requests.get(url, stream=True, timeout=600); r.raise_for_status()
    total, downloaded = int(r.headers.get("content-length", 0)), 0
    t0 = time.time()
    label = f"[{file_num}/{file_total}] " if file_total else ""
    with open(target, "wb") as f:
        for chunk in r.iter_content(chunk_size=4*1024*1024):
            f.write(chunk); downloaded += len(chunk)
            elapsed = time.time() - t0
            speed = _human(downloaded / elapsed) if elapsed > 0 else "?"
            if total > 0:
                pct = downloaded * 100 // total; bar = "#" * (pct // 5) + "-" * (20 - pct // 5)
                print(f"\r  {label}[{bar}] {pct:>3}% {target.name} ({_human(downloaded)}/{_human(total)}) @ {speed}/s", end="", flush=True)
            else:
                print(f"\r  {label}{target.name} {_human(downloaded)} @ {speed}/s", end="", flush=True)
    elapsed = time.time() - t0
    print(f"\r  {label}{target.name:<40} {_human(downloaded):>10}  {elapsed:.1f}s{' '*20}")

def _download_via_sdk(model, version, out_dir):
    """Download using azure-storage-blob SDK with SAS URLs for parallel/fast downloads."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    try:
        from azure.storage.blob import BlobClient
    except ImportError:
        print("  [ERROR] Install azure-storage-blob:"); print("    pip install azure-storage-blob"); return
    # Get SAS URLs from MDS API (no local Azure credentials needed)
    print("  Fetching download URLs from MDS...")
    r = requests.post(f"{BASE_URL}/download", params={"model": model, "version": version}, headers=_headers(), timeout=60)
    r.raise_for_status(); data = r.json()
    if "download_url" in data and "files" not in data:
        print("  Single file model, using URL download..."); out_dir.mkdir(parents=True, exist_ok=True)
        _download_file(data["download_url"], out_dir / (PurePosixPath(data["download_url"].split("?")[0]).name or model), 1, 1)
        return
    files = data.get("files", [])
    if not files: print("  [WARN] No files returned."); return
    out_dir.mkdir(parents=True, exist_ok=True)
    total_files = len(files)
    total_bytes, downloaded_bytes = 0, 0
    lock = threading.Lock()
    t0 = time.time()
    WORKERS = min(6, total_files)  # parallel file downloads

    def _dl_one(idx, entry):
        nonlocal downloaded_bytes
        blob_name = entry["file"]
        sas_url = entry["download_url"]
        parts = PurePosixPath(blob_name).parts
        rel = str(PurePosixPath(*parts[2:])) if len(parts) > 2 and parts[1][:1] == "v" and parts[1][1:].isdigit() else PurePosixPath(blob_name).name
        target = out_dir / rel; target.parent.mkdir(parents=True, exist_ok=True)
        # Use Azure SDK for parallel chunk download (max_concurrency)
        blob_client = BlobClient.from_blob_url(sas_url)
        stream = blob_client.download_blob(max_concurrency=4)
        data = stream.readall()
        target.write_bytes(data)
        fsize = len(data)
        with lock:
            downloaded_bytes += fsize
            elapsed = time.time() - t0
            speed = _human(downloaded_bytes / elapsed) + "/s" if elapsed > 0.1 else "---"
            print(f"  [{idx}/{total_files}] {rel:<40} {_human(fsize):>10}  ({speed} overall)")
        return fsize

    print(f"  {total_files} file(s) to download (parallel={WORKERS})")
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(_dl_one, i, entry): i for i, entry in enumerate(files, 1)}
        for future in as_completed(futures):
            total_bytes += future.result()
    elapsed = time.time() - t0
    speed = _human(total_bytes / elapsed) + "/s" if elapsed > 0 else "?"
    print(f"  Download complete: {_human(total_bytes)} in {elapsed:.1f}s ({speed})")

# -- Shared upload helpers --------------------------------------------------------

def _finalize_staged(session_id, start_time):
    """Call /upload/complete with retry logic and long timeout."""
    print(f"  Finalizing (registering model in registry)...")
    for attempt in range(1, 4):
        try:
            r = requests.post(f"{BASE_URL}/upload/complete", json={"session_id": session_id},
                              headers=_headers(), timeout=300)
            r.raise_for_status(); data = r.json()
            elapsed = time.time() - start_time
            print(f"  Status: {data.get('status','?')}  Model: {data.get('model','?')} v{data.get('version','?')}")
            print(f"  Blobs: {data.get('blobs_registered','?')}  Total time: {elapsed:.1f}s")
            return
        except requests.exceptions.ReadTimeout:
            if attempt < 3:
                print(f"  [retry {attempt}/3] Server still processing, retrying in 10s...")
                time.sleep(10)
            else:
                print(f"  [WARN] Server timed out on registry write. Files are in blob storage.")
                print(f"  You can retry: POST {BASE_URL}/upload/complete {{\"session_id\": \"{session_id}\"}}")
        except Exception as e:
            if attempt < 3:
                print(f"  [retry {attempt}/3] {e}")
                time.sleep(5)
            else:
                print(f"  [ERROR] Finalize failed: {e}")
                print(f"  Files are in blob. Retry: POST {BASE_URL}/upload/complete {{\"session_id\": \"{session_id}\"}}")

def _collect_files(source):
    source = Path(source)
    if not source.exists(): print(f"[ERROR] Not found: {source}"); sys.exit(1)
    if source.is_file(): return [(source.name, source)]
    fl = sorted(f for f in source.rglob("*") if f.is_file()
                and not any(p.startswith((".", "__")) for p in f.relative_to(source).parts))
    if not fl: print(f"[ERROR] No files in: {source}"); sys.exit(1)
    return [(str(f.relative_to(source)), f) for f in fl]

def _do_upload(model_name, source, task=None, device=None, description=None):
    pairs = _collect_files(source)
    total_size = sum(p.stat().st_size for _, p in pairs)
    form = {"model_name": model_name}
    if task: form["task"] = task
    if device: form["device"] = device
    if description: form["description"] = description
    print(f"  Uploading {len(pairs)} file(s) ({_human(total_size)})...")
    for i, (rel, p) in enumerate(pairs, 1):
        print(f"    [{i}/{len(pairs)}] {rel:<40} {_human(p.stat().st_size):>10}")
    # Use a progress-tracking wrapper around the multipart body
    files = [("files", (rel, open(p, "rb"), "application/octet-stream")) for rel, p in pairs]
    req = requests.Request("POST", f"{BASE_URL}/upload", data=form, files=files, headers=_headers())
    prepared = req.prepare()
    for _, (_, fh, _) in files:
        if hasattr(fh, "close"): fh.close()
    body_size = int(prepared.headers.get("Content-Length", 0)) or total_size
    # Wrap body with progress
    class ProgressReader:
        def __init__(self, data, total):
            self._data = data if hasattr(data, 'read') else io.BytesIO(data if isinstance(data, bytes) else data.encode())
            self._total, self._sent, self._t0 = total, 0, time.time()
        def read(self, size=-1):
            chunk = self._data.read(size)
            if chunk:
                self._sent += len(chunk)
                elapsed = time.time() - self._t0
                speed = _human(self._sent / elapsed) + "/s" if elapsed > 0 else "?"
                if self._total > 0:
                    pct = self._sent * 100 // self._total
                    bar = "#" * (pct // 5) + "-" * (20 - pct // 5)
                    print(f"\r  [{bar}] {pct:>3}% {_human(self._sent)}/{_human(self._total)} @ {speed}  ", end="", flush=True)
                else:
                    print(f"\r  Sent: {_human(self._sent)} @ {speed}  ", end="", flush=True)
            return chunk
        def __len__(self): return self._total
    start = time.time()
    prepared.body = ProgressReader(prepared.body, body_size)
    s = requests.Session()
    r = s.send(prepared, timeout=600)
    elapsed = time.time() - start
    print(f"\r  Upload complete.{' '*50}")
    r.raise_for_status(); data = r.json()
    print(f"  Status: {data.get('status','?')}  Model: {data.get('model','?')} v{data.get('version','?')}")
    print(f"  Files: {data.get('files_uploaded','?')}  Bytes: {data.get('total_bytes',0):,}  Time: {elapsed:.1f}s")

def _do_staged_upload(model_name, source, task=None, device=None, description=None, no_wait=False):
    pairs = _collect_files(source)
    total_size = sum(p.stat().st_size for _, p in pairs)
    body = {"model_name": model_name}
    if task: body["task"] = task
    if device: body["device"] = device
    if description: body["description"] = description
    print(f"  Files: {len(pairs)} ({_human(total_size)})")
    for i, (rel, p) in enumerate(pairs, 1):
        print(f"    [{i}/{len(pairs)}] {rel:<40} {_human(p.stat().st_size):>10}")
    r = requests.post(f"{BASE_URL}/upload/begin", json=body, headers=_headers(), timeout=30)
    r.raise_for_status(); begin = r.json()
    sid, upload_url = begin["session_id"], begin["upload_url"]
    print(f"  Session: {sid}  Prefix: {begin['blob_prefix']}")
    if no_wait:
        print(f'  azcopy copy "{source}" "{upload_url}" --recursive')
        print(f'  POST {BASE_URL}/upload/complete  {{"session_id": "{sid}"}}'); return
    import subprocess
    print(f"  Starting azcopy transfer...")
    start = time.time()
    # Let azcopy show its own progress by NOT capturing output
    result = subprocess.run(["azcopy", "copy", str(source), upload_url, "--recursive"])
    elapsed = time.time() - start
    if result.returncode != 0:
        print(f"  [ERROR] azcopy failed (exit code {result.returncode})"); return
    print(f"  azcopy transfer done in {elapsed:.1f}s ({_human(total_size / elapsed if elapsed > 0 else 0)}/s)")
    _finalize_staged(sid, start)

def _do_staged_upload_sdk(model_name, source, task=None, device=None, description=None):
    """Staged upload using Azure SDK -- parallel block staging for speed."""
    from urllib.parse import urlparse
    from concurrent.futures import ThreadPoolExecutor, as_completed
    pairs = _collect_files(source)
    total_size = sum(p.stat().st_size for _, p in pairs)
    body = {"model_name": model_name}
    if task: body["task"] = task
    if device: body["device"] = device
    if description: body["description"] = description
    print(f"  Files: {len(pairs)} ({_human(total_size)})")
    for i, (rel, p) in enumerate(pairs, 1):
        print(f"    [{i}/{len(pairs)}] {rel:<40} {_human(p.stat().st_size):>10}")
    r = requests.post(f"{BASE_URL}/upload/begin", json=body, headers=_headers(), timeout=30)
    r.raise_for_status(); begin = r.json()
    sid, upload_url = begin["session_id"], begin["upload_url"]
    prefix = begin["blob_prefix"]
    print(f"  Session: {sid}  Prefix: {prefix}")
    parsed = urlparse(upload_url)
    account_url = f"{parsed.scheme}://{parsed.hostname}"
    container = parsed.path.strip("/").split("/")[0]
    sas_token = parsed.query
    from azure.storage.blob import BlobServiceClient, BlobBlock
    import uuid, threading
    blob_svc = BlobServiceClient(account_url=account_url, credential=sas_token)
    container_client = blob_svc.get_container_client(container)
    start = time.time()
    uploaded_bytes = 0
    lock = threading.Lock()
    BLOCK_SIZE = 4 * 1024 * 1024  # 4 MB blocks (smaller = more parallelism)
    WORKERS = 8  # concurrent block uploads

    def _upload_small(blob_client, p, fsize):
        """Small file: single put with retry."""
        for attempt in range(3):
            try:
                with open(p, "rb") as fh:
                    blob_client.upload_blob(fh, length=fsize, overwrite=True, blob_type="BlockBlob")
                return
            except Exception as e:
                if attempt == 2: raise
                time.sleep(2 ** attempt)

    def _upload_large_parallel(i, blob_client, p, fsize, prev_bytes):
        """Large file: parallel block staging with live progress."""
        # Read all blocks first
        blocks = []
        with open(p, "rb") as fh:
            while True:
                chunk = fh.read(BLOCK_SIZE)
                if not chunk: break
                bid = str(uuid.uuid4())
                blocks.append((bid, chunk))
        staged_ids = []
        completed_bytes = 0
        file_t0 = time.time()

        def _stage_block(bid, data):
            for attempt in range(3):
                try:
                    blob_client.stage_block(bid, io.BytesIO(data), length=len(data))
                    return bid, len(data)
                except Exception as e:
                    if attempt == 2: raise
                    time.sleep(2 ** attempt)

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(_stage_block, bid, data): bid for bid, data in blocks}
            for future in as_completed(futures):
                bid, nbytes = future.result()
                staged_ids.append(bid)
                with lock:
                    nonlocal uploaded_bytes
                    completed_bytes += nbytes
                    done_total = prev_bytes + completed_bytes
                    elapsed_file = time.time() - file_t0
                    speed = _human(completed_bytes / elapsed_file) + "/s" if elapsed_file > 0.1 else "---"
                    pct_file = completed_bytes * 100 // fsize
                    pct_total = done_total * 100 // total_size if total_size else 100
                    bar = "#" * (pct_file // 5) + "-" * (20 - pct_file // 5)
                    print(f"\r  [{i}/{len(pairs)}] [{bar}] {pct_file:>3}% "
                          f"{_human(completed_bytes)}/{_human(fsize)} @ {speed} "
                          f"(total {pct_total}%)   ", end="", flush=True)
        # Commit in original order
        ordered = [BlobBlock(block_id=bid) for bid, _ in blocks]
        blob_client.commit_block_list(ordered)
        elapsed_file = time.time() - file_t0
        speed = _human(fsize / elapsed_file) + "/s" if elapsed_file > 0 else "?"
        print(f"\r  [{i}/{len(pairs)}] {p.name:<35} {_human(fsize):>10}  done in {elapsed_file:.1f}s  ({speed})")
        return fsize

    for i, (rel, p) in enumerate(pairs, 1):
        fsize = p.stat().st_size
        blob_name = f"{prefix}/{rel}".replace("\\", "/")
        blob_client = container_client.get_blob_client(blob_name)
        if fsize <= BLOCK_SIZE:
            _upload_small(blob_client, p, fsize)
            uploaded_bytes += fsize
            elapsed = time.time() - start
            speed = _human(uploaded_bytes / elapsed) + "/s" if elapsed > 0.1 else "---"
            pct = uploaded_bytes * 100 // total_size if total_size else 100
            print(f"  [{i}/{len(pairs)}] {rel:<35} {_human(fsize):>10}  done  (total {pct}% @ {speed})")
        else:
            uploaded_bytes += _upload_large_parallel(i, blob_client, p, fsize, uploaded_bytes)
    elapsed = time.time() - start
    print(f"  Upload done in {elapsed:.1f}s ({_human(total_size / elapsed if elapsed > 0 else 0)}/s)")
    _finalize_staged(sid, start)

# -- CLI commands -----------------------------------------------------------------

def cmd_list(_):
    models, reg = _fetch_models(detail=True)
    _show_models(models, reg)

def cmd_sync(_):
    r = requests.get(f"{BASE_URL}/models/sync", headers=_headers(), timeout=60)
    r.raise_for_status(); data = r.json()
    counts = data.get("counts", {})
    print(f"\nModel Sync Report  ({data.get('total',0)} models)")
    print(f"  Synced: {counts.get('synced',0)}  Blob-only: {counts.get('blob_only',0)}  Registry-only: {counts.get('registry_only',0)}")
    print()
    for m in data.get("models", []):
        status = m["status"]
        tag = {"synced": "OK", "blob_only": "BLOB", "registry_only": "REG"}[status]
        ver = m.get("latest_version", "")
        blob_vers = ",".join(m.get("blob_versions", []))
        print(f"  [{tag:>4}] {m['name']:<35} reg={f'v{ver}' if ver else '-':<6} blob=[{blob_vers}]")

def cmd_info(args):
    params = {"version": args.version} if args.version else {}
    r = requests.get(f"{BASE_URL}/models/{args.model_name}", params=params, headers=_headers(), timeout=30)
    r.raise_for_status(); print(json.dumps(r.json(), indent=2))

def cmd_delete(args):
    """Delete a model from registry and blob storage."""
    name = args.model_name
    ver = getattr(args, "version", None)
    skip = getattr(args, "yes", False)
    label = f"{name} v{ver}" if ver else f"{name} (all versions)"
    if not skip:
        confirm = input(f"  Delete '{label}'? This cannot be undone. [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Cancelled."); return
    print(f"  Deleting {label}...")
    params = {"version": ver} if ver else {}
    r = requests.delete(f"{BASE_URL}/models/{name}", params=params, headers=_headers(), timeout=60)
    r.raise_for_status()
    data = r.json()
    deleted = data.get("versions_deleted", [])
    print(f"  ✓ Deleted {len(deleted)} version(s): {', '.join(f'v{v}' for v in deleted)}")
    warnings = data.get("blob_warnings", [])
    if warnings:
        for w in warnings:
            print(f"  [WARN] Blob cleanup: {w}")

def cmd_status(_):
    """Show service status."""
    r = requests.get(f"{BASE_URL}/status", timeout=15)
    r.raise_for_status(); data = r.json()
    print(f"\n  {'─'*50}")
    print(f"  MDS Service Status")
    print(f"  {'─'*50}")
    print(f"  Status:      {data['status'].upper()}")
    print(f"  Uptime:      {data['uptime']}")
    print(f"  Started:     {data['started_at'][:19]}")
    print(f"  Registry:    {data['registry']}")
    print(f"  Storage:     {data['storage']}")
    print(f"  Models:      {data['model_count']}")
    print(f"  Uploads:     {data['stats']['uploads']}  (last: {data['stats']['last_upload'] or 'never'})")
    print(f"  Downloads:   {data['stats']['downloads']}  (last: {data['stats']['last_download'] or 'never'})")
    print(f"  {'─'*50}")

def cmd_dashboard(_):
    """Open the live dashboard in the default browser."""
    import webbrowser
    url = f"{BASE_URL}/dashboard"
    print(f"  Opening dashboard: {url}")
    webbrowser.open(url)

def cmd_catalog(args):
    r = requests.post(f"{BASE_URL}/catalog", json={"indexEntitiesRequest": {"pageSize": args.page_size or 50}},
                      headers=_headers(), timeout=60)
    r.raise_for_status(); resp = r.json().get("indexEntitiesResponse", {})
    for m in resp.get("value", []):
        name = m.get("annotations", {}).get("displayName", m.get("properties", {}).get("name", "?"))
        print(f"  {name:<40} task={m.get('properties',{}).get('task','?')}")

def cmd_catalog_fl(args):
    """List private MDS models via the Foundry Local catalog API (API-key auth)."""
    api_key = getattr(args, "api_key", None) or os.getenv("MDS_API_KEY", "")
    if not api_key:
        print("[ERROR] API key required.  Use --api-key KEY  or  set MDS_API_KEY env var."); sys.exit(1)
    device = getattr(args, "device", None)
    ep = getattr(args, "execution_provider", None)
    body = {"indexEntitiesRequest": {"pageSize": 100, "filters": []}}
    if device:
        body["indexEntitiesRequest"]["filters"].append(
            {"field": "properties/variantInfo/variantMetadata/device", "operator": "eq", "values": [device]})
    if ep:
        body["indexEntitiesRequest"]["filters"].append(
            {"field": "properties/variantInfo/variantMetadata/executionProvider", "operator": "eq", "values": [ep]})
    r = requests.post(f"{BASE_URL}/catalog/foundrylocal/{api_key}", json=body, timeout=60)
    r.raise_for_status()
    resp = r.json().get("indexEntitiesResponse", {})
    models = resp.get("value", [])
    print(f"\nPrivate Models  ({len(models)} found)")
    print(f"  {'NAME':<40} {'VER':>4}  {'DEVICE':<5}  {'EP':<28}  {'SIZE':>8}  URI")
    print(f"  {'-'*40} {'-'*4}  {'-'*5}  {'-'*28}  {'-'*8}  {'-'*50}")
    for m in models:
        props = m.get("properties", {})
        ann = m.get("annotations", {})
        vm = props.get("variantInfo", {}).get("variantMetadata", {})
        name = ann.get("name", props.get("name", "?"))
        ver = m.get("version", props.get("version", "?"))
        device_type = vm.get("device", "?")
        exec_prov = vm.get("executionProvider", "?")
        size_bytes = vm.get("fileSizeBytes", 0)
        size_str = _human(size_bytes) if size_bytes else "?"
        uri = m.get("uri", "")
        print(f"  {name:<40} {str(ver):>4}  {device_type:<5}  {exec_prov:<28}  {size_str:>8}  {uri}")

def cmd_download_fl(args):
    """Download a model from the Foundry Local public catalog via `foundry model download`."""
    import shutil, subprocess
    foundry = shutil.which("foundry") or shutil.which("foundry.exe")
    if not foundry:
        print("[ERROR] `foundry` CLI not found in PATH. Install from: https://learn.microsoft.com/azure/ai-foundry/foundry-local/get-started")
        sys.exit(1)
    model_name = args.model_name
    print(f"\n  Downloading '{model_name}' via Foundry Local CLI...")
    print(f"  (This downloads from the public Azure Foundry catalog)\n")
    cmd = [foundry, "model", "download", model_name]
    try:
        proc = subprocess.run(cmd, check=False)
        if proc.returncode != 0:
            print(f"\n  [ERROR] foundry model download exited with code {proc.returncode}")
            sys.exit(proc.returncode)
        print(f"\n  ✓ Model '{model_name}' is now cached locally.")
    except FileNotFoundError:
        print("[ERROR] Failed to run foundry CLI."); sys.exit(1)
    # Offer to run the model immediately
    run_now = getattr(args, '_run_after', None)
    if run_now is None:
        try:
            run_now = input("\n  Run this model now? [Y/n]: ").strip().lower() != "n"
        except (KeyboardInterrupt, EOFError):
            run_now = False
    if run_now:
        if not _fl_ensure_service():
            print("  Cannot start Foundry Local service. Run manually:")
            print(f"    foundry service start && mds run {model_name}")
            return
        cmd_run(argparse.Namespace(
            model_name=model_name, temperature=0.7, max_tokens=800,
            system=None, no_stream=False))

def cmd_list_fl(_):
    """List models from the Foundry Local public catalog via `foundry model list`."""
    import shutil, subprocess
    foundry = shutil.which("foundry") or shutil.which("foundry.exe")
    if not foundry:
        print("[ERROR] `foundry` CLI not found in PATH."); sys.exit(1)
    subprocess.run([foundry, "model", "list"], check=False)

def cmd_download(args):
    model, version, out_dir = args.model_name, args.version, Path(args.output or f"./{args.model_name}")
    method = getattr(args, 'method', None) or args.format or "urls"
    if not version:
        r = requests.get(f"{BASE_URL}/models/{model}/versions", headers=_headers(), timeout=30)
        r.raise_for_status(); versions = r.json().get("versions", [])
        if not versions: print(f"[ERROR] No versions for: {model}"); sys.exit(1)
        version = versions[0]; print(f"Using latest version: v{version}")
    print(f"Downloading {model} v{version} -> {out_dir}/")
    t0 = time.time()
    if method == "zip": _download_zip(model, version, out_dir)
    elif method == "sdk": _download_via_sdk(model, version, out_dir)
    else: _download_urls(model, version, out_dir)
    print(f"\nDone. Files saved to: {out_dir.resolve()}  ({time.time()-t0:.1f}s)")

def cmd_upload(args): _do_upload(args.model_name, args.path, args.task, args.device, args.description)
def cmd_upload_staged(args):
    if getattr(args, 'method', 'azcopy') == 'sdk':
        _do_staged_upload_sdk(args.model_name, args.path, args.task, args.device, args.description)
    else:
        _do_staged_upload(args.model_name, args.path, args.task, args.device, args.description, args.no_wait)

# -- Foundry Local service helpers ------------------------------------------------

def _fl_get(path, **kw):
    """GET request to the Foundry Local service."""
    r = requests.get(f"{FL_URL}{path}", timeout=kw.pop("timeout", 30), **kw)
    r.raise_for_status()
    return r

def _fl_post(path, **kw):
    """POST request to the Foundry Local service."""
    r = requests.post(f"{FL_URL}{path}", timeout=kw.pop("timeout", 300), **kw)
    r.raise_for_status()
    return r

def _fl_health():
    """Check if Foundry Local service is running."""
    try:
        r = requests.get(f"{FL_URL}/openai/models", timeout=5)
        return r.status_code == 200
    except Exception:
        return False

def _fl_ensure_service(silent=False):
    """Ensure Foundry Local service is running. Auto-starts it if not.
    Returns True if service is healthy, False if it cannot be started."""
    if _fl_health():
        return True
    # Try to auto-start
    import shutil, subprocess
    foundry = shutil.which("foundry") or shutil.which("foundry.exe")
    if not foundry:
        if not silent:
            print("  [ERROR] Foundry Local service is not running and `foundry` CLI not found.")
            print("  Install from: https://learn.microsoft.com/azure/ai-foundry/foundry-local/get-started")
        return False
    if not silent:
        print("  Foundry Local service not running. Starting...")
    try:
        subprocess.Popen(
            [foundry, "service", "start"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    except Exception as e:
        if not silent:
            print(f"  [ERROR] Failed to start service: {e}")
        return False
    # Wait for service to be ready (up to 30 seconds)
    for i in range(30):
        time.sleep(1)
        if _fl_health():
            if not silent:
                print(f"  ✓ Foundry Local service started ({i+1}s)")
            return True
        if not silent and i % 5 == 4:
            print(f"    Waiting... ({i+1}s)")
    if not silent:
        print("  [ERROR] Foundry Local service did not start within 30 seconds.")
    return False

def _fl_list_models():
    """List all models from FL service catalog (via /v1/models OpenAI-compatible endpoint)."""
    try:
        r = _fl_get("/v1/models")
        return r.json().get("data", [])
    except Exception:
        return []

def _fl_cached_models():
    """Get locally cached model IDs using `foundry cache list`."""
    import shutil, subprocess
    foundry = shutil.which("foundry") or shutil.which("foundry.exe")
    if not foundry:
        return set()
    try:
        proc = subprocess.run(
            [foundry, "cache", "list"], capture_output=True, text=True, timeout=15
        )
        # Parse the tabular output -- lines starting with 💾 have cached models
        cached = set()
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("\U0001f4be"):
                parts = line.split()
                if len(parts) >= 2:
                    # Last token is the model ID
                    cached.add(parts[-1])
        return cached
    except Exception:
        return set()

def _fl_load_model(model_id):
    """Load a model in FL service. Returns True on success."""
    try:
        r = _fl_get(f"/openai/load/{model_id}", timeout=120)
        return r.status_code == 200
    except Exception as e:
        print(f"  [ERROR] Load failed: {e}")
        return False

def _fl_unload_model(model_id):
    """Unload a model from FL service. Falls back to foundry CLI."""
    import shutil, subprocess
    # Try API first
    try:
        r = _fl_get(f"/openai/unload/{model_id}", timeout=30)
        if r.status_code == 200:
            return True
    except Exception:
        pass
    # Fallback: use foundry CLI
    foundry = shutil.which("foundry") or shutil.which("foundry.exe")
    if foundry:
        try:
            proc = subprocess.run(
                [foundry, "model", "unload", model_id],
                capture_output=True, text=True, timeout=30
            )
            if proc.returncode == 0:
                return True
            print(f"  [ERROR] foundry model unload: {proc.stderr.strip() or proc.stdout.strip()}")
        except Exception as e:
            print(f"  [ERROR] Unload failed: {e}")
    else:
        print(f"  [ERROR] Unload API returned error and `foundry` CLI not found.")
    return False

def _fl_chat_stream(model_id, messages, temperature=0.7, max_tokens=800):
    """Stream a chat completion from FL service. Yields content chunks."""
    body = {
        "model": model_id,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    r = requests.post(f"{FL_URL}/v1/chat/completions", json=body, stream=True, timeout=300)
    r.raise_for_status()
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload.strip() == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
            delta = chunk.get("choices", [{}])[0].get("delta", {})
            content = delta.get("content", "")
            if content:
                yield content
        except json.JSONDecodeError:
            pass

def _fl_chat(model_id, messages, temperature=0.7, max_tokens=800):
    """Non-streaming chat completion from FL service."""
    body = {
        "model": model_id,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    r = _fl_post("/v1/chat/completions", json=body, timeout=300)
    return r.json()


# -- FL CLI commands --------------------------------------------------------------

def cmd_cache(args):
    """Show models available in the Foundry Local cache."""
    if not _fl_ensure_service():
        sys.exit(1)
    models = _fl_list_models()           # /v1/models → list of dicts
    cached_ids = _fl_cached_models()     # foundry cache list → set of model ID strings
    print(f"\nFoundry Local Service: {FL_URL}")
    if models:
        cached = [m for m in models if m.get("id", "") in cached_ids]
        remote = [m for m in models if m.get("id", "") not in cached_ids]
        if cached:
            print(f"\n  Cached models ({len(cached)}) — ready to load & run:")
            for m in cached:
                mid = m.get("id", "?")
                max_in = m.get("maxInputTokens", "")
                max_out = m.get("maxOutputTokens", "")
                tool = "✓" if m.get("toolCalling") else " "
                print(f"    💾 {mid:<50} tokens={max_in}/{max_out}  tool={tool}")
        if remote:
            print(f"\n  Available remotely ({len(remote)}) — need download:")
            for m in remote[:10]:
                print(f"    ○ {m.get('id', '?')}")
            if len(remote) > 10:
                print(f"    ... and {len(remote)-10} more")
    else:
        print("  No models found. Is the service running?")
    if not cached_ids:
        print("\n  No cached models. Download one first:")
        print("    mds download-fl <model-name>")

def cmd_load(args):
    """Load a model in the Foundry Local service."""
    if not _fl_ensure_service():
        sys.exit(1)
    model_id = args.model_name
    print(f"  Loading '{model_id}'...")
    if _fl_load_model(model_id):
        print(f"  ✓ Model '{model_id}' loaded and ready.")
    else:
        sys.exit(1)

def cmd_unload(args):
    """Unload a model from the Foundry Local service."""
    if not _fl_ensure_service():
        sys.exit(1)
    model_id = args.model_name
    print(f"  Unloading '{model_id}'...")
    if _fl_unload_model(model_id):
        print(f"  ✓ Model '{model_id}' unloaded.")
    else:
        sys.exit(1)

def cmd_run(args):
    """Interactive chat with a Foundry Local model (REPL)."""
    if not _fl_ensure_service():
        sys.exit(1)

    model_id = args.model_name
    temperature = getattr(args, "temperature", 0.7) or 0.7
    max_tokens = getattr(args, "max_tokens", 800) or 800
    system_prompt = getattr(args, "system", None)
    no_stream = getattr(args, "no_stream", False)

    # Load the model (idempotent -- FL handles already-loaded case)
    print(f"  Loading '{model_id}'...")
    if not _fl_load_model(model_id):
        print(f"  [ERROR] Failed to load model. Is it downloaded?")
        print(f"  Try: mds download-fl {model_id}"); sys.exit(1)
    print(f"  ✓ Loaded.")

    print(f"\n{'='*60}")
    print(f"  Chat with: {model_id}")
    print(f"  Temperature: {temperature}  Max tokens: {max_tokens}")
    print(f"  Type 'exit' or 'quit' to end. Ctrl+C to abort.")
    print(f"{'='*60}\n")

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
        print(f"  [system] {system_prompt}\n")

    while True:
        try:
            user_input = input("You> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Goodbye!"); break
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "/exit", "/quit"):
            print("  Goodbye!"); break
        if user_input.lower() in ("/clear", "/reset"):
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            print("  [conversation cleared]\n"); continue
        if user_input.lower() == "/help":
            print("  Commands: /clear /reset /help /exit /quit")
            print(f"  Model: {model_id}  Temp: {temperature}  MaxTokens: {max_tokens}\n")
            continue

        messages.append({"role": "user", "content": user_input})
        print(f"\n{model_id}> ", end="", flush=True)

        try:
            if no_stream:
                resp = _fl_chat(model_id, messages, temperature, max_tokens)
                content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
                print(content)
                messages.append({"role": "assistant", "content": content})
            else:
                full_response = []
                for chunk in _fl_chat_stream(model_id, messages, temperature, max_tokens):
                    print(chunk, end="", flush=True)
                    full_response.append(chunk)
                print()
                messages.append({"role": "assistant", "content": "".join(full_response)})
        except requests.HTTPError as e:
            print(f"\n  [ERROR] {e.response.status_code}: {e.response.text[:200]}")
            messages.pop()  # remove failed user message
        except Exception as e:
            print(f"\n  [ERROR] {e}")
            messages.pop()
        print()

def cmd_chat(args):
    """One-shot chat with a Foundry Local model (non-interactive)."""
    if not _fl_ensure_service():
        sys.exit(1)

    model_id = args.model_name
    prompt = args.prompt
    temperature = getattr(args, "temperature", 0.7) or 0.7
    max_tokens = getattr(args, "max_tokens", 800) or 800
    system_prompt = getattr(args, "system", None)
    no_stream = getattr(args, "no_stream", False)

    # Load if needed (idempotent)
    print(f"  Loading '{model_id}'...", file=sys.stderr)
    if not _fl_load_model(model_id):
        print(f"  [ERROR] Failed to load model.", file=sys.stderr); sys.exit(1)

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    if no_stream:
        resp = _fl_chat(model_id, messages, temperature, max_tokens)
        content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
        print(content)
    else:
        for chunk in _fl_chat_stream(model_id, messages, temperature, max_tokens):
            print(chunk, end="", flush=True)
        print()


# -- Interactive helpers ----------------------------------------------------------

def _pick(title, options, allow_back=False):
    print(f"\n  {title}")
    if allow_back: print("    [0] Back")
    for i, o in enumerate(options, 1): print(f"    [{i}] {o}")
    while True:
        v = input("  > ").strip()
        if allow_back and v == "0": return 0, None
        if v.isdigit() and 1 <= int(v) <= len(options): return int(v), options[int(v)-1]
        lo = 0 if allow_back else 1
        print(f"  Enter {lo}-{len(options)}")

def _fetch_models(detail=False):
    try:
        params = {"detail": "true"} if detail else {}
        r = requests.get(f"{BASE_URL}/models", params=params, timeout=15); r.raise_for_status()
        data = r.json(); return data.get("models", []), data.get("registry", "?")
    except Exception as e:
        print(f"  [WARN] {e}"); return [], "?"

def _show_models(models, reg):
    print(f"\n  Registry: {reg}  ({len(models)} models)")
    if not models:
        print("    (no models)")
        return
    # Check if we have detail fields
    has_detail = any(m.get("task") for m in models)
    if has_detail:
        hdr = f"    {'#':>3}  {'NAME':<32} {'VER':>4}  {'TASK':<18} {'DEVICE':<6} {'SIZE':>9}  {'FL':>3}"
        sep = f"    {'─'*3}  {'─'*32} {'─'*4}  {'─'*18} {'─'*6} {'─'*9}  {'─'*3}"
        print(hdr)
        print(sep)
        for i, m in enumerate(models, 1):
            name = m['name'][:32]
            ver = f"v{m['latest_version']}"
            task = (m.get('task') or '—')[:18]
            device = (m.get('device') or '—')[:6]
            sb = m.get('size_bytes', 0)
            size = _human(sb) if sb else '—'
            fl = '✓' if m.get('fl_ready') else '—'
            print(f"    {i:>3}  {name:<32} {ver:>4}  {task:<18} {device:<6} {size:>9}  {fl:>3}")
    else:
        for i, m in enumerate(models, 1):
            print(f"    [{i}] {m['name']:<40} v{m['latest_version']}")

# -- Interactive mode -------------------------------------------------------------

def _interactive():
    global _TOKEN
    print("\n" + "=" * 60 + f"\n  MDS -- Model Distribution Service  v{__version__}\n" + "=" * 60)
    print(f"\n  Server: {BASE_URL}")
    if input("  Change URL? [y/N]: ").strip().lower() == "y":
        url = input("  URL: ").strip()
        if url: _set_base_url(url)

    # Step 1: Auth (POC -- we mint our own JWT, simulating the customer)
    print(f"\n{'- '*30}\n  Step 1: Authentication  (POC)\n{'- '*30}")
    if _TOKEN:
        print("  Token already loaded from MDS_TOKEN env var.")
    elif os.getenv("MDS_PRIVATE_KEY_PATH"):
        print(f"  Key loaded from env: {os.getenv('MDS_PRIVATE_KEY_PATH')}")
    else:
        # Auto-detect local keys
        local_keys = sorted(Path("keys").rglob("private.pem")) if Path("keys").exists() else []
        opts = []
        for k in local_keys:
            cust = k.parent.name
            opts.append(f"Use key: {k}  (customer: {cust})")
        opts += ["Generate new keypair", "Paste a bearer token", "Skip authentication"]
        idx, chosen = _pick("Authenticate", opts)
        if idx <= len(local_keys):
            key_path = str(local_keys[idx - 1])
            cust = local_keys[idx - 1].parent.name
            os.environ["MDS_PRIVATE_KEY_PATH"] = key_path
            os.environ.setdefault("MDS_CUSTOMER_ID", cust)
            print(f"  Loaded key: {key_path}  (customer: {cust})")
            _TOKEN = _get_token()
            print(f"  JWT minted. Token: {_TOKEN[:40]}...")
        elif chosen.startswith("Generate"):
            cust = input("  Customer ID [phonepe-india]: ").strip() or "phonepe-india"
            cust_dir = Path("keys") / cust.split("-")[0]; cust_dir.mkdir(parents=True, exist_ok=True)
            from cryptography.hazmat.primitives.asymmetric import rsa as rsa_mod
            from cryptography.hazmat.primitives import serialization
            priv = rsa_mod.generate_private_key(public_exponent=65537, key_size=2048)
            priv_path = cust_dir / "private.pem"
            priv_path.write_bytes(priv.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            pub_path = cust_dir / "public.pem"
            pub_path.write_bytes(priv.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
            os.environ["MDS_PRIVATE_KEY_PATH"] = str(priv_path)
            os.environ["MDS_CUSTOMER_ID"] = cust
            print(f"  Keypair saved: {cust_dir}/")
            _TOKEN = _get_token()
            print(f"  JWT minted. Token: {_TOKEN[:40]}...")
        elif chosen.startswith("Paste"):
            _TOKEN = input("  Token: ").strip()
        else:
            print("  Skipped -- browse models without auth.")

    # Step 2: Entitlement
    print(f"\n{'- '*30}\n  Step 2: Entitlement\n{'- '*30}")
    models, reg = _fetch_models(detail=True)
    _show_models(models, reg)

    # Step 3: Action loop
    while True:
        print(f"\n{'- '*30}\n  Step 3: Choose action\n{'- '*30}")
        idx, _ = _pick("Action", [
            "List models       (Private or Public catalog)",
            "Download a model  (Private or Public catalog)",
            "Run a model       (interactive chat)",
            "Upload a model    (Private Azure Storage)",
            "Delete a model    (remove from registry + storage)",
            "Model cache       (show cached/loaded models)",
            "View model details",
            "Service status    (health, uptime, stats)",
            "Sync report       (registry vs blob)",
            "Exit",
        ])
        try:
            if idx == 10: print("\n  Goodbye!"); break
            elif idx == 9: cmd_sync(None)
            elif idx == 8: cmd_status(None)
            elif idx == 7:
                if models:
                    idx_d, name = _pick("Model", [m["name"] for m in models], allow_back=True)
                    if idx_d == 0: continue
                else:
                    name = input("  Model name: ").strip()
                    if not name: continue
                cmd_info(argparse.Namespace(model_name=name, version=None))
            elif idx == 6: cmd_cache(argparse.Namespace())
            elif idx == 5:
                if not models: models, reg = _fetch_models(detail=True)
                if not models:
                    print("  No models to delete."); continue
                idx_d, name = _pick("Delete which model?", [m["name"] for m in models], allow_back=True)
                if idx_d == 0: continue
                cmd_delete(argparse.Namespace(model_name=name, version=None, yes=False))
                models, reg = _fetch_models(detail=True)  # refresh
            elif idx == 4: _interactive_upload()
            elif idx == 3:
                src, _ = _pick("Run from which source?", [
                    "Private Models  (downloaded to local folder)",
                    "Public Models   (cached models from public catalog)",
                ], allow_back=True)
                if src == 0: continue
                elif src == 1:
                    _interactive_run_mds(models)
                else:
                    _interactive_run()
            elif idx == 1:  # List
                src, _ = _pick("List from which source?", [
                    "Private Models ",
                    "Public Models ",
                ], allow_back=True)
                if src == 0: continue
                elif src == 1:
                    models, reg = _fetch_models(detail=True); _show_models(models, reg)
                else:
                    cmd_list_fl(None)
            elif idx == 2:  # Download
                src, _ = _pick("Download from which source?", [
                    "Private Models ",
                    "Public Models ",
                ], allow_back=True)
                if src == 0: continue
                elif src == 1:
                    if not models: models, reg = _fetch_models(detail=True); _show_models(models, reg)
                    _interactive_download(models)
                else:
                    _interactive_download_fl()
        except requests.HTTPError as e:
            print(f"  [ERROR] HTTP {e.response.status_code}: {e.response.text[:300]}")
        except Exception as e:
            print(f"  [ERROR] {e}")

def _interactive_download_fl():
    """Interactive flow for downloading a model from the Foundry Local public catalog."""
    import shutil, subprocess
    foundry = shutil.which("foundry") or shutil.which("foundry.exe")
    if not foundry:
        print("  [ERROR] `foundry` CLI not found. Install from: https://learn.microsoft.com/azure/ai-foundry/foundry-local/get-started")
        return
    # Show FL catalog first
    print("\n  Fetching Foundry Local catalog...")
    subprocess.run([foundry, "model", "list"], check=False)
    name = input("\n  Enter model name or alias to download: ").strip()
    if not name: print("  Cancelled."); return
    print(f"\n  Downloading '{name}' via Foundry Local CLI...")
    proc = subprocess.run([foundry, "model", "download", name], check=False)
    if proc.returncode != 0:
        print(f"\n  [ERROR] Download failed (exit code {proc.returncode})")
        return
    print(f"\n  ✓ Model '{name}' is now cached locally.")
    # Offer to run immediately
    try:
        run_now = input("\n  Run this model now? [Y/n]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return
    if run_now == "n": return
    if not _fl_ensure_service():
        print("  Cannot start service. Start manually: foundry service start")
        return
    _run_chat_session(name)

def _build_inference_model_json(model_dir, model_alias, registry_tags=None):
    """Build an inference_model.json for FL from the model's config files.

    FL requires this file to know the prompt template and model name.

    Resolution order for the prompt template:
      1. `promptTemplate` tag from the MDS registry (set during upload)
      2. Detection from tokenizer_config.json special tokens (im_start/im_end etc.)
      3. Detection from genai_config.json / config.json model type field
      4. Default: ChatML (most common format)
    """
    import json as _json
    model_dir = Path(model_dir)
    registry_tags = registry_tags or {}

    # --- Known prompt templates ---
    CHATML = {
        "system": "<|im_start|>system\n{Content}<|im_end|>",
        "user":   "<|im_start|>user\n{Content}<|im_end|>",
        "assistant": "<|im_start|>assistant\n{Content}<|im_end|>",
        "prompt": "<|im_start|>user\n{Content}<|im_end|>\n<|im_start|>assistant",
    }
    PHI = {
        "system": "<|system|>\n{Content}<|end|>",
        "user":   "<|user|>\n{Content}<|end|>",
        "assistant": "<|assistant|>\n{Content}<|end|>",
        "prompt": "<|user|>\n{Content}<|end|>\n<|assistant|>",
    }
    LLAMA = {
        "system": "<|start_header_id|>system<|end_header_id|>\n\n{Content}<|eot_id|>",
        "user":   "<|start_header_id|>user<|end_header_id|>\n\n{Content}<|eot_id|>",
        "assistant": "<|start_header_id|>assistant<|end_header_id|>\n\n{Content}<|eot_id|>",
        "prompt": "<|start_header_id|>user<|end_header_id|>\n\n{Content}<|eot_id|><|start_header_id|>assistant<|end_header_id|>",
    }

    template = None

    # --- 1. Use promptTemplate from MDS registry tags (most authoritative) ---
    pt_tag = registry_tags.get("promptTemplate", "")
    if pt_tag:
        try:
            template = _json.loads(pt_tag) if isinstance(pt_tag, str) else pt_tag
        except (_json.JSONDecodeError, TypeError):
            pass

    # --- 2. Detect from tokenizer_config.json special tokens ---
    if not template:
        tok_path = model_dir / "tokenizer_config.json"
        if tok_path.exists():
            try:
                tok = _json.loads(tok_path.read_text(encoding="utf-8"))
                added = tok.get("added_tokens_decoder", {})
                token_contents = {v.get("content", "") for v in added.values() if isinstance(v, dict)}
                if "<|im_start|>" in token_contents and "<|im_end|>" in token_contents:
                    template = CHATML
                elif "<|system|>" in token_contents and "<|end|>" in token_contents:
                    template = PHI
                elif "<|start_header_id|>" in token_contents and "<|eot_id|>" in token_contents:
                    template = LLAMA
            except Exception:
                pass

    # --- 3. Detect from genai_config.json / config.json model type ---
    if not template:
        model_type = None
        genai_path = model_dir / "genai_config.json"
        config_path = model_dir / "config.json"
        if genai_path.exists():
            try:
                gc = _json.loads(genai_path.read_text(encoding="utf-8"))
                model_type = gc.get("model", {}).get("type", "").lower()
            except Exception:
                pass
        if not model_type and config_path.exists():
            try:
                cc = _json.loads(config_path.read_text(encoding="utf-8"))
                model_type = cc.get("model_type", "").lower()
            except Exception:
                pass

        template_map = {
            "qwen": CHATML, "qwen2": CHATML, "qwen3": CHATML,
            "phi": PHI, "phi3": PHI, "phi4": PHI,
            "llama": LLAMA, "llama2": LLAMA, "llama3": LLAMA,
            "mistral": CHATML, "gemma": CHATML,
        }
        if model_type:
            for key, tmpl in template_map.items():
                if key in model_type:
                    template = tmpl; break

    # --- 4. Default: ChatML ---
    if not template:
        template = CHATML

    return {
        "Name": model_alias,
        "PromptTemplate": template,
    }


def _fetch_model_tags(model_name):
    """Fetch FL tags for a model from the MDS registry. Returns dict or {}."""
    try:
        r = requests.get(f"{BASE_URL}/models/{model_name}", headers=_headers(), timeout=15)
        if r.status_code == 200:
            data = r.json()
            # Tags may be at top level or under 'tags' key depending on API
            return data.get("tags", data.get("properties", {}).get("tags", {}))
    except Exception:
        pass
    return {}


def _sideload_to_fl_cache(model_name, model_dir, registry_tags=None):
    """Sideload a local model folder into the FL cache so `foundry model load` can find it.

    Creates a symlink (or copies files) into:
      ~/.foundry/cache/models/MDS/{model_name}/v1/

    Returns the alias string like 'mds-{model_name}:1' or None on failure.
    """
    import json as _json, shutil
    model_dir = Path(model_dir).resolve()
    alias = f"mds-{model_name}:1"

    # Find FL cache root
    cache_root = Path.home() / ".foundry" / "cache" / "models" / "MDS" / model_name / "v1"
    cache_root.mkdir(parents=True, exist_ok=True)

    # Check if already sideloaded with same files
    marker = cache_root / ".mds_sideloaded"
    if marker.exists():
        try:
            meta = _json.loads(marker.read_text(encoding="utf-8"))
            if meta.get("source") == str(model_dir):
                print(f"  Already sideloaded: {alias}")
                return alias
        except Exception:
            pass

    print(f"  Sideloading '{model_name}' into FL cache...")
    print(f"    Source: {model_dir}")
    print(f"    Cache:  {cache_root}")

    # Copy model files (skip huge .onnx.data -- symlink it instead)
    for src_file in model_dir.iterdir():
        if not src_file.is_file():
            continue
        dst_file = cache_root / src_file.name
        if dst_file.exists():
            # Skip if same size (already copied)
            if dst_file.stat().st_size == src_file.stat().st_size:
                continue
            dst_file.unlink()

        if src_file.stat().st_size > 100 * 1024 * 1024:  # >100MB: symlink
            try:
                dst_file.symlink_to(src_file)
                print(f"    ↗ {src_file.name}  (symlink)")
            except OSError:
                # Symlink failed (no privileges) -- copy instead
                print(f"    ⟳ {src_file.name}  ({_human(src_file.stat().st_size)}, copying...)")
                shutil.copy2(src_file, dst_file)
        else:
            shutil.copy2(src_file, dst_file)
            print(f"    ✓ {src_file.name}")

    # Generate inference_model.json if missing
    inf_path = cache_root / "inference_model.json"
    if not inf_path.exists():
        inf_model = _build_inference_model_json(model_dir, alias, registry_tags)
        inf_path.write_text(_json.dumps(inf_model, indent=2), encoding="utf-8")
        print(f"    ✓ inference_model.json  (generated)")

    # Write sideload marker
    marker.write_text(_json.dumps({
        "source": str(model_dir),
        "alias": alias,
        "model_name": model_name,
    }, indent=2), encoding="utf-8")

    print(f"  Sideloaded as: {alias}")
    return alias


def _interactive_run_mds(models):
    """Interactive flow for running a model downloaded from MDS via Foundry Local.

    Algorithm (mirrors FL SDK):
      1. Pick an MDS model from the registry
      2. Verify the model has been downloaded to a local folder
      3. Sideload into FL cache (~/.foundry/cache/models/MDS/{name}/v1/)
         - Copy/symlink model files
         - Generate inference_model.json with correct prompt template
      4. Load via `foundry model load {alias}`
      5. Start interactive chat via FL REST API (/v1/chat/completions)
    """
    if not models:
        print("\n  No MDS models loaded. Listing from server...")
        models, _ = _fetch_models()
    if not models:
        print("  [ERROR] No models available."); return

    idx, name = _pick("Select MDS model to run", [m["name"] for m in models], allow_back=True)
    if idx == 0: return

    # The model must have been downloaded locally -- check for a local folder
    local_dir = Path(name)
    if not local_dir.is_dir():
        print(f"\n  [WARN] Local folder '{name}/' not found.")
        print(f"  Download it first:  Action > Download > MDS > {name}")
        return

    # Verify it has ONNX model files
    onnx_files = list(local_dir.glob("*.onnx")) + list(local_dir.glob("*.onnx.data"))
    if not onnx_files:
        print(f"\n  [WARN] No .onnx files found in '{name}/'. Not a runnable model.")
        print(f"  Only ONNX GenAI models can be loaded into Foundry Local.")
        return

    # Ensure FL service is running
    if not _fl_ensure_service():
        print("  Cannot start FL service. Start manually: foundry service start"); return

    # Fetch FL tags from MDS registry (promptTemplate, task, etc.)
    print(f"  Fetching FL tags for '{name}' from registry...")
    tags = _fetch_model_tags(name)
    if tags:
        pt = tags.get("promptTemplate", "")
        task = tags.get("task", "")
        print(f"    task={task or '(none)'}  promptTemplate={'yes' if pt else '(auto-detect)'}")
    else:
        print(f"    No registry tags found, will auto-detect from model files.")

    # Sideload into FL cache
    alias = _sideload_to_fl_cache(name, local_dir, registry_tags=tags)
    if not alias:
        print("  [ERROR] Sideload failed."); return

    # Load via foundry CLI
    import shutil as _shutil, subprocess as _sp
    foundry = _shutil.which("foundry") or _shutil.which("foundry.exe")
    if foundry:
        print(f"\n  Loading '{alias}' into FL service...")
        proc = _sp.run([foundry, "model", "load", alias],
                       capture_output=True, encoding="utf-8", errors="replace")
        if proc.returncode == 0:
            print(f"  ✓ Model loaded: {alias}")
        else:
            err_msg = (proc.stderr or proc.stdout or "").strip()
            print(f"  [WARN] foundry load returned code {proc.returncode}: {err_msg}")
            print(f"  Trying API load...")
            _fl_load_model(alias)
    else:
        print("  `foundry` CLI not found, trying API load...")
        _fl_load_model(alias)

    # Determine the actual model ID (FL may adjust the name)
    model_id = alias
    loaded = _fl_list_models()
    if loaded:
        for m in loaded:
            mid = m.get("id", "")
            if name.lower() in mid.lower() or alias.lower() == mid.lower():
                model_id = mid; break

    _run_chat_session(model_id)

def _interactive_run():
    """Interactive flow for running a model via Foundry Local."""
    if not _fl_ensure_service():
        return

    # Get cached models (ready to run)
    cached_ids = _fl_cached_models()
    if not cached_ids:
        # Fallback: get all model IDs from the service
        fl_models = _fl_list_models()
        cached_ids = {m.get("id", "") for m in fl_models} if fl_models else set()

    if not cached_ids:
        print("\n  No models available. Download one first:")
        print("    mds download-fl <model-name>")
        return

    available = sorted(cached_ids)
    idx, chosen = _pick("Select model to chat with", available, allow_back=True)
    if idx == 0: return
    model_id = chosen.strip()

    _run_chat_session(model_id)

def _run_chat_session(model_id):
    """Prompt for settings and start a chat REPL with the given model."""
    # Optional settings with explanations
    print("\n  Chat settings (press Enter to use defaults):")
    print("  ─────────────────────────────────────────────")
    print("  System prompt : Sets the model's persona/behavior before the chat.")
    print("                  e.g. 'You are a helpful coding assistant'")
    system = input("  System prompt (Enter to skip): ").strip() or None
    print("  Temperature   : Controls randomness. 0.0 = deterministic, 0.7 = balanced,")
    print("                  1.0+ = creative. Keep between 0.1–1.0 for best results.")
    temp_str = input("  Temperature [0.7]: ").strip()
    temperature = float(temp_str) if temp_str else 0.7
    print("  Max tokens    : Maximum response length (~1 token ≈ ¾ word).")
    print("                  200 = short, 800 = medium, 4096 = long.")
    max_str = input("  Max tokens [800]: ").strip()
    max_tokens = int(max_str) if max_str else 800

    # Start the chat REPL
    cmd_run(argparse.Namespace(
        model_name=model_id, temperature=temperature, max_tokens=max_tokens,
        system=system, no_stream=False))

def _interactive_download(models):
    if models:
        idx, name = _pick("Download which model?", [m["name"] for m in models], allow_back=True)
        if idx == 0: return
    else:
        name = input("  Model name: ").strip()
        if not name: return
    try:
        r = requests.get(f"{BASE_URL}/models/{name}/versions", headers=_headers(), timeout=30)
        r.raise_for_status(); versions = r.json().get("versions", [])
    except Exception: versions = []
    if versions:
        labels = [f"v{v} (latest)" if i == 0 else f"v{v}" for i, v in enumerate(versions)]
        idx_v, chosen = _pick("Version", labels, allow_back=True)
        if idx_v == 0: return
        ver = chosen.split()[0].lstrip("v")
    else:
        ver = input("  Version (blank=latest): ").strip() or None
    idx_m, method = _pick("Download method", [
        "SAS URL download (direct from Azure Storage -- fast)",
        "Show SAS URLs only (copy/paste into browser)",
        "ZIP archive (server packages all files)",
        "Parallel download (Azure SDK -- fastest for large models)",
    ], allow_back=True)
    if idx_m == 0: return
    if "Show SAS" in method:
        print("  Fetching SAS URLs...")
        r = requests.post(f"{BASE_URL}/download", params={"model": name, "version": ver},
                          headers=_headers(), timeout=60)
        r.raise_for_status(); data = r.json()
        if "download_url" in data and "files" not in data:
            print(f"\n  {name} v{ver} (single file):")
            print(f"  {data['download_url']}\n")
        else:
            files = data.get("files", [])
            if not files:
                print("  [WARN] No files returned.")
            else:
                print(f"\n  {name} v{ver} ({len(files)} file(s)):\n")
                for entry in files:
                    print(f"  {entry['file']}:")
                    print(f"    {entry['download_url']}\n")
        return
    default = f"./{name}"
    raw = input(f"  Output dir [{default}]: ").strip().strip('"')
    out = Path(raw) if raw else Path(default)
    print(f"\n  Downloading {name} v{ver} -> {out}/")
    t0 = time.time()
    if "ZIP" in method:
        _download_zip(name, ver, out)
    elif "Parallel" in method:
        _download_via_sdk(name, ver, out)
    else:
        _download_urls(name, ver, out)
    elapsed = time.time() - t0
    print(f"  Done! Saved to: {out.resolve()}  ({elapsed:.1f}s total)")
    # Offer to run the downloaded model
    _offer_run_after(name, str(out.resolve()))

def _offer_run_after(model_name, local_path=None):
    """Ask the user if they want to run the model after upload/download."""
    try:
        run_now = input("\n  Run this model now? [Y/n]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return
    if run_now == "n": return
    src, _ = _pick("Run from which source?", [
        "Private Models  (use local downloaded folder)",
        "Public Models   (cached models from public catalog)",
    ], allow_back=True)
    if src == 0: return
    if src == 1:
        if not local_path or not Path(local_path).is_dir():
            print(f"  [WARN] Local folder not found at '{local_path}'.")
            return
        # Check for ONNX files
        onnx_files = list(Path(local_path).glob("*.onnx"))
        if not onnx_files:
            print(f"  [WARN] No .onnx files in '{local_path}'. Not a runnable model.")
            return
        if not _fl_ensure_service():
            print("  Cannot start FL service."); return
        alias = _sideload_to_fl_cache(model_name, local_path)
        if not alias:
            print("  [ERROR] Sideload failed."); return
        import shutil as _sh, subprocess as _sp
        foundry = _sh.which("foundry") or _sh.which("foundry.exe")
        if foundry:
            print(f"  Loading '{alias}'...")
            _sp.run([foundry, "model", "load", alias],
                    capture_output=True, encoding="utf-8", errors="replace")
        _run_chat_session(alias)
    else:
        if not _fl_ensure_service():
            print("  Cannot start FL service."); return
        cached = sorted(_fl_cached_models())
        if not cached:
            print("  No FL cached models available."); return
        idx, chosen = _pick("Select model to chat with", cached, allow_back=True)
        if idx == 0: return
        _run_chat_session(chosen.strip())

def _interactive_upload():
    name = input("  Model name: ").strip()
    if not name: return
    idx_s, _ = _pick("Source", ["Single file", "Multiple files", "Folder"], allow_back=True)
    if idx_s == 0: return
    if idx_s == 1:
        path = input("  File path: ").strip().strip('"')
    elif idx_s == 2:
        print("  Enter paths (empty line to finish):")
        paths = []
        while True:
            line = input("    > ").strip().strip('"')
            if not line and paths: break
            elif line: paths.append(line)
        path = paths  # list
    else:
        path = input("  Folder path: ").strip().strip('"')
    # Calculate total size for recommendation
    if isinstance(path, list):
        pairs = []; [pairs.extend(_collect_files(p)) for p in path]
    else:
        pairs = _collect_files(path)
    total_size = sum(p.stat().st_size for _, p in pairs)
    SIZE_THRESHOLD = 50 * 1024 * 1024  # 50 MB
    methods = ["Direct upload (HTTP POST)", "Staged upload (azcopy)", "Staged upload (Azure SDK)"]
    if total_size > SIZE_THRESHOLD:
        print(f"  Total size: {_human(total_size)} -- staged upload recommended for speed")
        default_hint = " [recommended]"
        methods[1] += default_hint; methods[2] += default_hint
    idx_m, _ = _pick("Method", methods, allow_back=True)
    if idx_m == 0: return
    print("  Optional metadata (Enter to skip):")
    task = input("    Task: ").strip() or None
    device = input("    Device: ").strip() or None
    desc = input("    Description: ").strip() or None
    src = path if isinstance(path, str) else path[0]
    if idx_m == 2:
        _do_staged_upload(name, src, task, device, desc)
    elif idx_m == 3:
        _do_staged_upload_sdk(name, src, task, device, desc)
    elif isinstance(path, list):
        for p in path: _do_upload(name, p, task, device, desc)
    else:
        _do_upload(name, path, task, device, desc)
    # Offer to run the just-uploaded model
    local = src if isinstance(path, str) else (path[0] if isinstance(path, list) else str(path))
    _offer_run_after(name, local)

# -- CLI entry point --------------------------------------------------------------

def _set_fl_url(url):
    global FL_URL
    FL_URL = url

def main():
    p = argparse.ArgumentParser(prog="mds", description="MDS SDK Client",
                                epilog="Run without arguments for interactive demo mode.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--base-url", default=BASE_URL, help="MDS server URL")
    p.add_argument("--fl-url", default=FL_URL, help="Foundry Local service URL")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("list", help="List models")
    sub.add_parser("sync", help="Sync report: registry vs blob")
    ip = sub.add_parser("info", help="Model detail"); ip.add_argument("model_name"); ip.add_argument("--version", "-v", default=None)
    cp = sub.add_parser("catalog", help="Foundry Local catalog"); cp.add_argument("--page-size", type=int, default=50)
    fp = sub.add_parser("catalog-fl", help="List private MDS models (API-key auth)")
    fp.add_argument("--api-key", default=None, help="MDS API key (or set MDS_API_KEY env)")
    fp.add_argument("--device", choices=["cpu", "gpu"], default=None, help="Filter by device type")
    fp.add_argument("--execution-provider", default=None, help="Filter by execution provider")
    sub.add_parser("list-fl", help="List Foundry Local public catalog models")
    fl_dl = sub.add_parser("download-fl", help="Download from Foundry Local public catalog")
    fl_dl.add_argument("model_name", help="Model name or alias (e.g. phi-4-mini)")
    # FL run/chat/cache/load/unload commands
    rp = sub.add_parser("run", help="Interactive chat with a Foundry Local model")
    rp.add_argument("model_name", help="Model name or ID")
    rp.add_argument("--temperature", "-t", type=float, default=0.7, help="Sampling temperature")
    rp.add_argument("--max-tokens", type=int, default=800, help="Max response tokens")
    rp.add_argument("--system", "-s", default=None, help="System prompt")
    rp.add_argument("--no-stream", action="store_true", help="Disable streaming")
    chp = sub.add_parser("chat", help="One-shot chat with a Foundry Local model")
    chp.add_argument("model_name", help="Model name or ID")
    chp.add_argument("prompt", help="User message")
    chp.add_argument("--temperature", "-t", type=float, default=0.7)
    chp.add_argument("--max-tokens", type=int, default=800)
    chp.add_argument("--system", "-s", default=None, help="System prompt")
    chp.add_argument("--no-stream", action="store_true")
    sub.add_parser("cache", help="Show cached/loaded Foundry Local models")
    lp = sub.add_parser("load", help="Load a model in Foundry Local")
    lp.add_argument("model_name", help="Model name or ID")
    ulp = sub.add_parser("unload", help="Unload a model from Foundry Local")
    ulp.add_argument("model_name", help="Model name or ID")
    dp = sub.add_parser("download", help="Download model"); dp.add_argument("model_name")
    dp.add_argument("--version", "-v", default=None); dp.add_argument("--output", "-o", default=None)
    dp.add_argument("--format", "-f", choices=["urls", "zip"], default="urls")
    dp.add_argument("--method", "-m", choices=["sas", "zip", "sdk"], default=None, help="Download method: sas (default), zip, or sdk")
    up = sub.add_parser("upload", help="Upload file/folder"); up.add_argument("model_name"); up.add_argument("path")
    up.add_argument("--task", default=None); up.add_argument("--device", default=None); up.add_argument("--description", default=None)
    sp = sub.add_parser("upload-staged", help="Staged upload"); sp.add_argument("model_name"); sp.add_argument("path")
    sp.add_argument("--task", default=None); sp.add_argument("--device", default=None); sp.add_argument("--description", default=None)
    sp.add_argument("--no-wait", action="store_true")
    sp.add_argument("--method", choices=["azcopy", "sdk"], default="azcopy", help="Upload method: azcopy (default) or sdk")
    # Delete command
    delp = sub.add_parser("delete", help="Delete a model from registry and storage")
    delp.add_argument("model_name", help="Model name to delete")
    delp.add_argument("--version", "-v", default=None, help="Delete specific version (default: all versions)")
    delp.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    # Status/dashboard command
    sub.add_parser("status", help="Show service status")
    sub.add_parser("dashboard", help="Open live dashboard in browser")
    args = p.parse_args()
    if args.base_url != BASE_URL: _set_base_url(args.base_url)
    if args.fl_url != FL_URL: _set_fl_url(args.fl_url)
    if not args.command: _interactive(); return
    cmds = {"list": cmd_list, "sync": cmd_sync, "info": cmd_info, "catalog": cmd_catalog,
            "catalog-fl": cmd_catalog_fl, "list-fl": cmd_list_fl, "download-fl": cmd_download_fl,
            "download": cmd_download, "upload": cmd_upload, "upload-staged": cmd_upload_staged,
            "run": cmd_run, "chat": cmd_chat, "cache": cmd_cache,
            "load": cmd_load, "unload": cmd_unload,
            "delete": cmd_delete, "status": cmd_status, "dashboard": cmd_dashboard}
    try: cmds[args.command](args)
    except requests.HTTPError as e: print(f"\n[ERROR] HTTP {e.response.status_code}: {e.response.text[:300]}"); sys.exit(1)
    except KeyboardInterrupt: print("\nCancelled."); sys.exit(130)

if __name__ == "__main__":
    main()
