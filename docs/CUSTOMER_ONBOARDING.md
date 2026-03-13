# Customer Onboarding Guide

> **Audience**: Engineering teams at customer organizations (e.g. PhonePe) who need to integrate their ML models with the Model Distribution Service (MDS).

---

## Overview

MDS is a private model distribution service that:
1. Stores your ONNX / custom ML models in Azure Blob Storage
2. Registers model metadata in Azure ML Registry
3. Serves a **Foundry Local SDK–compatible catalog** so your on-device inference stack can discover and download models
4. Authenticates every request with **JWT / RS256** tokens issued by _your_ auth system

### Authentication Modes

| Mode | Registration | Best For |
|------|-------------|----------|
| **Self-service JWT** (recommended) | None — token carries all claims | New customers, any IdP |
| **API key** (legacy) | Required via `/admin/register` | Backward compat with FL Core |

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
| `iss` | Your IdP's issuer URL | `https://dev-xxx.us.auth0.com/` |
| `sub` | Your subject identifier | `my-app@clients` |
| `aud` | `model-distribution-service` | (fixed value) |
| `iat` | Issued-at timestamp | `1719849600` |
| `exp` | Expiry timestamp (max 24 hours recommended) | `1719853200` |

### Self-Service Claims (Recommended)

These claims enable the **self-service flow** — no MDS pre-registration needed:

| Claim | Value | Example |
|-------|-------|---------|
| `registry_name` | Your Azure ML Registry name | `customer-phone` |
| `storage_account` | Your Azure Storage account name | `customermodelstorage` |
| `entitlements` | Model access rules (object) | `{"models":["*"],"versions":["*"]}` |

> **Note:** Auth0 requires a URL namespace for object claims. Use `https://mds.microsoft.com/entitlements` as the claim name. If `entitlements` is omitted, MDS defaults to wildcard access.

### Required JWT Header

| Field | Value |
|-------|-------|
| `alg` | `RS256` |
| `kid` | Key ID matching a key in your JWKS endpoint |

### IdP-Specific Configuration

#### Auth0
1. Create a **Machine-to-Machine** application
2. Authorize it against API `model-distribution-service`
3. Create an **Action** with trigger `Machine to Machine / Credentials Exchange`:
```js
exports.onExecuteCredentialsExchange = async (event, api) => {
  api.accessToken.setCustomClaim('registry_name', '<your-registry>');
  api.accessToken.setCustomClaim('storage_account', '<your-storage>');
  api.accessToken.setCustomClaim('https://mds.microsoft.com/entitlements', {
    models: ['*'], versions: ['*']
  });
};
```
4. Deploy and wire the action into the **Machine to Machine** flow
5. Token endpoint: `https://<domain>/oauth/token`

#### Okta
1. Create an OAuth2 **service application**
2. Add custom claims to your authorization server
3. Token endpoint: `https://<domain>/oauth2/default/v1/token`

#### Azure AD / Entra ID
1. Register an application and create a client secret
2. Configure custom claims via claims-mapping policy
3. Token endpoint: `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`

---

## Step 3 — Provision Azure Resources

Run the onboarding script to create storage and ML registry:

### Self-service JWT (recommended)
```powershell
.\infrastructure\onboard.ps1 -Customer acme `
  -Issuer "https://acme.us.auth0.com/" `
  -SelfServiceJwt `
  -TokenEndpoint "https://acme.us.auth0.com/oauth/token" `
  -ClientId "your-client-id"
```

This creates Azure resources and outputs your FL Core config — no MDS registration needed.

### API key mode (legacy)
```powershell
.\infrastructure\onboard.ps1 -Customer acme `
  -Issuer "https://auth.acme.com" `
  -MdsUrl "https://mds-model-distribution.azurewebsites.net" `
  -AdminKey "<admin-key>"
```

The MDS team will provide the admin key. An API key is generated and shown once.

---

## Step 4 — Register Models in Azure ML Registry

Models are registered directly in the Azure ML Registry using the Azure CLI or SDK. Contact the MDS team for access to your dedicated registry.

```bash
# Register a model using the Azure CLI
az ml model create \
  --name my-model \
  --version 1 \
  --path ./model-files/ \
  --registry-name <your-registry> \
  --tags foundryLocal=true task=chat-completion device=cpu modelType=onnx
```

Required tags for Foundry Local compatibility:
- `foundryLocal`: Set to `"true"` to include in FL catalog
- `task`: e.g. `chat-completion`, `text-generation`, `classification`
- `device`: e.g. `cpu`, `gpu`, `npu`
- `modelType`: e.g. `onnx`

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
# {"status": "ok"}
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
