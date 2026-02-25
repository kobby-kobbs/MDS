"""
MDS SDK Client -- Interactive & CLI modes.
Interactive (demo): python mds_cli.py        CLI: python mds_cli.py list|download|upload|...
Env: MDS_BASE_URL, MDS_TOKEN, MDS_PRIVATE_KEY_PATH
"""
import argparse, io, json, os, sys, time, zipfile
from pathlib import Path, PurePosixPath
import requests

BASE_URL = os.getenv("MDS_BASE_URL", "https://mds-model-distribution.azurewebsites.net")
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
    r = requests.get(f"{BASE_URL}/models", timeout=30); r.raise_for_status(); data = r.json()
    models = data.get("models", [])
    print(f"\nRegistry: {data.get('registry','?')}  ({len(models)} models)")
    for m in models: print(f"  {m['name']:<40} v{m['latest_version']}")

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
    print(f"\nMDS Private Models  ({len(models)} found)")
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
        print(f"\n  Done. Model '{model_name}' is now cached locally.")
        print(f"  Run: foundry model run {model_name}")
    except FileNotFoundError:
        print("[ERROR] Failed to run foundry CLI."); sys.exit(1)

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

# -- Interactive helpers ----------------------------------------------------------

def _pick(title, options):
    print(f"\n  {title}")
    for i, o in enumerate(options, 1): print(f"    [{i}] {o}")
    while True:
        v = input("  > ").strip()
        if v.isdigit() and 1 <= int(v) <= len(options): return int(v), options[int(v)-1]
        print(f"  Enter 1-{len(options)}")

def _fetch_models():
    try:
        r = requests.get(f"{BASE_URL}/models", timeout=15); r.raise_for_status()
        data = r.json(); return data.get("models", []), data.get("registry", "?")
    except Exception as e:
        print(f"  [WARN] {e}"); return [], "?"

def _show_models(models, reg):
    print(f"\n  Registry: {reg}  ({len(models)} models)")
    for i, m in enumerate(models, 1): print(f"    [{i}] {m['name']:<40} v{m['latest_version']}")

# -- Interactive mode -------------------------------------------------------------

def _interactive():
    global _TOKEN
    print("\n" + "=" * 60 + "\n  MDS -- Model Distribution Service\n" + "=" * 60)
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
    models, reg = _fetch_models()
    _show_models(models, reg)

    # Step 3: Action loop
    while True:
        print(f"\n{'- '*30}\n  Step 3: Choose action\n{'- '*30}")
        idx, _ = _pick("Action", [
            "List models       (choose source: MDS or Foundry Local)",
            "Download a model  (choose source: MDS or Foundry Local)",
            "Upload a model    (MDS blob storage)",
            "View model details (MDS)",
            "Sync report       (MDS registry vs blob)",
            "Exit",
        ])
        try:
            if idx == 6: print("\n  Goodbye!"); break
            elif idx == 5: cmd_sync(None)
            elif idx == 4:
                _, name = _pick("Model", [m["name"] for m in models]) if models else (0, input("  Model name: ").strip())
                cmd_info(argparse.Namespace(model_name=name, version=None))
            elif idx == 3: _interactive_upload()
            elif idx == 1:  # List
                src, _ = _pick("List from which source?", [
                    "MDS Private Models  (your blob storage, requires JWT)",
                    "Foundry Local       (public Microsoft catalog, 107+ models)",
                ])
                if src == 1:
                    models, reg = _fetch_models(); _show_models(models, reg)
                else:
                    cmd_list_fl(None)
            elif idx == 2:  # Download
                src, _ = _pick("Download from which source?", [
                    "MDS Private Models  (your blob storage, requires JWT)",
                    "Foundry Local       (public Microsoft catalog, uses foundry CLI)",
                ])
                if src == 1:
                    if not models: models, reg = _fetch_models(); _show_models(models, reg)
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
    if proc.returncode == 0:
        print(f"\n  Done! Run:  foundry model run {name}")
    else:
        print(f"\n  [ERROR] Download failed (exit code {proc.returncode})")

def _interactive_download(models):
    _, name = _pick("Download which model?", [m["name"] for m in models]) if models else (0, input("  Model name: ").strip())
    try:
        r = requests.get(f"{BASE_URL}/models/{name}/versions", headers=_headers(), timeout=30)
        r.raise_for_status(); versions = r.json().get("versions", [])
    except Exception: versions = []
    if versions:
        labels = [f"v{v} (latest)" if i == 0 else f"v{v}" for i, v in enumerate(versions)]
        _, chosen = _pick("Version", labels); ver = chosen.split()[0].lstrip("v")
    else:
        ver = input("  Version (blank=latest): ").strip() or None
    _, method = _pick("Download method", [
        "SAS URL (direct from Azure Storage -- fast)",
        "ZIP archive (server packages all files)",
        "Parallel download (Azure SDK -- fastest for large models)",
    ])
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

def _interactive_upload():
    name = input("  Model name: ").strip()
    if not name: return
    idx_s, _ = _pick("Source", ["Single file", "Multiple files", "Folder"])
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
    idx_m, _ = _pick("Method", methods)
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

# -- CLI entry point --------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(prog="mds", description="MDS SDK Client",
                                epilog="Run without arguments for interactive demo mode.")
    p.add_argument("--base-url", default=BASE_URL, help="MDS server URL")
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
    args = p.parse_args()
    if args.base_url != BASE_URL: _set_base_url(args.base_url)
    if not args.command: _interactive(); return
    cmds = {"list": cmd_list, "sync": cmd_sync, "info": cmd_info, "catalog": cmd_catalog,
            "catalog-fl": cmd_catalog_fl, "list-fl": cmd_list_fl, "download-fl": cmd_download_fl,
            "download": cmd_download, "upload": cmd_upload, "upload-staged": cmd_upload_staged}
    try: cmds[args.command](args)
    except requests.HTTPError as e: print(f"\n[ERROR] HTTP {e.response.status_code}: {e.response.text[:300]}"); sys.exit(1)
    except KeyboardInterrupt: print("\nCancelled."); sys.exit(130)

if __name__ == "__main__":
    main()
