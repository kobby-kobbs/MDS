#!/usr/bin/env python3
"""
MDS Customer Setup Script
=========================
Customers run this to connect to MDS, browse entitled models, and download
a baseline set into a local directory.

Prerequisites:
  pip install requests

Usage:
  # Interactive -- prompts for everything
  python customer_setup.py

  # Provide token directly
  python customer_setup.py --token <JWT>

  # Download all entitled models at once
  python customer_setup.py --token <JWT> --download-all

  # Download specific models
  python customer_setup.py --token <JWT> --models mnist,squeezenet

  # Custom server / output directory
  python customer_setup.py --server https://my-mds.azurewebsites.net --output ./my_models
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: 'requests' package required.  Install with:  pip install requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_SERVER = "https://mds-model-distribution.azurewebsites.net"
DEFAULT_OUTPUT = "./mds_models"
CONFIG_FILE = ".mds_config.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _human(n):
    """Human-readable byte size."""
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def _headers(token):
    return {"Authorization": f"Bearer {token}"} if token else {}


def _download_file(url, target, idx, total):
    """Stream-download a single file with progress bar."""
    r = requests.get(url, stream=True, timeout=600)
    r.raise_for_status()
    size = int(r.headers.get("content-length", 0))
    downloaded, t0 = 0, time.time()
    tag = f"[{idx}/{total}]"
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as f:
        for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
            f.write(chunk)
            downloaded += len(chunk)
            elapsed = time.time() - t0
            speed = _human(downloaded / elapsed) + "/s" if elapsed > 0.1 else "---"
            if size > 0:
                pct = downloaded * 100 // size
                bar = "#" * (pct // 5) + "-" * (20 - pct // 5)
                print(f"\r    {tag} [{bar}] {pct:>3}%  {target.name}  "
                      f"{_human(downloaded)}/{_human(size)}  @ {speed}   ",
                      end="", flush=True)
            else:
                print(f"\r    {tag} {target.name}  {_human(downloaded)}  @ {speed}   ",
                      end="", flush=True)
    elapsed = time.time() - t0
    print(f"\r    {tag} {target.name:<40} {_human(downloaded):>10}  "
          f"{elapsed:.1f}s  ({_human(downloaded / elapsed) if elapsed > 0 else '?'}/s)")


# ---------------------------------------------------------------------------
# Core actions
# ---------------------------------------------------------------------------

def check_health(server):
    """Verify server is reachable."""
    try:
        r = requests.get(f"{server}/health", timeout=15)
        r.raise_for_status()
        data = r.json()
        print(f"  Server OK  (registry: {data.get('registry','?')}, "
              f"storage: {data.get('storage','?')})")
        return True
    except Exception as e:
        print(f"  Server UNREACHABLE: {e}")
        return False


def list_entitled_models(server, token):
    """Fetch the models this customer is entitled to."""
    r = requests.get(f"{server}/models", headers=_headers(token), timeout=30)
    r.raise_for_status()
    data = r.json()
    models = data.get("models", [])
    return models


def get_model_info(server, token, name):
    """Get detailed info for a single model."""
    try:
        r = requests.get(f"{server}/models/{name}",
                         headers=_headers(token), timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def download_model(server, token, name, version, out_dir):
    """Download a model (all files) to out_dir/<name>/."""
    model_dir = out_dir / name
    print(f"\n  Downloading: {name} v{version} -> {model_dir}/")
    r = requests.post(f"{server}/download",
                      params={"model": name, "version": version},
                      headers=_headers(token), timeout=120)
    r.raise_for_status()
    data = r.json()

    # Single-file model
    if "download_url" in data and "files" not in data:
        url = data["download_url"]
        fname = url.split("?")[0].split("/")[-1] or f"{name}.onnx"
        model_dir.mkdir(parents=True, exist_ok=True)
        _download_file(url, model_dir / fname, 1, 1)
        return [model_dir / fname]

    # Multi-file model
    files = data.get("files", [])
    if not files:
        print("    No files returned.")
        return []

    model_dir.mkdir(parents=True, exist_ok=True)
    downloaded_paths = []
    for idx, entry in enumerate(files, 1):
        blob = entry["file"]
        parts = blob.split("/")
        # Strip model_name/vN prefix to get relative path
        if len(parts) > 2 and parts[1].startswith("v") and parts[1][1:].isdigit():
            rel = "/".join(parts[2:])
        else:
            rel = parts[-1]
        target = model_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        _download_file(entry["download_url"], target, idx, len(files))
        downloaded_paths.append(target)

    return downloaded_paths


def save_config(server, token, out_dir):
    """Save config for future CLI/SDK use."""
    cfg = {
        "server": server,
        "output_dir": str(out_dir),
        "configured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    # Don't save the token itself (security), just note that auth is required
    cfg["auth"] = "bearer_token"
    cfg_path = out_dir / CONFIG_FILE
    cfg_path.write_text(json.dumps(cfg, indent=2))
    print(f"  Config saved: {cfg_path}")


# ---------------------------------------------------------------------------
# Interactive mode
# ---------------------------------------------------------------------------

def interactive(server, token, out_dir):
    """Walk the customer through setup interactively."""
    print(f"\n{'=' * 60}")
    print(f"  MDS Customer Setup")
    print(f"{'=' * 60}")

    # 1. Health check
    print(f"\n-- Step 1: Check server connectivity --")
    if not check_health(server):
        print("  Cannot reach MDS server. Check your network / VPN.")
        sys.exit(1)

    # 2. Auth check
    print(f"\n-- Step 2: Verify authentication --")
    if not token:
        token = input("  Paste your Bearer token: ").strip()
        if not token:
            print("  No token provided. Exiting.")
            sys.exit(1)

    # Test auth by listing models
    try:
        models = list_entitled_models(server, token)
    except requests.HTTPError as e:
        if e.response.status_code == 401:
            print("  Authentication FAILED. Check your token.")
        elif e.response.status_code == 403:
            print("  Access DENIED. Your token is valid but not authorized.")
        else:
            print(f"  Server error: {e.response.status_code}")
        sys.exit(1)

    print(f"  Authenticated. You have access to {len(models)} model(s).")

    # 3. Show entitled models
    print(f"\n-- Step 3: Available models --")
    if not models:
        print("  No models available. Contact your MDS administrator.")
        sys.exit(0)

    for i, m in enumerate(models, 1):
        print(f"    [{i:>2}] {m['name']:<35} v{m['latest_version']}")

    # 4. Choose what to download
    print(f"\n-- Step 4: Download models --")
    print(f"  Output directory: {out_dir}")
    print()
    print("  Options:")
    print("    [A] Download ALL models")
    print("    [S] Select specific models (comma-separated numbers)")
    print("    [N] Skip downloads (just save config)")
    choice = input("  > ").strip().upper()

    to_download = []
    if choice == "A":
        to_download = models
    elif choice == "S":
        nums = input("  Enter model numbers (e.g. 1,3,5): ").strip()
        for n in nums.split(","):
            n = n.strip()
            if n.isdigit() and 1 <= int(n) <= len(models):
                to_download.append(models[int(n) - 1])
    elif choice == "N":
        pass
    else:
        print(f"  Invalid choice: {choice}")

    # 5. Download
    if to_download:
        print(f"\n-- Step 5: Downloading {len(to_download)} model(s) --")
        out_dir.mkdir(parents=True, exist_ok=True)
        total_t0 = time.time()
        total_files = 0
        for m in to_download:
            paths = download_model(server, token, m["name"],
                                   m["latest_version"], out_dir)
            total_files += len(paths)
        elapsed = time.time() - total_t0
        print(f"\n  Downloaded {total_files} file(s) for "
              f"{len(to_download)} model(s) in {elapsed:.1f}s")

    # 6. Save config
    print(f"\n-- Step 6: Save configuration --")
    out_dir.mkdir(parents=True, exist_ok=True)
    save_config(server, token, out_dir)

    # 7. Summary
    print(f"\n{'=' * 60}")
    print(f"  Setup complete!")
    print(f"{'=' * 60}")
    print(f"  Models directory: {out_dir.resolve()}")
    if to_download:
        print(f"  Downloaded: {', '.join(m['name'] for m in to_download)}")
    print(f"\n  To download more models later:")
    print(f"    python customer_setup.py --token <JWT> --models <name1>,<name2>")
    print(f"\n  To list all available models:")
    print(f"    python customer_setup.py --token <JWT> --list")
    print()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="MDS Customer Setup -- authenticate, browse, and download models",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--server", default=DEFAULT_SERVER,
                   help=f"MDS server URL (default: {DEFAULT_SERVER})")
    p.add_argument("--token", default=os.getenv("MDS_TOKEN", ""),
                   help="Bearer token (or set MDS_TOKEN env var)")
    p.add_argument("--output", "-o", default=DEFAULT_OUTPUT,
                   help=f"Output directory for models (default: {DEFAULT_OUTPUT})")
    p.add_argument("--list", action="store_true",
                   help="List available models and exit")
    p.add_argument("--models", default="",
                   help="Comma-separated model names to download")
    p.add_argument("--download-all", action="store_true",
                   help="Download all entitled models")
    args = p.parse_args()

    server = args.server.rstrip("/")
    token = args.token
    out_dir = Path(args.output)

    # Non-interactive: --list
    if args.list:
        if not token:
            print("ERROR: --token required"); sys.exit(1)
        models = list_entitled_models(server, token)
        print(f"\n  {len(models)} model(s) available:")
        for m in models:
            print(f"    {m['name']:<35} v{m['latest_version']}")
        sys.exit(0)

    # Non-interactive: --models or --download-all
    if args.models or args.download_all:
        if not token:
            print("ERROR: --token required"); sys.exit(1)
        models = list_entitled_models(server, token)
        if args.download_all:
            to_download = models
        else:
            wanted = {n.strip().lower() for n in args.models.split(",")}
            to_download = [m for m in models if m["name"].lower() in wanted]
            missing = wanted - {m["name"].lower() for m in to_download}
            if missing:
                print(f"  [WARN] Not found / not entitled: {', '.join(missing)}")
        if not to_download:
            print("  No models to download."); sys.exit(0)
        print(f"\n  Downloading {len(to_download)} model(s) to {out_dir}/")
        out_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        for m in to_download:
            download_model(server, token, m["name"], m["latest_version"], out_dir)
        print(f"\n  Done in {time.time() - t0:.1f}s")
        save_config(server, token, out_dir)
        sys.exit(0)

    # Interactive mode (default)
    interactive(server, token, out_dir)


if __name__ == "__main__":
    main()
