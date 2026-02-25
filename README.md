# Model Distribution Service (MDS)

Private model catalog and distribution API for Foundry Local.
Validates JWT tokens, serves the catalog in Foundry Local SDK format, and returns SAS URLs for model downloads.

```
Customer App → JWT → MDS → SAS URL → Azure Blob Storage
```

## Project Structure

```
MDS/
├── src/mds/              # Application source
│   ├── main.py            # FastAPI app & route handlers
│   ├── auth.py            # JWT validation & entitlement checks
│   ├── azure_clients.py   # Azure ML Registry + Blob Storage clients
│   ├── catalog.py         # Foundry Local catalog Pydantic models & response builder
│   ├── customers.py       # Customer config & JWKS key resolution
│   ├── jwks.py            # JWKS key fetching with 1hr cache + stale fallback
│   └── metadata.py        # Auto-extract metadata from ONNX model files on upload
├── tests/
│   ├── conftest.py        # Shared pytest fixtures (RSA keypair, JWT tokens)
│   ├── unit/              # Unit tests (all Azure calls mocked)
│   └── integration/       # End-to-end smoke tests (requires running server)
├── docs/
│   ├── JWT_SPECIFICATION.md
│   └── SDK_INTEGRATION.md # How to point Foundry Local SDK at MDS
├── infrastructure/
│   ├── onboard.bicep      # Customer onboarding ARM template
│   └── onboard.ps1        # Customer onboarding script
├── scripts/
│   └── deploy_to_azure.ps1
├── models/                # Test model files (.onnx, gitignored)
├── pyproject.toml         # Build config, pytest settings, ruff config
├── requirements.txt       # Pip dependencies
├── .env.example           # Environment variable template
├── .gitignore
└── ROADMAP.md
```

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run the server
cd src/mds
uvicorn main:app --reload --port 8000
```

## API Endpoints

| Method | Path              | Auth | Description                            |
|--------|-------------------|------|----------------------------------------|
| GET    | `/health`         | No   | Health check                           |
| GET    | `/models`         | No   | List all models in registry            |
| POST   | `/catalog`        | JWT  | Foundry Local SDK catalog endpoint     |
| GET    | `/models/{name}`  | JWT  | Single model detail (FL format)        |
| POST   | `/download`       | JWT  | Get SAS download URL(s) or zip archive |
| POST   | `/upload`         | JWT  | Upload model with FL metadata          |
| POST   | `/upload/begin`   | JWT  | Begin staged upload (returns SAS URL)  |
| POST   | `/upload/complete`| JWT  | Complete staged upload → register      |

## SDK Integration

The Foundry Local SDK can use MDS as an alternate private catalog:

```javascript
const manager = FoundryLocalManager.create({
    appName: 'my-app',
    serviceEndpoint: 'http://localhost:5000',
    logLevel: 'info',
    additionalSettings: {
        "AzureCatalogUri": "https://mds-model-distribution.azurewebsites.net"
    }
});
```

See [`docs/SDK_INTEGRATION.md`](docs/SDK_INTEGRATION.md) for full integration guide.

## Running Tests

```bash
# Install test dependencies
pip install pytest httpx

# Run unit tests (no Azure credentials needed)
pytest tests/unit/ -v

# Run integration tests (requires running server + Azure credentials)
pytest tests/integration/ -v -m integration
```

## Environment Variables

| Variable            | Default                | Description                  |
|---------------------|------------------------|------------------------------|
| `REGISTRY_NAME`     | `fl_private_model`     | Azure ML Registry name       |
| `STORAGE_ACCOUNT`   | `customermodelstorage` | Azure Blob Storage account   |
| `STORAGE_CONTAINER` | `models`               | Blob container name          |

### Per-Customer Overrides

| Variable                       | Description                        |
|--------------------------------|------------------------------------|
| `CUSTOMER_<ID>_REGISTRY`       | Dedicated ML Registry for customer |
| `CUSTOMER_<ID>_STORAGE`        | Dedicated Storage for customer     |
| `CUSTOMER_<ID>_ISSUER`         | JWT issuer URL override            |
| `CUSTOMER_<ID>_JWKS_URL`       | JWKS endpoint URL override         |

## Deployment

```powershell
.\scripts\deploy_to_azure.ps1
```

Deployed at: `https://mds-model-distribution.azurewebsites.net`