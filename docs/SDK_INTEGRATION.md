# Foundry Local SDK Integration Guide

## Who Owns What

```
┌─────────────────────────────────────────────────────────────────┐
│                    WHAT WE OWN (MDS)                            │
│                                                                 │
│  POST /catalog  — returns indexEntitiesResponse JSON            │
│  POST /download — validates JWT, returns SAS URL                │
│  POST /upload   — accepts model + FL metadata                   │
│  GET  /models/* — model detail in FL format                     │
│  JWKS validation — verifies customer tokens                     │
│                                                                 │
│  ✅ All implemented and deployed at                              │
│     https://mds-model-distribution.azurewebsites.net            │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│              WHAT THE CUSTOMER APP OWNS                         │
│                                                                 │
│  1. Authenticate user (their IdP → JWT)                         │
│  2. Initialize Foundry Local SDK with AzureCatalogUri → MDS     │
│  3. Attach JWT Authorization header on each request to MDS      │
│  4. Call catalog.getModels() → SDK hits MDS POST /catalog       │
│  5. Call model.download()   → SDK hits MDS POST /download       │
│     → SDK downloads .onnx from SAS URL to local cache           │
│  6. Call model.load()       → SDK loads model into local memory │
│  7. Call chatClient.completeChat() → runs inference LOCALLY     │
│                                                                 │
│  ⚠️  Steps 2-7 require Foundry Local runtime installed          │
│     on the end user's device. We do NOT build this.             │
└─────────────────────────────────────────────────────────────────┘
```

> **Key point**: MDS is a *catalog + distribution* service. It tells the SDK
> what models exist and where to download them. The SDK handles local inference.
> MDS never runs models.

## How MDS Works as an Alternate Catalog

Foundry Local normally fetches models from the **public Azure AI catalog**.
MDS acts as a **private alternate catalog** — the SDK talks to MDS instead of (or
in addition to) the public catalog to discover and download private models.

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
3. The SDK downloads the model binary directly from Azure Blob Storage using the SAS URL.

### Setting the Alternate Catalog URI

Configure the Foundry Local SDK to point at MDS by setting `AzureCatalogUri`
in `additionalSettings`.

#### JavaScript

```javascript
import { FoundryLocalManager } from '@prathikrao/foundry-local-sdk';

const manager = FoundryLocalManager.create({
    appName: 'my-app',
    serviceEndpoint: 'http://localhost:5000',
    logLevel: 'info',
    additionalSettings: {
        // Point at MDS instead of the public catalog
        "AzureCatalogUri": "https://mds-model-distribution.azurewebsites.net"
    }
});

const catalog = manager.catalog;
const models = await catalog.getModels();   // calls POST /catalog on MDS
```

> **Reference sample**: [natke/foundry-local-js](https://github.com/natke/foundry-local-js)

#### C# (.NET)

```csharp
var config = new Configuration
{
    AppName = "my-app",
    LogLevel = Microsoft.AI.Foundry.Local.LogLevel.Information,
};

// Set alternate catalog URI via additional settings
config.AdditionalSettings["AzureCatalogUri"] =
    "https://mds-model-distribution.azurewebsites.net";

await FoundryLocalManager.CreateAsync(config, logger);
var manager = FoundryLocalManager.Instance;
var catalog = await manager.GetCatalogAsync();
var models  = await catalog.ListModelsAsync();  // calls POST /catalog on MDS
```

#### Python

```python
# Using the Foundry Local Python SDK (when available)
from foundry_local import FoundryLocalManager

manager = FoundryLocalManager(
    app_name="my-app",
    service_endpoint="http://localhost:5000",
    additional_settings={
        "AzureCatalogUri": "https://mds-model-distribution.azurewebsites.net"
    }
)
catalog = manager.catalog
models = catalog.get_models()  # calls POST /catalog on MDS
```

### Authentication Flow

The Foundry Local SDK must attach a signed JWT `Authorization: Bearer <token>`
header to every request to MDS. The token is obtained from the customer's own
identity provider (e.g., `company.login.com` or `login.microsoft.com` for
Microsoft staging).

```
1. App → company.login.com  → JWT token
2. App → MDS /catalog       → model list (token validated via JWKS)
3. App → MDS /download      → SAS URL   (token + entitlement check)
4. App → Azure Blob Storage  → model binary (direct download via SAS)
```

### What MDS Already Implements

| SDK Operation        | MDS Endpoint          | Status | Who calls it |
|---------------------|-----------------------|--------|--------------|
| List catalog models | `POST /catalog`       | ✅     | SDK internally (via `AzureCatalogUri`) |
| Get model info      | `GET /models/{name}`  | ✅     | SDK or customer app |
| Download model      | `POST /download`      | ✅     | SDK internally (`model.download()`) |
| Upload model        | `POST /upload`        | ✅     | Customer's CI/CD or admin script |
| Health check        | `GET /health`         | ✅     | Monitoring / health probes |

### What MDS Does NOT Do

| Operation | Who does it | Why |
|-----------|------------|-----|
| Run inference / chat completions | Foundry Local SDK (on device) | Models run locally on user hardware |
| Manage local model cache | Foundry Local SDK | SDK caches downloaded .onnx files |
| Authenticate users | Customer's IdP (`company.login.com`) | MDS only *validates* signed JWTs |
| Install Foundry Local runtime | Customer app packaging | SDK ships as NuGet/npm package |

### Running MDS Locally for Development

```bash
# Start MDS on port 8000
cd src/mds
uvicorn main:app --reload --port 8000

# Then configure the SDK to use http://localhost:8000 as the catalog URI
```

### Catalog Response Format

MDS returns the exact same JSON schema as the public Azure AI catalog:

```json
{
  "indexEntitiesResponse": {
    "totalCount": 2,
    "value": [
      {
        "assetId": "fraud-model-v1",
        "version": "1",
        "annotations": {
          "tags": {
            "author": "phonepe",
            "alias": "fraud-model",
            "task": "classification",
            "inputModalities": "text",
            "outputModalities": "text",
            "license": "proprietary",
            "foundryLocal": "true"
          },
          "systemCatalogData": { "publisher": "MDS", "displayName": "fraud-model" },
          "name": "fraud-model"
        },
        "properties": {
          "name": "fraud-model",
          "version": 1,
          "variantInfo": {
            "parents": [],
            "variantMetadata": {
              "modelType": "onnx",
              "device": "cpu",
              "executionProvider": "cpuexecutionprovider",
              "fileSizeBytes": 5242880
            }
          }
        }
      }
    ],
    "nextSkip": null
  }
}
```

This matches the schema at:
https://learn.microsoft.com/en-us/azure/ai-foundry/foundry-local/reference/reference-catalog-api
