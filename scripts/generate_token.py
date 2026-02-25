"""Generate a test JWT token and RSA keypair for MDS demo testing.

Usage:
    python scripts/generate_token.py                  # Generate token for phonepe
    python scripts/generate_token.py --token-only     # Print token only (reuse saved keys)
    python scripts/generate_token.py --deploy         # Generate token + push public key to App Service
    python scripts/generate_token.py --customer acme  # Generate token for a different customer
    python scripts/generate_token.py --jwks-only      # Start JWKS server only (legacy)

This script:
    1. Generates (or loads) an RSA-2048 keypair in keys/<customer>/
    2. Prints a valid JWT token for the specified customer
    3. Optionally pushes the public key to App Service as an env var
       (CUSTOMER_<ID>_PUBLIC_KEY) so no JWKS blob/server is needed.
"""

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

try:
    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
except ImportError:
    print("Missing dependencies. Install with:")
    print("  pip install pyjwt cryptography")
    sys.exit(1)


KEYS_DIR = Path(__file__).resolve().parent.parent / "keys"
AUDIENCE = "model-distribution-service"
TOKEN_HOURS = 24

# App Service deployment info (for --deploy)
APP_NAME = "mds-model-distribution"
RESOURCE_GROUP = "customer-ml-registry-rg"

# Customer profiles - matches customers.py entries.
# Add new test customers here.
TEST_CUSTOMERS = {
    "phonepe": {
        "issuer": "https://auth.phonepe.com",
        "subject": "phonepe-india",
        "kid": "demo-kid-1",
    },
    "acme": {
        "issuer": "https://auth.acme.com",
        "subject": "acme-corp",
        "kid": "acme-kid-1",
    },
}


def ensure_keypair(customer_id: str = "phonepe"):
    """Generate or load RSA keypair for a customer."""
    cust_dir = KEYS_DIR / customer_id
    cust_dir.mkdir(parents=True, exist_ok=True)

    # Backwards compat: if keys exist at old location, use those for phonepe
    priv_file = cust_dir / "private.pem"
    pub_file = cust_dir / "public.pem"

    if customer_id == "phonepe" and not priv_file.exists():
        old_priv = KEYS_DIR / "demo_private.pem"
        old_pub = KEYS_DIR / "demo_public.pem"
        if old_priv.exists() and old_pub.exists():
            priv_file.write_text(old_priv.read_text())
            pub_file.write_text(old_pub.read_text())
            print(f"[OK] Migrated existing keypair to {cust_dir}")

    if priv_file.exists() and pub_file.exists():
        print(f"[OK] Loaded existing keypair from {cust_dir}")
        private_pem = priv_file.read_text()
        public_pem = pub_file.read_text()
    else:
        print(f"[OK] Generating new RSA-2048 keypair for {customer_id}...")
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        public_pem = private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        priv_file.write_text(private_pem)
        pub_file.write_text(public_pem)
        print(f"[OK] Saved keypair to {cust_dir}")

    return private_pem, public_pem


