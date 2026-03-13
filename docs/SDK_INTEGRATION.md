# Foundry Local SDK Integration Guide

> **Note**: This document covers how MDS works as an alternate catalog.
> For the recommended integration approach using the **Private Catalog SDK**
> (`AddCatalogAsync` / auto-connect), see [`PRIVATE_CATALOG_GUIDE.md`](PRIVATE_CATALOG_GUIDE.md).

## How MDS Works as an Alternate Catalog

Foundry Local normally fetches models from the **public Azure AI catalog**.
MDS acts as a **private alternate catalog** — the SDK talks to MDS to
discover and download private models, alongside the public catalog.

### Architecture

```
┌──────────────────────┐          ┌──────────────────────┐
│  End User Device     │  POST    │  MDS (this service)  │
│                      │ /catalog │                      │
│  Foundry Local SDK ──┼─────────►│  FastAPI + JWT auth  │
│  (JS / C# / Python)  │◄─────────┤  Azure ML Registry   │
│                      │  models  │  Azure Blob Storage  │
└──────────────────────┘          └──────────────────────┘
```

1. The SDK calls `POST /catalog` (with a signed JWT) → MDS returns models in
   the official `indexEntitiesResponse` format.
2. The SDK calls `POST /download` → MDS validates the JWT, checks entitlements,
   and returns a time-limited SAS URL.
3. The SDK downloads the model binary directly from Azure Blob Storage.

### Integration Methods

| Method | Approach | Best For |
|--------|----------|----------|
| **Auto-connect** (recommended) | Set env vars or config, SDK handles everything | Production apps, zero-code integration |
| **AddCatalogAsync** | Call SDK method in code | Apps needing fine-grained control |
| **AzureCatalogUri** (legacy) | Point entire catalog at MDS | Backward compatibility |

See [`PRIVATE_CATALOG_GUIDE.md`](PRIVATE_CATALOG_GUIDE.md) for detailed setup.

### Authentication Flow

```
1. App → Auth0/Okta/Entra ID → JWT token (OAuth2 client credentials)
2. FL Core → MDS /catalog      → model list (JWT validated via JWKS)
3. FL Core → MDS /download     → SAS URL   (JWT + entitlement check)
4. FL Core → Azure Blob        → model binary (direct download via SAS)
```

### What MDS Implements

| SDK Operation        | MDS Endpoint          | Status |
|---------------------|-----------------------|--------|
| List catalog models | `POST /catalog`       | Done   |
| Get model info      | `GET /models/{name}`  | Done   |
| Download model      | `POST /download`      | Done   |
| Health check        | `GET /health`         | Done   |

### Catalog Response Format

MDS returns the same JSON schema as the public Azure AI catalog:

```json
{
  "indexEntitiesResponse": {
    "totalCount": 2,
    "value": [
      {
        "annotations": {
          "name": "fraud-model",
          "tags": { "alias": "fraud-model", "task": "classification", "foundryLocal": "true" },
          "systemCatalogData": { "publisher": "MDS", "displayName": "fraud-model" }
        },
        "properties": {
          "version": 1,
          "variantInfo": {
            "variantMetadata": { "modelType": "onnx", "device": "cpu" }
          }
        }
      }
    ]
  }
}
```
