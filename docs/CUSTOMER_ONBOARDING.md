# Customer Onboarding Guide

> **Audience**: Engineering teams at customer organizations (e.g. PhonePe) who need to integrate their ML models with the Model Distribution Service (MDS).

---

## Overview

MDS is a private model distribution service that:
1. Stores your ONNX / custom ML models in Azure Blob Storage
2. Registers model metadata in Azure ML Registry
3. Serves a **Foundry Local SDK–compatible catalog** so your on-device inference stack can discover and download models
4. Authenticates every request with **JWT / RS256** tokens issued by _your_ auth system

```
┌──────────────┐     JWT     ┌─────────┐     Azure ML     ┌──────────────┐
│  Your Auth   │────────────▶│   MDS   │────────────────▶  │  ML Registry │
│   Service    │             │  (API)  │                   └──────────────┘
└──────────────┘             │         │     Azure Blob    ┌──────────────┐
                             │         │────────────────▶  │ Blob Storage │
┌──────────────┐  /catalog   │         │                   └──────────────┘
│ Foundry Local│────────────▶│         │
│     SDK      │  /download  │         │
└──────────────┘             └─────────┘
```

---

## Step 1 — Set Up Your JWKS Endpoint

MDS validates JWTs using your **JSON Web Key Set (JWKS)** endpoint.

### Requirements

| Item | Specification |
|------|--------------|
| **URL** | `https://auth.<your-domain>.com/.well-known/jwks.json` |
| **Algorithm** | RS256 (RSA + SHA-256) |
| **Key format** | JWK with `kid` (Key ID), `kty: RSA`, `use: sig` |
| **Availability** | Must be publicly reachable (HTTPS, no auth) |
| **Key rotation** | Supported — MDS caches JWKS for 1 hour with stale fallback |

### Example JWKS Response

```json
{
  "keys": [
    {
      "kty": "RSA",
      "kid": "prod-key-2025-01",
      "use": "sig",
      "alg": "RS256",
      "n": "<base64url-encoded modulus>",
      "e": "AQAB"
    }
  ]
}
```

### Key Rotation

When rotating keys:
1. Add the **new key** to the JWKS endpoint first
2. Start issuing tokens with the new `kid`
3. Keep the **old key** in JWKS for at least 2 hours (tokens may be cached)
4. Remove the old key after the grace period

MDS handles rotation automatically — it caches JWKS for 1 hour and falls back to stale keys if the endpoint is temporarily unreachable.

---

## Step 2 — Configure Your JWT Tokens

Every API call to MDS requires a valid JWT in the `Authorization: Bearer <token>` header.

### Required JWT Claims

| Claim | Value | Example |
|-------|-------|---------|
| `iss` | `https://auth.<customer_id>.com` | `https://auth.phonepe.com` |
| `sub` | Your subject identifier | `phonepe-india` |
| `aud` | `model-distribution-service` | (fixed value) |
| `iat` | Issued-at timestamp | `1719849600` |
| `exp` | Expiry timestamp (max 1 hour recommended) | `1719853200` |

### Required JWT Header

| Field | Value |
|-------|-------|
| `alg` | `RS256` |
| `kid` | Key ID matching a key in your JWKS endpoint |

### Example Token Generation (Python)

```python
import jwt
from datetime import datetime, timedelta, timezone

token = jwt.encode(
    {
        "sub": "phonepe-india",
        "iss": "https://auth.phonepe.com",
        "aud": "model-distribution-service",
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    },
    private_key_pem,
    algorithm="RS256",
    headers={"kid": "prod-key-2025-01"},
)
```

---

## Step 3 — Register with MDS

Contact the MDS team to register your organization. You will provide:

| Information | Purpose |
|-------------|---------|
| **Customer ID** | Short identifier (e.g. `phonepe`) — used to match JWT issuers |
| **JWKS URL** | Your JWKS endpoint for key verification |
| **Model entitlements** | Which models you can access (`*` = all, or specific names) |
| **Contact email** | For incident notifications |

The MDS team will add your configuration to the server:

```python
# Added to customers.py
CUSTOMERS["phonepe"] = {
    "name": "PhonePe India",
    "sub": "phonepe-india",
    "models": ["*"],
    "jwks_url": "https://auth.phonepe.com/.well-known/jwks.json",
}
```

---

## Step 4 — Upload Models

### Option A: Single-File Upload (< 500 MB)

```bash
curl -X POST https://mds-model-distribution.azurewebsites.net/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "model_name=my-model" \
  -F "description=My custom ONNX model" \
  -F "task=text-generation" \
  -F "input_modalities=text" \
  -F "output_modalities=text" \
  -F "device=cpu" \
  -F "model_type=onnx" \
  -F "file=@my-model.onnx"
```

**Auto-metadata**: MDS automatically extracts metadata from uploaded files:
- `.onnx` files → model type, task hints, modality hints from operators
- `.zip` / `.tar.gz` archives → parses `config.json`, `tokenizer_config.json`, `README.md`
  for model architecture, tokenizer info, license, and task inference

### Option B: Staged Upload for Large Models (> 500 MB / Multi-File)

For large models or model directories with multiple files, use the **two-phase upload**:

