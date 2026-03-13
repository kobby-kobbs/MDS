# JWT Token Specification for MDS

## Overview

Customers authenticate to MDS using **RS256-signed JWT tokens**. Each customer runs their own authentication service and signs tokens with their private key. MDS verifies tokens using the customer's public key.

---

## Required Claims

| Claim | Type | Description | Example |
|-------|------|-------------|---------|
| `iss` | string | **Issuer** - URL identifying the customer's auth service | `https://auth.phonepe.com` |
| `sub` | string | **Subject** - Unique identifier for the user/service | `model-service-01` |
| `aud` | string | **Audience** - Must be `model-distribution-service` | `model-distribution-service` |
| `exp` | number | **Expiration** - Unix timestamp when token expires | `1738800000` |
| `iat` | number | **Issued At** - Unix timestamp when token was issued | `1738796400` |

---

## Optional Claims (Recommended)

| Claim | Type | Description | Example |
|-------|------|-------------|---------|
| `jti` | string | **JWT ID** - Unique identifier for the token (for replay protection) | `abc123-def456` |
| `nbf` | number | **Not Before** - Token not valid before this time | `1738796400` |
| `scope` | string | **Scope** - Space-separated permissions | `models:read` |
| `device_id` | string | **Device ID** - For mobile SDK, identifies the device | `pixel7-abc123` |
| `app_version` | string | **App Version** - Customer's app version | `2.5.1` |

---

## Token Structure

### Header
```json
{
  "alg": "RS256",
  "typ": "JWT",
  "kid": "key-2024-01"  // Optional: Key ID for key rotation
}
```

### Payload
```json
{
  "iss": "https://auth.phonepe.com",
  "sub": "fraud-detection-service",
  "aud": "model-distribution-service",
  "exp": 1738800000,
  "iat": 1738796400,
  "jti": "unique-token-id-12345",
  "scope": "models:read",
  "device_id": "production-server-01"
}
```

### Signature
RS256 signature using customer's private key.

---

## Issuer URL Convention

MDS derives the JWKS URL automatically from the token's `iss` claim:

```
JWKS URL = {iss}/.well-known/jwks.json
```

Any publicly-reachable OIDC-compliant issuer works — Auth0, Okta, Entra ID, or custom. No customer pre-registration needed.

---

## Self-Service Claims

These custom claims enable the **self-service JWT flow** where no pre-registration with MDS is needed. When present, MDS uses these claims directly instead of looking up customer config.

| Claim | Type | Required | Description | Example |
|-------|------|----------|-------------|---------|
| `registry_name` | string | Yes | Azure ML Registry name | `customer-phone` |
| `storage_account` | string | Yes | Azure Blob Storage account | `customermodelstorage` |
| `entitlements` | object | No | Model/version access rules | `{"models":["*"],"versions":["*"]}` |

**Entitlements format:**
```json
{
  "models": ["model-a", "model-b"],  // or ["*"] for all
  "versions": ["1", "2"]             // or ["*"] for all
}
```

**Notes:**
- If `entitlements` is omitted, defaults to wildcard access (all models, all versions)
- Auth0 requires a URL namespace for object claims: use `https://mds.microsoft.com/entitlements` as the claim name
- MDS accepts `entitlements` as a JSON object, JSON-encoded string, or the namespaced key

---

## Issuer Examples
- Auth0: `https://dev-xxx.us.auth0.com/`
- Okta: `https://acme.okta.com/oauth2/default`
- Entra ID: `https://login.microsoftonline.com/{tenant}/v2.0`
- Custom: `https://auth.phonepe.com`

MDS derives the JWKS URL from the issuer to fetch the public key for signature verification.

---

## Scopes (Future Enhancement)

| Scope | Permission |
|-------|------------|
| `models:read` | Download models |
| `models:delete` | Delete models |
| `models:admin` | Full access |

Currently, MDS uses entitlements per model. Scopes may be added for finer-grained control.

---

## Token Lifetime

| Use Case | Recommended Lifetime |
|----------|---------------------|
| Server-to-server | 1 hour |
| Mobile SDK | 15 minutes |
| One-time download | 5 minutes |

---

## Key Rotation

Customers should rotate their RSA key pairs periodically:

1. **Generate new key pair**
2. **Add new public key to MDS** (old key still active)
3. **Update auth service** to sign with new key
4. **Remove old public key** from MDS after grace period

Using the `kid` (Key ID) header claim helps identify which key to use during rotation.

---

## JWKS Endpoint (Production)

In production, instead of hardcoding public keys, MDS will fetch them from customer's JWKS endpoint:

```
https://auth.{customer_id}.com/.well-known/jwks.json
```

Example JWKS response:
```json
{
  "keys": [
    {
      "kty": "RSA",
      "kid": "key-2024-01",
      "use": "sig",
      "alg": "RS256",
      "n": "...",
      "e": "AQAB"
    }
  ]
}
```

---

## Validation Steps (MDS)

1. **Parse token** - Extract header and payload
2. **Check `alg`** - Must be RS256 (reject none, HS256, etc.)
3. **Extract `iss`** - Identify customer from issuer URL
4. **Fetch public key** - From JWKS or config
5. **Verify signature** - Using customer's public key
6. **Check `exp`** - Token must not be expired
7. **Check `aud`** - Must be `model-distribution-service`
8. **Check `iat`** - Should be in the past
9. **Check entitlements** - Does customer have access to requested model?

---

## Error Responses

| HTTP Code | Error | Description |
|-----------|-------|-------------|
| 401 | `Invalid authorization header` | Missing or malformed Bearer token |
| 401 | `Invalid token format` | JWT is not properly structured |
| 401 | `Unknown token issuer` | Issuer not recognized |
| 401 | `Token expired` | `exp` claim is in the past |
| 401 | `Invalid signature` | Signature verification failed |
| 401 | `Invalid audience` | `aud` is not `model-distribution-service` |
| 403 | `Not entitled to: {model}` | Customer doesn't have access to this model |

---

## Industry Standards Comparison

| Standard | MDS Implementation |
|----------|-------------------|
| **RFC 7519** (JWT) | ✅ Fully compliant |
| **RFC 7515** (JWS) | ✅ RS256 signatures |
| **RFC 7517** (JWK) | ✅ JWKS with 1hr cache + stale fallback |
| **OpenID Connect** | 🔄 Partial (not full OIDC) |
| **OAuth 2.0** | 🔄 Partial (token format only) |

---

## Sample Token Generation (Python)

```python
import jwt
from datetime import datetime, timedelta

# Customer's private key (keep secret!)
private_key = open("private_key.pem").read()

# Generate token
token = jwt.encode(
    {
        "iss": "https://auth.phonepe.com",
        "sub": "model-downloader",
        "aud": "model-distribution-service",
        "exp": datetime.utcnow() + timedelta(hours=1),
        "iat": datetime.utcnow(),
        "jti": str(uuid.uuid4())
    },
    private_key,
    algorithm="RS256"
)

print(token)
```

---

*Last Updated: February 5, 2026*
