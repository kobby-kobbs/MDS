"""
Customer Onboarding Script for MDS.

End-to-end setup for a new customer:
  1. Generate RSA keypair (for JWT auth)
  2. Provision Azure resources (storage + ML registry via Bicep)
  3. Configure App Service env vars
  4. Add customer entry to customers.py
  5. Upload baseline models to their storage

Usage:
  python scripts/onboard_customer.py phonepe
  python scripts/onboard_customer.py acme --location eastus --models mnist,squeezenet
  python scripts/onboard_customer.py demo --skip-azure   # keys only, no Azure resources
"""

import argparse
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEYS_DIR = ROOT / "keys"
CUSTOMERS_DIR = ROOT / "customers"
CUSTOMERS_PY = ROOT / "src" / "mds" / "customers.py"
INFRA_DIR = ROOT / "infrastructure"

# Shared defaults
DEFAULT_RG = "customer-ml-registry-rg"
DEFAULT_APP = "mds-model-distribution"
DEFAULT_APP_RG = "customer-ml-registry-rg"
DEFAULT_LOCATION = "centralus"
DEFAULT_MODELS = []  # baseline models to upload (empty = none)


def _run(cmd, **kw):
    """Run a shell command, return stdout."""
    print(f"  $ {cmd}")
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        print(f"  [ERROR] {r.stderr.strip()}")
    return r


def step_generate_keys(customer: str):
    """Generate RSA keypair for customer JWT auth."""
    key_dir = KEYS_DIR / customer
    priv_path = key_dir / "private.pem"
    pub_path = key_dir / "public.pem"

    if priv_path.exists():
        print(f"  Keys already exist: {key_dir}")
        ans = input("  Overwrite? [y/N]: ").strip().lower()
        if ans != "y":
            print("  Skipping key generation.")
            return pub_path.read_text() if pub_path.exists() else None

    key_dir.mkdir(parents=True, exist_ok=True)

    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    priv_path.write_bytes(priv_pem)
    pub_path.write_bytes(pub_pem)
    print(f"  Private key: {priv_path}")
    print(f"  Public key:  {pub_path}")
    return pub_pem.decode()


def step_provision_azure(customer: str, location: str):
    """Create Azure resources via Bicep template."""
    bicep = INFRA_DIR / "onboard.bicep"
    if not bicep.exists():
        print(f"  [WARN] Bicep template not found: {bicep}")
        print("  Skipping Azure resource provisioning.")
        return None

    rg = f"mds-{customer}-rg"
    print(f"  Resource group: {rg}")
    print(f"  Location: {location}")

    # Create RG
    _run(f'az group create -n {rg} -l {location} -o none')

    # Deploy Bicep
    r = _run(f'az deployment group create -g {rg} --template-file "{bicep}" '
             f'--parameters customer={customer} -o json')
    if r.returncode != 0:
        return None

    try:
        result = json.loads(r.stdout)
        cfg = result["properties"]["outputs"]["config"]["value"]
        CUSTOMERS_DIR.mkdir(parents=True, exist_ok=True)
        cfg_path = CUSTOMERS_DIR / f"{customer}.json"
        cfg_path.write_text(json.dumps(cfg, indent=2))
        print(f"  Config saved: {cfg_path}")
        return cfg
    except (json.JSONDecodeError, KeyError) as e:
        print(f"  [ERROR] Failed to parse deployment output: {e}")
        return None


def step_configure_app(customer: str, cfg: dict, pub_pem: str):
    """Set App Service env vars for the new customer."""
    upper = customer.upper()
    registry = cfg.get("registry", f"mds-{customer}-registry")
    storage = cfg.get("storage", f"mds{customer}store")
    issuer = f"https://auth.{customer}.com"
    jwks_url = f"https://auth.{customer}.com/.well-known/jwks.json"

    # Escape public key for env var (replace newlines)
    pub_escaped = pub_pem.strip().replace("\n", "\\n") if pub_pem else ""

    settings = {
        f"CUSTOMER_{upper}_REGISTRY": registry,
        f"CUSTOMER_{upper}_STORAGE": storage,
        f"CUSTOMER_{upper}_ISSUER": issuer,
        f"CUSTOMER_{upper}_JWKS_URL": jwks_url,
    }
    if pub_escaped:
        settings[f"CUSTOMER_{upper}_PUBLIC_KEY"] = pub_escaped

    settings_str = " ".join(f'{k}="{v}"' for k, v in settings.items())
    print(f"  Setting {len(settings)} env vars on {DEFAULT_APP}...")
    r = _run(f'az webapp config appsettings set '
             f'--name {DEFAULT_APP} --resource-group {DEFAULT_APP_RG} '
             f'--settings {settings_str} -o none')

    if r.returncode == 0:
        print("  App Service configured.")
    else:
        print("  [WARN] Failed to set env vars. Set them manually:")
        for k, v in settings.items():
            print(f"    {k}={v}")