```bash
# 1. Begin staging — get a SAS upload URL (valid for 4 hours)
curl -X POST https://mds-model-distribution.azurewebsites.net/upload/begin \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model_name": "large-model", "task": "chat-completion", "device": "npu"}'

# Response:
# {
#   "session_id": "abc123...",
#   "upload_url": "https://customermodelstorage.blob.core.windows.net/models/large-model/v1?sv=...",
#   "blob_prefix": "large-model/v1",
#   "expires_in": "4 hours"
# }

# 2. Upload files with azcopy
azcopy copy ./model_directory "$UPLOAD_URL" --recursive

# 3. Complete staging — register the model
curl -X POST https://mds-model-distribution.azurewebsites.net/upload/complete \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "abc123..."}'
```

### Upload Form Fields Reference

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `model_name` | ✅ | — | Unique model identifier |
| `description` | | `""` | Human-readable description |
| `alias` | | model_name | Display alias |
| `task` | | `"custom"` | `text-generation`, `chat-completion`, `classification`, etc. |
| `input_modalities` | | `"text"` | `text`, `image`, `audio` |
| `output_modalities` | | `"text"` | `text`, `image`, `audio` |
| `device` | | `"cpu"` | `cpu`, `gpu`, `npu` |
| `execution_provider` | | `"cpuexecutionprovider"` | ONNX runtime EP |
| `model_type` | | `"onnx"` | `onnx`, `pytorch`, etc. |
| `prompt_template` | | `""` | Chat template (Jinja2) |
| `license_id` | | `""` | SPDX license ID |
| `max_output_tokens` | | `""` | Max output token count |
| `supports_tool_calling` | | `""` | `"true"` if model supports tools |

---

## Step 5 — Integrate with Foundry Local SDK

MDS exposes a catalog API that is **compatible with the Foundry Local SDK**.

### Set AzureCatalogUri

Point the SDK at your MDS instance:

**JavaScript / TypeScript:**
```javascript
const client = new FoundryLocalClient({
    azureCatalogUri: "https://mds-model-distribution.azurewebsites.net"
});
```

**C# / .NET:**
```csharp
var client = new FoundryLocalClient(new FoundryLocalClientOptions {
    AzureCatalogUri = new Uri("https://mds-model-distribution.azurewebsites.net")
});
```

**Python:**
```python
client = FoundryLocalClient(
    azure_catalog_uri="https://mds-model-distribution.azurewebsites.net"
)
```

### SDK Request Flow

```
SDK                          MDS                        Azure
 │                            │                           │
 │  POST /catalog             │                           │
 │  Authorization: Bearer ... │                           │
 │───────────────────────────▶│  validate JWT             │
 │                            │  list models from ───────▶│ ML Registry
 │                            │  filter by entitlements   │
 │  { indexEntitiesResponse } │                           │
 │◀───────────────────────────│                           │
 │                            │                           │
 │  POST /download            │                           │
 │  ?model=phi3&version=1     │                           │
 │───────────────────────────▶│  generate SAS URL ───────▶│ Blob Storage
 │  { download_url: "..." }   │                           │
 │◀───────────────────────────│                           │
 │                            │                           │
 │  GET <download_url>        │                           │
 │────────────────────────────┼──────────────────────────▶│
 │  <model bytes>             │                           │
 │◀───────────────────────────┼───────────────────────────│
```

### Catalog Pagination

Large catalogs are paginated. Use `continuationToken` to fetch subsequent pages:

```json
// Request — first page
{"indexEntitiesRequest": {"pageSize": 10}}

// Response
{
  "indexEntitiesResponse": {
    "totalCount": 50,
    "value": [...],
    "continuationToken": "eyJzIjoxMCwicCI6MTB9"
  }
}

// Request — next page
{"indexEntitiesRequest": {"continuationToken": "eyJzIjoxMCwicCI6MTB9"}}
```

---

## Step 6 — Verify Integration

Use these curl commands to verify your setup:

### 1. Health Check (no auth required)
```bash
curl https://mds-model-distribution.azurewebsites.net/health
# {"status": "ok", "registry": "customer-phone", "storage": "customermodelstorage"}
```

### 2. List Models
```bash
curl https://mds-model-distribution.azurewebsites.net/models
```

### 3. Fetch Catalog (with auth)
```bash
curl -X POST https://mds-model-distribution.azurewebsites.net/catalog \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

### 4. Download a Model (with auth)
```bash
curl -X POST "https://mds-model-distribution.azurewebsites.net/download?model=my-model&version=1" \
  -H "Authorization: Bearer $TOKEN"
```

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| **401 Unknown token issuer** | JWT `iss` doesn't match `https://auth.<id>.com` | Check your issuer URL format |
| **401 Invalid signature** | JWT signed with wrong key | Ensure `kid` header matches JWKS |
| **401 Token expired** | JWT `exp` is in the past | Issue a fresh token |
| **401 Invalid audience** | JWT `aud` ≠ `model-distribution-service` | Set audience correctly |
| **403 Not entitled** | Customer not authorized for this model | Contact MDS team for entitlement |
| **404 Model not found** | Model name or version doesn't exist | Check `/models` endpoint |

---

## Support

- **MDS API Base URL**: `https://mds-model-distribution.azurewebsites.net`
- **Health Check**: `GET /health`
- **API Documentation**: See `README.md` in the MDS repository
- **JWT Specification**: See `docs/JWT_SPECIFICATION.md`
- **SDK Integration**: See `docs/SDK_INTEGRATION.md`