def generate_token(private_pem: str, *, issuer: str, subject: str, kid: str) -> str:
    """Generate a signed JWT token."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "iss": issuer,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + timedelta(hours=TOKEN_HOURS),
    }
    return pyjwt.encode(
        payload,
        private_pem,
        algorithm="RS256",
        headers={"kid": kid},
    )


def build_jwks(public_pem: str, kid: str) -> dict:
    """Build a JWKS document from the public key."""
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    pub_key = load_pem_public_key(public_pem.encode())
    numbers: RSAPublicNumbers = pub_key.public_numbers()

    def _b64url(num: int, length: int) -> str:
        return base64.urlsafe_b64encode(
            num.to_bytes(length, byteorder="big")
        ).decode().rstrip("=")

    n_bytes = (numbers.n.bit_length() + 7) // 8
    e_bytes = (numbers.e.bit_length() + 7) // 8

    return {
        "keys": [
            {
                "kty": "RSA",
                "alg": "RS256",
                "use": "sig",
                "kid": kid,
                "n": _b64url(numbers.n, n_bytes),
                "e": _b64url(numbers.e, e_bytes),
            }
        ]
    }


def deploy_public_key(customer_id: str, public_pem: str):
    """Push the public key to Azure App Service as an env var.

    Sets CUSTOMER_<ID>_PUBLIC_KEY so the deployed MDS app can verify
    tokens without needing a JWKS URL / blob.
    """
    import subprocess

    env_name = f"CUSTOMER_{customer_id.upper()}_PUBLIC_KEY"
    # Collapse newlines to literal \n for safe transport in az CLI
    escaped = public_pem.strip().replace("\n", "\\n")

    cmd = [
        "az", "webapp", "config", "appsettings", "set",
        "--name", APP_NAME,
        "--resource-group", RESOURCE_GROUP,
        "--settings", f"{env_name}={escaped}",
    ]
    print(f"\n[DEPLOY] Setting {env_name} on {APP_NAME}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"[OK] {env_name} set successfully")
    else:
        print(f"[ERROR] az CLI failed: {result.stderr}")
        sys.exit(1)


def start_jwks_server(jwks_doc: dict, port: int = 7777):
    """Start a minimal JWKS HTTP server."""
    jwks_json = json.dumps(jwks_doc, indent=2).encode()

    class JWKSHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/.well-known/jwks.json":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.write = self.wfile.write
                self.wfile.write(jwks_json)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            print(f"  [JWKS] {args[0]}")

    server = HTTPServer(("0.0.0.0", port), JWKSHandler)
    print(f"\n[OK] JWKS server running at http://localhost:{port}/.well-known/jwks.json")
    print("     Keep this running while testing against the deployed MDS service.")
    print("     Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[OK] JWKS server stopped.")
        server.server_close()


def main():
    customers_list = ", ".join(TEST_CUSTOMERS.keys())
    parser = argparse.ArgumentParser(
        description="Generate test tokens for MDS",
        epilog=f"Available customers: {customers_list}",
    )
    parser.add_argument(
        "--customer", "-c", default="phonepe",
        help=f"Customer ID to generate token for (default: phonepe)",
    )
    parser.add_argument(
        "--token-only", action="store_true",
        help="Print token and exit",
    )
    parser.add_argument(
        "--deploy", action="store_true",
        help="Push public key to App Service (no JWKS blob needed)",
    )
    parser.add_argument(
        "--jwks-only", action="store_true",
        help="Start local JWKS server only (legacy)",
    )
    parser.add_argument("--port", type=int, default=7777, help="JWKS server port")
    args = parser.parse_args()

    cid = args.customer.lower()
    if cid not in TEST_CUSTOMERS:
        print(f"Unknown customer '{cid}'. Available: {customers_list}")
        sys.exit(1)

    profile = TEST_CUSTOMERS[cid]
    private_pem, public_pem = ensure_keypair(cid)
    jwks_doc = build_jwks(public_pem, profile["kid"])

    if args.jwks_only:
        start_jwks_server(jwks_doc, args.port)
        return

    token = generate_token(
        private_pem,
        issuer=profile["issuer"],
        subject=profile["subject"],
        kid=profile["kid"],
    )

    print(f"\n{'=' * 60}")
    print(f"  JWT Token for '{cid}' (valid {TOKEN_HOURS}h)")
    print(f"{'=' * 60}")
    print(f"\n{token}\n")
    print(f"{'=' * 60}")
    print(f"  Customer: {cid}")
    print(f"  Issuer:   {profile['issuer']}")
    print(f"  Subject:  {profile['subject']}")
    print(f"  Audience: {AUDIENCE}")
    print(f"  KID:      {profile['kid']}")
    print(f"{'=' * 60}")

    if args.deploy:
        deploy_public_key(cid, public_pem)

    if args.token_only or args.deploy:
        return

    print("\nStarting local JWKS server (Ctrl+C to stop)...")
    start_jwks_server(jwks_doc, args.port)


if __name__ == "__main__":
    main()