def step_update_customers_py(customer: str, cfg: dict):
    """Add customer entry to customers.py CUSTOMERS dict."""
    upper = customer.upper()
    content = CUSTOMERS_PY.read_text()

    # Check if customer already exists
    if f'"{customer}"' in content:
        print(f"  Customer '{customer}' already in customers.py")
        return

    # Build the new entry
    entry = textwrap.dedent(f'''\
    # Customer: {customer.title()}
    "{customer}": {{
        "name": "{customer.title()}",
        "sub": "{customer}-default",
        "issuer": os.getenv("CUSTOMER_{upper}_ISSUER", "https://auth.{customer}.com"),
        "jwks_url": os.getenv(
            "CUSTOMER_{upper}_JWKS_URL",
            "https://auth.{customer}.com/.well-known/jwks.json",
        ),
        "models": ["*"],
        "registry_name": os.getenv("CUSTOMER_{upper}_REGISTRY", ""),
        "storage_account": os.getenv("CUSTOMER_{upper}_STORAGE", ""),
    }},''')

    # Insert before the closing brace of CUSTOMERS dict
    marker = "    # Add more customers below"
    if marker in content:
        content = content.replace(marker, f"{entry}\n{marker}")
    else:
        # Fallback: insert before the last }
        idx = content.rfind("}")
        if idx > 0:
            content = content[:idx] + entry + "\n" + content[idx:]

    CUSTOMERS_PY.write_text(content)
    print(f"  Added '{customer}' to {CUSTOMERS_PY}")


def step_upload_baseline(customer: str, models: list[str], cfg: dict):
    """Upload baseline models to customer's storage."""
    if not models:
        print("  No baseline models specified. Skipping.")
        return

    models_dir = ROOT / "models"
    cli = ROOT / "mds_cli.py"
    priv_key = KEYS_DIR / customer / "private.pem"

    env = os.environ.copy()
    env["MDS_PRIVATE_KEY_PATH"] = str(priv_key)
    env["MDS_CUSTOMER_ID"] = f"{customer}-default"

    for model_name in models:
        # Look for model file/folder locally
        model_path = models_dir / model_name
        if not model_path.exists():
            # Try with common extensions
            for ext in [".onnx", ".onnx.data"]:
                candidate = models_dir / f"{model_name}{ext}"
                if candidate.exists():
                    model_path = candidate
                    break
        if not model_path.exists():
            print(f"  [WARN] Model not found locally: {model_name} (checked {models_dir})")
            continue

        print(f"  Uploading: {model_name} ({model_path})")
        _run(f'python "{cli}" upload-staged {model_name} "{model_path}" --method sdk',
             env=env)


def main():
    p = argparse.ArgumentParser(
        description="Onboard a new MDS customer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python scripts/onboard_customer.py phonepe
              python scripts/onboard_customer.py acme --location eastus
              python scripts/onboard_customer.py demo --skip-azure --models mnist,squeezenet
        """),
    )
    p.add_argument("customer", help="Customer short ID (e.g. phonepe, acme)")
    p.add_argument("--location", default=DEFAULT_LOCATION, help="Azure region (default: centralus)")
    p.add_argument("--models", default="", help="Comma-separated baseline models to upload")
    p.add_argument("--skip-azure", action="store_true", help="Skip Azure resource provisioning")
    p.add_argument("--skip-keys", action="store_true", help="Skip keypair generation")
    p.add_argument("--skip-upload", action="store_true", help="Skip baseline model upload")
    args = p.parse_args()

    customer = args.customer.lower().strip()
    models = [m.strip() for m in args.models.split(",") if m.strip()] if args.models else DEFAULT_MODELS
    cfg = {}

    print(f"\n{'='*60}")
    print(f"  MDS Customer Onboarding: {customer}")
    print(f"{'='*60}")

    # Step 1: Keys
    pub_pem = None
    if not args.skip_keys:
        print(f"\n--- Step 1: Generate RSA Keypair ---")
        pub_pem = step_generate_keys(customer)
    else:
        print(f"\n--- Step 1: Skipped (--skip-keys) ---")
        pub_path = KEYS_DIR / customer / "public.pem"
        if pub_path.exists():
            pub_pem = pub_path.read_text()

    # Step 2: Azure resources
    if not args.skip_azure:
        print(f"\n--- Step 2: Provision Azure Resources ---")
        cfg = step_provision_azure(customer, args.location) or {}
    else:
        print(f"\n--- Step 2: Skipped (--skip-azure) ---")
        cfg = {
            "registry": f"mds-{customer}-registry",
            "storage": f"mds{customer}store",
            "container": "models",
        }

    # Step 3: Configure App Service
    if cfg and not args.skip_azure:
        print(f"\n--- Step 3: Configure App Service ---")
        step_configure_app(customer, cfg, pub_pem)
    else:
        print(f"\n--- Step 3: Skipped (no Azure config) ---")

    # Step 4: Update customers.py
    print(f"\n--- Step 4: Update customers.py ---")
    step_update_customers_py(customer, cfg)

    # Step 5: Upload baseline models
    if models and not args.skip_upload:
        print(f"\n--- Step 5: Upload Baseline Models ---")
        step_upload_baseline(customer, models, cfg)
    else:
        print(f"\n--- Step 5: Skipped (no baseline models) ---")

    # Summary
    print(f"\n{'='*60}")
    print(f"  Onboarding complete: {customer}")
    print(f"{'='*60}")
    print(f"  Keys:     {KEYS_DIR / customer}")
    print(f"  Config:   {CUSTOMERS_DIR / f'{customer}.json'}" if cfg else "  Config:   (skipped)")
    print(f"  Models:   {', '.join(models)}" if models else "  Models:   (none)")
    print(f"\n  Next steps:")
    print(f"    1. Share keys/{customer}/private.pem with the customer (secure channel)")
    print(f"    2. Customer configures their JWT issuer to match: https://auth.{customer}.com")
    print(f"    3. Redeploy server: python make_deploy_zip.py && az webapp deploy ...")
    print(f"    4. Test: python mds_cli.py --base-url https://mds-model-distribution.azurewebsites.net list")
    print()


if __name__ == "__main__":
    main()
