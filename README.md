# Model Distribution Service (MDS)

Private model catalog and distribution API for Foundry Local.
Validates JWT tokens, serves the catalog in Foundry Local SDK format, and returns SAS URLs for model downloads.

```
Foundry Local → MDS (auth + catalog) → SAS URL → Azure Blob Storage
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
│   └── jwks.py            # JWKS key fetching with 1hr cache + stale fallback
├── tests/
│   ├── conftest.py        # Shared pytest fixtures (RSA keypair, JWT tokens)
│   ├── unit/              # Unit tests (all Azure calls mocked)
│   └── integration/       # End-to-end smoke tests (requires running server)
├── docs/
│   ├── JWT_SPECIFICATION.md
│   ├── SDK_INTEGRATION.md # How to point Foundry Local SDK at MDS
│   └── CUSTOMER_ONBOARDING.md
├── infrastructure/
│   ├── onboard.bicep      # Customer onboarding ARM template
│   └── onboard.ps1        # Customer onboarding script
├── scripts/
│   └── deploy_to_azure.ps1
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

| Method | Path                              | Auth        | Description                            |
|--------|-----------------------------------|-------------|----------------------------------------|
| GET    | `/health`                         | No          | Health check                           |
| GET    | `/models`                         | No          | List all models in registry            |
| GET    | `/status`                         | JWT         | Service status and uptime              |
| POST   | `/catalog`                        | JWT         | Foundry Local SDK catalog endpoint     |
| POST   | `/catalog/foundrylocal/{api_key}` | API key     | FL catalog (key in URL path)           |
| POST   | `/catalog/foundrylocal`           | API key/JWT | FL catalog (key/JWT in header)         |
| GET    | `/models/{name}`                  | JWT         | Single model detail (FL format)        |
| GET    | `/models/{name}/versions`         | JWT         | List model versions                    |
| POST   | `/download`                       | JWT/API key | Get SAS download URL(s)                |
| DELETE | `/models/{name}`                  | JWT         | Delete model from registry + blobs     |
| GET    | `/models/sync`                    | JWT         | Cross-reference registry vs blob storage |
| POST   | `/admin/register`                 | Admin key   | Register a new customer                |
| POST   | `/admin/offboard`                 | Admin key   | Remove a customer                      |
| POST   | `/admin/refresh-jwks/{id}`        | Admin key   | Force-refresh JWKS cache for customer  |

## SDK Integration

The Foundry Local SDK can use MDS as a private catalog. Configure via `PrivateCatalogUri`:

```json
{
    "PrivateCatalogUri": "https://mds-model-distribution.azurewebsites.net",
    "PrivateCatalogClientId": "phonepe",
    "PrivateCatalogClientSecret": "<secret>"
}
```

Or use the API-key-in-URL approach (no client credentials needed):

```json
{
    "AzureCatalogUri": "https://mds-model-distribution.azurewebsites.net/catalog/foundrylocal/<api-key>"
}
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

| Variable                | Default                | Description                         |
|-------------------------|------------------------|-------------------------------------|
| `REGISTRY_NAME`         | `fl_private_model`     | Azure ML Registry name              |
| `STORAGE_ACCOUNT`       | `customermodelstorage` | Azure Blob Storage account          |
| `STORAGE_CONTAINER`     | `models`               | Blob container name                 |
| `MDS_ADMIN_KEY`         | *(required)*           | Admin key for `/admin/*` endpoints  |
| `MDS_APP_NAME`          | `mds-model-distribution` | App name (used in catalog URLs)   |
| `MDS_CUSTOMERS_FILE`    | *(auto-detected)*      | Path to customers.json              |
| `MDS_DISABLE_RATE_LIMIT`| `false`                | Disable rate limiting (`true`/`1`)  |

### Per-Customer Overrides

| Variable                       | Description                        |
|--------------------------------|------------------------------------|
| `CUSTOMER_<ID>_REGISTRY`       | Dedicated ML Registry for customer |
| `CUSTOMER_<ID>_STORAGE`        | Dedicated Storage for customer     |
| `CUSTOMER_<ID>_ISSUER`         | JWT issuer URL override            |
| `CUSTOMER_<ID>_JWKS_URL`       | JWKS endpoint URL override         |
| `CUSTOMER_<ID>_API_KEY`        | API key for FL catalog access      |

## Deployment

```powershell
.\scripts\deploy_to_azure.ps1
```

Deployed at: `https://mds-model-distribution.azurewebsites.net`
